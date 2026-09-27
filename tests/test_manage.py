"""Tests for adding and removing cameras by hand."""

import tempfile
import unittest
from pathlib import Path

import camtool
from camera_monitor.database import Database
from camera_monitor.inventory import load_cameras
from tests.fake_camera import FakeCamera

CONFIG = """
site: {name: "Test Site"}
inventory: {file: cameras.csv}
credentials: {default: {username: admin, password: secret}}
checks: {workers: 4, tcp_timeout: 2.0, http_timeout: 5.0, offline_after_failures: 1}
database: {file: monitor.db}
alerts: {enabled: false}
"""


class ManageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "config.yaml").write_text(CONFIG, encoding="utf-8")
        self.config_path = str(self.root / "config.yaml")
        self.csv = self.root / "cameras.csv"

    def tearDown(self):
        self.dir.cleanup()

    def run_cmd(self, *args):
        return camtool.main(["--config", self.config_path, *args])

    def cameras(self):
        return load_cameras(self.csv)

    # ------------------------------------------------------------------ add

    def test_add_creates_the_file_when_it_does_not_exist(self):
        self.assertFalse(self.csv.exists())
        self.assertEqual(self.run_cmd("add", "10.10.12.64", "--name", "Reception"), 0)
        cameras = self.cameras()
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0].ip, "10.10.12.64")
        self.assertEqual(cameras[0].name, "Reception")

    def test_all_the_optional_fields_are_stored(self):
        self.run_cmd(
            "add", "10.10.12.64", "--name", "Ramp", "--location", "Basement",
            "--brand", "dahua", "--http-port", "8000", "--rtsp-port", "8554",
            "--username", "operator", "--password", "pw", "--notes", "east wall",
        )
        camera = self.cameras()[0]
        self.assertEqual(camera.location, "Basement")
        self.assertEqual(camera.brand, "dahua")
        self.assertEqual(camera.http_port, 8000)
        self.assertEqual(camera.rtsp_port, 8554)
        self.assertEqual(camera.username, "operator")
        self.assertEqual(camera.notes, "east wall")

    def test_adding_does_not_disturb_existing_cameras(self):
        self.run_cmd("add", "10.10.12.64", "--name", "First", "--location", "Lobby")
        self.run_cmd("add", "10.10.12.65", "--name", "Second")
        cameras = {camera.ip: camera for camera in self.cameras()}
        self.assertEqual(len(cameras), 2)
        self.assertEqual(cameras["10.10.12.64"].name, "First")
        self.assertEqual(cameras["10.10.12.64"].location, "Lobby")

    def test_duplicate_is_refused_unless_update_is_given(self):
        self.run_cmd("add", "10.10.12.64", "--name", "Original")
        self.assertEqual(self.run_cmd("add", "10.10.12.64", "--name", "Replacement"), 0)
        self.assertEqual(self.cameras()[0].name, "Original", "must not overwrite silently")

        self.run_cmd("add", "10.10.12.64", "--name", "Replacement", "--update")
        self.assertEqual(self.cameras()[0].name, "Replacement")

    def test_invalid_ip_is_rejected_and_nothing_is_written(self):
        self.assertEqual(self.run_cmd("add", "not-an-ip"), 1)
        self.assertFalse(self.csv.exists())

    def test_one_bad_address_does_not_stop_the_good_ones(self):
        listing = self.root / "ips.txt"
        listing.write_text("10.10.12.64\nnonsense\n10.10.12.66\n", encoding="utf-8")
        self.run_cmd("add", "--from-file", str(listing))
        self.assertEqual([c.ip for c in self.cameras()], ["10.10.12.64", "10.10.12.66"])

    def test_add_with_no_arguments_explains_itself(self):
        self.assertEqual(self.run_cmd("add"), 1)

    def test_disabled_camera_is_recorded_but_not_active(self):
        self.run_cmd("add", "10.10.12.64", "--disabled")
        self.assertFalse(self.cameras()[0].enabled)

    # ------------------------------------------------------------ bulk add

    def test_bulk_add_from_a_plain_list(self):
        listing = self.root / "ips.txt"
        listing.write_text(
            "# cameras exported from the NVR\n"
            "10.10.12.64,Reception,Ground Floor\n"
            "10.10.12.65,Parking Ramp\n"
            "\n"
            "10.10.12.66\n",
            encoding="utf-8",
        )
        self.assertEqual(self.run_cmd("add", "--from-file", str(listing)), 0)
        cameras = {camera.ip: camera for camera in self.cameras()}
        self.assertEqual(len(cameras), 3)
        self.assertEqual(cameras["10.10.12.64"].location, "Ground Floor")
        self.assertEqual(cameras["10.10.12.65"].name, "Parking Ramp")
        # No name given, so the default placeholder is used.
        self.assertEqual(cameras["10.10.12.66"].name, "camera-10-10-12-66")

    def test_bulk_add_reports_a_missing_file(self):
        self.assertEqual(self.run_cmd("add", "--from-file", str(self.root / "nope.txt")), 1)

    # --------------------------------------------------------------- check

    def test_check_reports_a_reachable_camera(self):
        with FakeCamera(brand="hikvision", storage="failed") as camera:
            code = self.run_cmd(
                "add", "127.0.0.1", "--name", "Live", "--http-port", str(camera.port), "--check"
            )
        self.assertEqual(code, 0)
        self.assertEqual(self.cameras()[0].ip, "127.0.0.1")

    def test_check_on_an_unreachable_camera_still_saves_it(self):
        # Adding must not depend on the camera answering - you often add
        # cameras before they are installed.
        self.assertEqual(self.run_cmd("add", "127.0.0.9", "--http-port", "1", "--check"), 0)
        self.assertEqual(len(self.cameras()), 1)

    # -------------------------------------------------------------- remove

    def test_remove_deletes_the_row(self):
        self.run_cmd("add", "10.10.12.64")
        self.run_cmd("add", "10.10.12.65")
        self.assertEqual(self.run_cmd("remove", "10.10.12.64"), 0)
        self.assertEqual([c.ip for c in self.cameras()], ["10.10.12.65"])

    def test_remove_several_at_once(self):
        for last in (64, 65, 66):
            self.run_cmd("add", f"10.10.12.{last}")
        self.run_cmd("remove", "10.10.12.64", "10.10.12.66")
        self.assertEqual([c.ip for c in self.cameras()], ["10.10.12.65"])

    def test_removing_an_unknown_ip_is_reported(self):
        self.run_cmd("add", "10.10.12.64")
        self.assertEqual(self.run_cmd("remove", "10.10.12.99"), 1)
        self.assertEqual(len(self.cameras()), 1)

    def test_remove_also_clears_the_recorded_status(self):
        with FakeCamera(brand="hikvision") as camera:
            self.run_cmd("add", "127.0.0.1", "--http-port", str(camera.port))
            self.run_cmd("scan", "--no-alerts")
            with Database(self.root / "monitor.db") as db:
                self.assertEqual(len(db.all_status()), 1)
            self.run_cmd("remove", "127.0.0.1")
            with Database(self.root / "monitor.db") as db:
                self.assertEqual(db.all_status(), [], "dashboard must not show deleted cameras")


if __name__ == "__main__":
    unittest.main()
