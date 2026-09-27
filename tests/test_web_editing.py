"""Tests for adding and removing cameras through the dashboard's API."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from camera_monitor.config import load_config
from camera_monitor.database import Database
from camera_monitor.inventory import load_cameras
from camera_monitor.web.app import create_server
from tests.fake_camera import FakeCamera

CONFIG = """
site: {name: "Test Site"}
inventory: {file: cameras.csv}
credentials: {default: {username: admin, password: secret}}
checks: {workers: 4, tcp_timeout: 2.0, http_timeout: 5.0, offline_after_failures: 1}
database: {file: monitor.db}
alerts: {enabled: false}
web: {host: 127.0.0.1, port: 0, allow_editing: true}
"""


class WebEditingBase(unittest.TestCase):
    config_text = CONFIG

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "config.yaml").write_text(self.config_text, encoding="utf-8")
        self.config = load_config(self.root / "config.yaml")
        self.db = Database(self.root / "monitor.db")
        self.server = create_server(self.config, self.db)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()
        self.dir.cleanup()

    def post(self, path, body, content_type="application/json", origin=None):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        request = urllib.request.Request(self.base + path, data=data, method="POST")
        if content_type:
            request.add_header("Content-Type", content_type)
        if origin:
            request.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, {}

    def cameras(self):
        path = self.root / "cameras.csv"
        return load_cameras(path) if path.exists() else []


class AddCameraTests(WebEditingBase):
    def test_add_creates_the_camera(self):
        status, body = self.post("/api/cameras", {"ip": "10.10.12.64", "name": "Reception"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        cameras = self.cameras()
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0].ip, "10.10.12.64")
        self.assertEqual(cameras[0].name, "Reception")

    def test_all_fields_round_trip(self):
        self.post(
            "/api/cameras",
            {
                "ip": "10.10.12.65", "name": "Ramp", "location": "Basement",
                "brand": "dahua", "http_port": 8000, "rtsp_port": "8554",
                "username": "operator", "password": "pw",
            },
        )
        camera = self.cameras()[0]
        self.assertEqual(camera.location, "Basement")
        self.assertEqual(camera.brand, "dahua")
        self.assertEqual(camera.http_port, 8000)
        self.assertEqual(camera.rtsp_port, 8554)
        self.assertEqual(camera.username, "operator")

    def test_invalid_ip_is_rejected(self):
        status, body = self.post("/api/cameras", {"ip": "not-an-ip"})
        self.assertEqual(status, 400)
        self.assertIn("valid IP", body["error"])
        self.assertEqual(self.cameras(), [])

    def test_missing_ip_is_rejected(self):
        status, _ = self.post("/api/cameras", {"name": "No address"})
        self.assertEqual(status, 400)

    def test_duplicate_is_refused(self):
        self.post("/api/cameras", {"ip": "10.10.12.64", "name": "First"})
        status, body = self.post("/api/cameras", {"ip": "10.10.12.64", "name": "Second"})
        self.assertEqual(status, 409)
        self.assertIn("already", body["error"])
        self.assertEqual(self.cameras()[0].name, "First")

    def test_a_bad_port_is_reported_not_silently_defaulted(self):
        status, body = self.post("/api/cameras", {"ip": "10.10.12.64", "http_port": "99999"})
        self.assertEqual(status, 400)
        self.assertIn("between 1 and 65535", body["error"])
        self.assertEqual(self.cameras(), [])

    def test_a_non_numeric_port_is_reported(self):
        status, body = self.post("/api/cameras", {"ip": "10.10.12.64", "http_port": "eighty"})
        self.assertEqual(status, 400)
        self.assertIn("must be a number", body["error"])

    def test_unknown_brand_is_rejected(self):
        status, body = self.post("/api/cameras", {"ip": "10.10.12.64", "brand": "sony"})
        self.assertEqual(status, 400)
        self.assertIn("unknown brand", body["error"])

    def test_check_reports_a_live_camera(self):
        with FakeCamera(brand="hikvision", storage="failed") as camera:
            status, body = self.post(
                "/api/cameras",
                {"ip": "127.0.0.1", "http_port": camera.port, "check": True},
            )
        self.assertEqual(status, 200)
        self.assertTrue(body["check"]["online"])
        self.assertEqual(body["check"]["storage_state"], "failed")

    def test_check_on_a_dead_camera_still_saves_it(self):
        status, body = self.post(
            "/api/cameras", {"ip": "127.0.0.9", "http_port": 1, "rtsp_port": 1, "check": True}
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["check"]["online"])
        self.assertEqual(len(self.cameras()), 1)


class RemoveCameraTests(WebEditingBase):
    def test_remove_deletes_the_camera(self):
        self.post("/api/cameras", {"ip": "10.10.12.64"})
        self.post("/api/cameras", {"ip": "10.10.12.65"})
        status, _ = self.post("/api/cameras/remove", {"ip": "10.10.12.64"})
        self.assertEqual(status, 200)
        self.assertEqual([c.ip for c in self.cameras()], ["10.10.12.65"])

    def test_removing_an_unknown_camera_is_a_404(self):
        self.post("/api/cameras", {"ip": "10.10.12.64"})
        status, _ = self.post("/api/cameras/remove", {"ip": "10.10.12.99"})
        self.assertEqual(status, 404)
        self.assertEqual(len(self.cameras()), 1)

    def test_remove_clears_the_recorded_status(self):
        self.post("/api/cameras", {"ip": "10.10.12.64"})
        self.db.upsert_status([{
            "ip": "10.10.12.64", "name": "Cam", "location": "", "brand": "auto",
            "online": 0, "consecutive_failures": 1, "latency_ms": None, "error": "",
            "storage_state": "unknown", "storage_message": "", "storage_checked_at": None,
            "last_checked_at": None, "last_online_at": None, "last_change_at": None,
        }])
        self.assertEqual(len(self.db.all_status()), 1)
        self.post("/api/cameras/remove", {"ip": "10.10.12.64"})
        self.assertEqual(self.db.all_status(), [], "deleted camera must leave the dashboard")


class WriteGuardTests(WebEditingBase):
    """The dashboard can modify the inventory, so the write path needs guarding."""

    def test_form_content_type_is_refused(self):
        # A cross-site HTML form can post this type without a CORS preflight.
        status, _ = self.post(
            "/api/cameras", b"ip=10.10.12.64",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.cameras(), [])

    def test_cross_site_origin_is_refused(self):
        status, body = self.post(
            "/api/cameras", {"ip": "10.10.12.64"}, origin="http://evil.example.com"
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-site", body["error"])
        self.assertEqual(self.cameras(), [])

    def test_same_origin_is_allowed(self):
        host = self.base.replace("http://", "")
        status, _ = self.post("/api/cameras", {"ip": "10.10.12.64"}, origin=f"http://{host}")
        self.assertEqual(status, 200)

    def test_oversized_body_is_refused(self):
        status, _ = self.post("/api/cameras", {"ip": "10.10.12.64", "notes": "x" * 70000})
        self.assertEqual(status, 400)

    def test_malformed_json_is_refused(self):
        status, _ = self.post("/api/cameras", b"{not json")
        self.assertEqual(status, 400)

    def test_unknown_post_route_is_a_404(self):
        status, _ = self.post("/api/nonsense", {"ip": "10.10.12.64"})
        self.assertEqual(status, 404)

    def test_get_routes_still_work(self):
        with urllib.request.urlopen(self.base + "/api/config", timeout=5) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["allow_editing"])
        self.assertIn("hikvision", payload["brands"])


class EditingDisabledTests(WebEditingBase):
    config_text = CONFIG.replace("allow_editing: true", "allow_editing: false")

    def test_adding_is_refused_when_editing_is_off(self):
        status, body = self.post("/api/cameras", {"ip": "10.10.12.64"})
        self.assertEqual(status, 403)
        self.assertIn("editing is disabled", body["error"])
        self.assertEqual(self.cameras(), [])

    def test_removing_is_refused_when_editing_is_off(self):
        status, _ = self.post("/api/cameras/remove", {"ip": "10.10.12.64"})
        self.assertEqual(status, 403)

    def test_the_page_is_told_not_to_show_the_controls(self):
        with urllib.request.urlopen(self.base + "/api/config", timeout=5) as response:
            self.assertFalse(json.loads(response.read())["allow_editing"])
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            self.assertIn("ALLOW_EDITING = false", response.read().decode())


class EditingNeedsLoginTests(WebEditingBase):
    config_text = CONFIG.replace(
        "web: {host: 127.0.0.1, port: 0, allow_editing: true}",
        'web: {host: 127.0.0.1, port: 0, allow_editing: true, '
        'username: "viewer", password: "letmein"}',
    )

    def test_anonymous_write_is_challenged(self):
        status, _ = self.post("/api/cameras", {"ip": "10.10.12.64"})
        self.assertEqual(status, 401)
        self.assertEqual(self.cameras(), [])


if __name__ == "__main__":
    unittest.main()


class NewCameraAppearsTests(WebEditingBase):
    """A camera added from the dashboard must show up in the table at once."""

    def status_rows(self):
        with urllib.request.urlopen(self.base + "/api/status", timeout=5) as response:
            return json.loads(response.read())["cameras"]

    def test_checked_camera_appears_immediately(self):
        with FakeCamera(brand="hikvision", storage="ok") as camera:
            self.post(
                "/api/cameras",
                {"ip": "127.0.0.1", "name": "New Cam", "http_port": camera.port, "check": True},
            )
        rows = self.status_rows()
        self.assertEqual([row["ip"] for row in rows], ["127.0.0.1"])
        self.assertTrue(rows[0]["online"])
        self.assertEqual(rows[0]["storage_state"], "ok")

    def test_unchecked_camera_is_reported_as_pending(self):
        status, body = self.post("/api/cameras", {"ip": "10.10.12.64", "check": False})
        self.assertEqual(status, 200)
        self.assertTrue(body["pending"], "the operator must be told it is not in the table yet")
        self.assertEqual(self.status_rows(), [])


class EditCameraTests(WebEditingBase):
    def inventory(self):
        with urllib.request.urlopen(self.base + "/api/cameras", timeout=5) as response:
            return json.loads(response.read())["cameras"]

    def setUp(self):
        super().setUp()
        self.post(
            "/api/cameras",
            {
                "ip": "10.10.12.64", "name": "Old Name", "location": "Old Place",
                "brand": "hikvision", "http_port": 80, "username": "admin",
                "password": "original", "check": False,
            },
        )

    def test_fields_are_updated(self):
        status, _ = self.post(
            "/api/cameras/update",
            {
                "ip": "10.10.12.64", "name": "New Name", "location": "New Place",
                "brand": "dahua", "http_port": 8000, "rtsp_port": 8554,
                "username": "operator", "check": False,
            },
        )
        self.assertEqual(status, 200)
        camera = self.cameras()[0]
        self.assertEqual(camera.name, "New Name")
        self.assertEqual(camera.location, "New Place")
        self.assertEqual(camera.brand, "dahua")
        self.assertEqual(camera.http_port, 8000)
        self.assertEqual(camera.rtsp_port, 8554)
        self.assertEqual(camera.username, "operator")

    def test_blank_password_keeps_the_existing_one(self):
        self.post("/api/cameras/update", {"ip": "10.10.12.64", "name": "X", "check": False})
        self.assertEqual(self.cameras()[0].password, "original")

    def test_a_new_password_replaces_it(self):
        self.post(
            "/api/cameras/update",
            {"ip": "10.10.12.64", "name": "X", "password": "changed", "check": False},
        )
        self.assertEqual(self.cameras()[0].password, "changed")

    def test_passwords_are_never_sent_to_the_browser(self):
        entry = self.inventory()[0]
        self.assertNotIn("password", entry)
        self.assertTrue(entry["has_password"])

    def test_inventory_listing_carries_what_the_form_needs(self):
        entry = self.inventory()[0]
        for key in ("ip", "name", "location", "brand", "http_port", "rtsp_port",
                    "username", "enabled"):
            self.assertIn(key, entry)

    def test_a_camera_can_be_paused_and_resumed(self):
        self.post("/api/cameras/update", {"ip": "10.10.12.64", "enabled": False, "check": False})
        self.assertFalse(self.cameras()[0].enabled)
        self.post("/api/cameras/update", {"ip": "10.10.12.64", "enabled": True, "check": False})
        self.assertTrue(self.cameras()[0].enabled)

    def test_editing_an_unknown_camera_is_a_404(self):
        status, _ = self.post("/api/cameras/update", {"ip": "10.10.12.99", "name": "X"})
        self.assertEqual(status, 404)

    def test_a_bad_port_is_refused_and_nothing_changes(self):
        status, body = self.post(
            "/api/cameras/update", {"ip": "10.10.12.64", "name": "X", "http_port": "99999"}
        )
        self.assertEqual(status, 400)
        self.assertIn("between 1 and 65535", body["error"])
        self.assertEqual(self.cameras()[0].name, "Old Name")

    def test_unknown_brand_is_refused(self):
        status, _ = self.post("/api/cameras/update", {"ip": "10.10.12.64", "brand": "sony"})
        self.assertEqual(status, 400)

    def test_rename_reaches_the_dashboard_table_at_once(self):
        # Status rows carry the label, so an edit must update them too or the
        # table shows the old name until the next check.
        self.db.upsert_status([{
            "ip": "10.10.12.64", "name": "Old Name", "location": "Old Place",
            "brand": "hikvision", "online": 1, "consecutive_failures": 0,
            "latency_ms": 2.0, "error": "", "storage_state": "ok",
            "storage_message": "", "storage_checked_at": None,
            "last_checked_at": None, "last_online_at": None, "last_change_at": None,
        }])
        self.post(
            "/api/cameras/update",
            {"ip": "10.10.12.64", "name": "Renamed", "location": "Lobby", "check": False},
        )
        row = self.db.all_status()[0]
        self.assertEqual(row["name"], "Renamed")
        self.assertEqual(row["location"], "Lobby")

    def test_editing_is_refused_cross_site(self):
        status, _ = self.post(
            "/api/cameras/update", {"ip": "10.10.12.64", "name": "X"},
            origin="http://evil.example.com",
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.cameras()[0].name, "Old Name")


class EditDisabledTests(WebEditingBase):
    config_text = CONFIG.replace("allow_editing: true", "allow_editing: false")

    def test_update_is_refused_when_editing_is_off(self):
        status, _ = self.post("/api/cameras/update", {"ip": "10.10.12.64", "name": "X"})
        self.assertEqual(status, 403)


class BrandDetectionTests(WebEditingBase):
    def test_detected_brand_is_written_back_to_the_camera_list(self):
        # Added as 'auto'; the check identifies it, and the list should say so
        # rather than re-running the guessing on every later check.
        with FakeCamera(brand="dahua", storage="ok") as camera:
            status, body = self.post(
                "/api/cameras",
                {"ip": "127.0.0.1", "brand": "auto", "http_port": camera.port, "check": True},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["check"]["brand"], "dahua")
        self.assertEqual(self.cameras()[0].brand, "dahua")
