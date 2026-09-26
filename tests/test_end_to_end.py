"""End-to-end tests: run the real CLI and the real web server against fake cameras."""

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import camtool
from camera_monitor.config import load_config
from camera_monitor.database import Database
from camera_monitor.web.app import create_server
from tests.fake_camera import FakeCamera

CONFIG_TEMPLATE = """
site:
  name: "Test Site"
inventory:
  file: cameras.csv
credentials:
  default:
    username: admin
    password: secret
checks:
  workers: 8
  tcp_timeout: 2.0
  http_timeout: 5.0
  offline_after_failures: 1
database:
  file: monitor.db
alerts:
  enabled: false
web:
  host: 127.0.0.1
  port: 0
"""


class EndToEndTests(unittest.TestCase):
    """One healthy camera, one with a failed card, one that is not there."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        # Each fake camera gets its own loopback address so the inventory has
        # three distinct IPs, like a real LAN.
        self.healthy = FakeCamera(brand="hikvision", storage="ok", host="127.0.0.1").__enter__()
        self.broken = FakeCamera(brand="dahua", storage="failed", host="127.0.0.2").__enter__()

        (self.root / "config.yaml").write_text(CONFIG_TEMPLATE, encoding="utf-8")
        (self.root / "cameras.csv").write_text(
            "ip,name,location,brand,http_port,rtsp_port,username,password,enabled,notes\n"
            f"127.0.0.1,Healthy Cam,Lobby,auto,{self.healthy.port},{self.healthy.port},,,yes,\n"
            f"127.0.0.2,Broken Card,Basement,auto,{self.broken.port},{self.broken.port},,,yes,\n"
            "127.0.0.3,Dead Cam,Roof,auto,1,1,,,yes,\n",
            encoding="utf-8",
        )
        self.config_path = str(self.root / "config.yaml")

    def tearDown(self):
        self.healthy.__exit__()
        self.broken.__exit__()
        self.dir.cleanup()

    def run_scan(self, *extra):
        return camtool.main(["--config", self.config_path, "scan", "--no-alerts", *extra])

    def test_scan_records_all_three_outcomes(self):
        self.assertEqual(self.run_scan(), 0)

        with Database(self.root / "monitor.db") as db:
            by_ip = {record["ip"]: record for record in db.all_status()}

        self.assertEqual(len(by_ip), 3)

        healthy = by_ip["127.0.0.1"]
        self.assertEqual(healthy["online"], 1)
        self.assertEqual(healthy["storage_state"], "ok")
        self.assertEqual(healthy["brand"], "hikvision", "brand should be auto-detected")

        broken = by_ip["127.0.0.2"]
        self.assertEqual(broken["online"], 1)
        self.assertEqual(broken["storage_state"], "failed")
        self.assertIn("read-only", broken["storage_message"])
        self.assertEqual(broken["brand"], "dahua")

        dead = by_ip["127.0.0.3"]
        self.assertEqual(dead["online"], 0)

    def test_detected_brands_are_written_back_to_the_csv(self):
        self.run_scan()
        rows = (self.root / "cameras.csv").read_text(encoding="utf-8")
        self.assertIn("hikvision", rows)
        self.assertIn("dahua", rows)

    def test_events_are_recorded_for_the_problems(self):
        self.run_scan()
        with Database(self.root / "monitor.db") as db:
            kinds = {(event["ip"], event["kind"]) for event in db.recent_events()}
        self.assertIn(("127.0.0.3", "offline"), kinds)
        self.assertIn(("127.0.0.2", "sd_card"), kinds)

    def test_cameras_removed_from_the_csv_are_dropped_from_the_status(self):
        self.run_scan()
        (self.root / "cameras.csv").write_text(
            "ip,name,http_port\n" f"127.0.0.1,Healthy Cam,{self.healthy.port}\n",
            encoding="utf-8",
        )
        self.run_scan()
        with Database(self.root / "monitor.db") as db:
            self.assertEqual([record["ip"] for record in db.all_status()], ["127.0.0.1"])

    def test_disabled_cameras_are_not_checked(self):
        (self.root / "cameras.csv").write_text(
            "ip,name,http_port,enabled\n" f"127.0.0.1,Healthy Cam,{self.healthy.port},no\n",
            encoding="utf-8",
        )
        self.run_scan()
        with Database(self.root / "monitor.db") as db:
            self.assertEqual(db.all_status(), [])

    def test_report_command_runs_after_a_scan(self):
        self.run_scan()
        exit_code = camtool.main(["--config", self.config_path, "report", "--events", "5"])
        self.assertEqual(exit_code, 0)

    def test_list_and_identify_commands_run(self):
        self.assertEqual(camtool.main(["--config", self.config_path, "list"]), 0)
        self.assertEqual(camtool.main(["--config", self.config_path, "identify"]), 0)


class WebDashboardTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "config.yaml").write_text(CONFIG_TEMPLATE, encoding="utf-8")
        (self.root / "cameras.csv").write_text("ip\n127.0.0.9\n", encoding="utf-8")

        self.config = load_config(self.root / "config.yaml")
        self.db = Database(self.root / "monitor.db")
        self.db.upsert_status(
            [
                {
                    "ip": "127.0.0.9", "name": "Test Cam", "location": "Lobby",
                    "brand": "hikvision", "online": 0, "consecutive_failures": 3,
                    "latency_ms": None, "error": "timed out", "storage_state": "failed",
                    "storage_message": "hdd1: status 'error'",
                    "storage_checked_at": "2026-09-26T10:00:00+00:00",
                    "last_checked_at": "2026-09-26T10:00:00+00:00",
                    "last_online_at": "2026-09-25T09:00:00+00:00",
                    "last_change_at": "2026-09-26T10:00:00+00:00",
                }
            ]
        )
        self.server = create_server(self.config, self.db)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()
        self.dir.cleanup()

    def fetch(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read().decode("utf-8")

    def test_dashboard_page_renders(self):
        status, body = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn("Test Site", body)
        self.assertNotIn("__SITE_NAME__", body)

    def test_status_api_returns_the_camera(self):
        _, body = self.fetch("/api/status")
        payload = json.loads(body)
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["summary"]["offline"], 1)
        self.assertEqual(payload["summary"]["sd_failed"], 1)
        self.assertEqual(payload["cameras"][0]["ip"], "127.0.0.9")
        self.assertFalse(payload["cameras"][0]["online"])

    def test_csv_export_has_a_header_and_the_row(self):
        status, body = self.fetch("/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("ip,name,location", body)
        self.assertIn("127.0.0.9", body)
        self.assertIn("offline", body)

    def test_unknown_route_is_a_clean_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_healthz_is_available_for_uptime_checks(self):
        status, body = self.fetch("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "config.yaml").write_text(
            CONFIG_TEMPLATE + '\n  username: "viewer"\n  password: "letmein"\n',
            encoding="utf-8",
        )
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

    def test_anonymous_request_is_challenged(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/", timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_correct_password_is_accepted(self):
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self.base, "viewer", "letmein")
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(manager))
        with opener.open(self.base + "/healthz", timeout=5) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
