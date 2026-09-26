"""Tests for reading and writing the camera CSV."""

import tempfile
import unittest
from pathlib import Path

from camera_monitor.inventory import (
    Camera,
    InventoryError,
    load_cameras,
    merge_discovered,
    save_cameras,
)


class CameraTests(unittest.TestCase):
    def test_defaults_are_filled_in(self):
        camera = Camera(ip="192.168.1.10")
        self.assertEqual(camera.name, "camera-192-168-1-10")
        self.assertEqual(camera.http_port, 80)
        self.assertEqual(camera.rtsp_port, 554)
        self.assertTrue(camera.enabled)
        self.assertEqual(camera.brand, "auto")

    def test_invalid_ip_is_rejected(self):
        with self.assertRaises(InventoryError):
            Camera(ip="not-an-ip")

    def test_text_values_are_coerced(self):
        camera = Camera(ip="10.0.0.1", http_port="8000", enabled="no", brand="HIKVISION")
        self.assertEqual(camera.http_port, 8000)
        self.assertFalse(camera.enabled)
        self.assertEqual(camera.brand, "hikvision")

    def test_out_of_range_port_falls_back_to_default(self):
        self.assertEqual(Camera(ip="10.0.0.1", http_port="99999").http_port, 80)
        self.assertEqual(Camera(ip="10.0.0.1", http_port="abc").http_port, 80)

    def test_unknown_brand_becomes_auto(self):
        self.assertEqual(Camera(ip="10.0.0.1", brand="sony").brand, "auto")


class InventoryFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "cameras.csv"

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        cameras = [
            Camera(ip="192.168.1.20", name="Gate", location="Outside"),
            Camera(ip="192.168.1.3", name="Lobby"),
        ]
        save_cameras(self.path, cameras)
        loaded = load_cameras(self.path)
        # Sorted numerically by IP, so .3 comes before .20.
        self.assertEqual([camera.ip for camera in loaded], ["192.168.1.3", "192.168.1.20"])
        self.assertEqual(loaded[1].name, "Gate")

    def test_minimal_file_with_only_ips(self):
        self.path.write_text("ip\n192.168.1.5\n192.168.1.6\n", encoding="utf-8")
        loaded = load_cameras(self.path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].http_port, 80)

    def test_bad_rows_are_skipped_not_fatal(self):
        self.path.write_text(
            "ip,name\n192.168.1.5,Good\nnonsense,Bad\n192.168.1.5,Duplicate\n",
            encoding="utf-8",
        )
        loaded = load_cameras(self.path)
        self.assertEqual([camera.ip for camera in loaded], ["192.168.1.5"])
        self.assertEqual(loaded[0].name, "Good")

    def test_missing_file_explains_what_to_do(self):
        with self.assertRaises(InventoryError) as ctx:
            load_cameras(self.path)
        self.assertIn("discover", str(ctx.exception))

    def test_header_must_contain_ip(self):
        self.path.write_text("address,name\n1.2.3.4,x\n", encoding="utf-8")
        with self.assertRaises(InventoryError):
            load_cameras(self.path)


class MergeTests(unittest.TestCase):
    def test_new_cameras_are_added_and_edits_preserved(self):
        existing = [Camera(ip="192.168.1.5", name="Lobby Cam", location="Ground")]
        discovered = [
            Camera(ip="192.168.1.5", name="camera-192-168-1-5", brand="hikvision"),
            Camera(ip="192.168.1.6"),
        ]
        merged, added = merge_discovered(existing, discovered)
        self.assertEqual(len(merged), 2)
        self.assertEqual([camera.ip for camera in added], ["192.168.1.6"])
        # The operator's name survives, but the detected brand is filled in.
        lobby = next(camera for camera in merged if camera.ip == "192.168.1.5")
        self.assertEqual(lobby.name, "Lobby Cam")
        self.assertEqual(lobby.brand, "hikvision")


if __name__ == "__main__":
    unittest.main()
