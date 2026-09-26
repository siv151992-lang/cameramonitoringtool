"""Tests for offline debouncing, event generation and alert rate limiting."""

import tempfile
import unittest
from pathlib import Path

from camera_monitor.alerts import AlertManager
from camera_monitor.config import Config, DEFAULTS, _merge
from camera_monitor.database import Database
from camera_monitor.health import CheckResult, apply_results, summarise
from camera_monitor.inventory import Camera
from camera_monitor.probes.base import StorageInfo, StorageState

import copy


def make_config(**overrides) -> Config:
    data = _merge(copy.deepcopy(DEFAULTS), overrides)
    return Config(data, None)


def result(camera, online=True, storage=None):
    return CheckResult(
        camera=camera,
        online=online,
        latency_ms=1.0 if online else None,
        error="" if online else "timed out",
        storage=storage or StorageInfo(),
        storage_checked=storage is not None,
        detected_brand="hikvision" if storage else "",
    )


class DebounceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.dir.name) / "test.db")
        self.camera = Camera(ip="192.168.1.5", name="Lobby")
        self.config = make_config(checks={"offline_after_failures": 2})

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_single_missed_check_does_not_declare_offline(self):
        apply_results(self.db, [result(self.camera, online=True)], self.config)
        records, events = apply_results(
            self.db, [result(self.camera, online=False)], self.config
        )
        self.assertEqual(records[0]["online"], 1, "one failure must not trip the alarm")
        self.assertEqual(records[0]["consecutive_failures"], 1)
        self.assertEqual(events, [])

    def test_second_consecutive_failure_declares_offline(self):
        apply_results(self.db, [result(self.camera, online=True)], self.config)
        apply_results(self.db, [result(self.camera, online=False)], self.config)
        records, events = apply_results(
            self.db, [result(self.camera, online=False)], self.config
        )
        self.assertEqual(records[0]["online"], 0)
        self.assertEqual([event["kind"] for event in events], ["offline"])

    def test_recovery_raises_an_online_event(self):
        for _ in range(3):
            apply_results(self.db, [result(self.camera, online=False)], self.config)
        records, events = apply_results(
            self.db, [result(self.camera, online=True)], self.config
        )
        self.assertEqual(records[0]["online"], 1)
        self.assertEqual(records[0]["consecutive_failures"], 0)
        self.assertEqual([event["kind"] for event in events], ["online"])

    def test_first_ever_check_is_believed_immediately(self):
        records, events = apply_results(
            self.db, [result(self.camera, online=False)], self.config
        )
        self.assertEqual(records[0]["online"], 0)
        self.assertEqual([event["kind"] for event in events], ["offline"])

    def test_no_duplicate_event_while_it_stays_offline(self):
        apply_results(self.db, [result(self.camera, online=False)], self.config)
        _, events = apply_results(self.db, [result(self.camera, online=False)], self.config)
        self.assertEqual(events, [])


class StorageEventTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.dir.name) / "test.db")
        self.camera = Camera(ip="192.168.1.6", name="Parking")
        self.config = make_config()

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_card_failure_raises_a_critical_event(self):
        healthy = StorageInfo(state=StorageState.OK, message="28160 MB free")
        broken = StorageInfo(state=StorageState.FAILED, message="hdd1: status 'error'")
        apply_results(self.db, [result(self.camera, storage=healthy)], self.config)
        records, events = apply_results(
            self.db, [result(self.camera, storage=broken)], self.config
        )
        self.assertEqual(records[0]["storage_state"], "failed")
        self.assertEqual([event["kind"] for event in events], ["sd_card"])
        self.assertEqual(events[0]["severity"], "critical")

    def test_repeat_failure_does_not_repeat_the_event(self):
        broken = StorageInfo(state=StorageState.FAILED, message="hdd1: status 'error'")
        apply_results(self.db, [result(self.camera, storage=broken)], self.config)
        _, events = apply_results(self.db, [result(self.camera, storage=broken)], self.config)
        self.assertEqual(events, [])

    def test_card_recovery_is_reported(self):
        broken = StorageInfo(state=StorageState.FAILED, message="error")
        healthy = StorageInfo(state=StorageState.OK, message="ok")
        apply_results(self.db, [result(self.camera, storage=broken)], self.config)
        _, events = apply_results(self.db, [result(self.camera, storage=healthy)], self.config)
        self.assertEqual([event["kind"] for event in events], ["sd_card_ok"])

    def test_skipping_the_storage_check_keeps_the_previous_answer(self):
        broken = StorageInfo(state=StorageState.FAILED, message="hdd1: status 'error'")
        apply_results(self.db, [result(self.camera, storage=broken)], self.config)
        # A cycle that only checks reachability must not reset the SD state.
        records, _ = apply_results(self.db, [result(self.camera, storage=None)], self.config)
        self.assertEqual(records[0]["storage_state"], "failed")
        self.assertIn("error", records[0]["storage_message"])

    def test_offline_camera_is_not_reported_as_an_sd_failure(self):
        records, events = apply_results(
            self.db, [result(self.camera, online=False)], self.config
        )
        self.assertEqual(records[0]["storage_state"], "unknown")
        self.assertEqual([event["kind"] for event in events], ["offline"])


class SummaryTests(unittest.TestCase):
    def test_counts_add_up(self):
        records = [
            {"online": 1, "storage_state": "ok"},
            {"online": 1, "storage_state": "failed"},
            {"online": 0, "storage_state": "unknown"},
            {"online": 1, "storage_state": "missing"},
        ]
        counts = summarise(records)
        self.assertEqual(counts["total"], 4)
        self.assertEqual(counts["online"], 3)
        self.assertEqual(counts["offline"], 1)
        self.assertEqual(counts["sd_failed"], 2)   # failed + missing
        self.assertEqual(counts["sd_ok"], 1)
        self.assertEqual(counts["sd_unknown"], 1)


class AlertRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.dir.name) / "test.db")
        self.config = make_config(alerts={"console": False, "min_repeat_hours": 6})
        self.manager = AlertManager(self.config, self.db)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def offline_record(self, ip="192.168.1.9"):
        return {
            "ip": ip, "name": "Cam", "location": "Lobby", "online": 0,
            "storage_state": "unknown", "storage_message": "", "last_online_at": None,
        }

    def test_ongoing_problem_is_not_repeated_every_cycle(self):
        self.assertTrue(self.manager.notify([], [self.offline_record()]))
        self.assertFalse(self.manager.notify([], [self.offline_record()]))

    def test_recovery_resets_the_limiter_so_a_relapse_alerts_at_once(self):
        self.manager.notify([], [self.offline_record()])
        recovery = {
            "ip": "192.168.1.9", "name": "Cam", "kind": "online",
            "message": "camera is responding again", "severity": "info",
        }
        self.manager.notify([recovery], [])
        self.assertTrue(
            self.manager.notify([], [self.offline_record()]),
            "a camera that fails again must alert immediately",
        )

    def test_many_cameras_produce_one_digest(self):
        records = [self.offline_record(f"192.168.1.{n}") for n in range(20, 40)]
        groups = self.manager.collect([], records)
        subject, body = self.manager.build_message(groups)
        self.assertIn("20 offline", subject)
        self.assertEqual(body.count("192.168.1."), 20)

    def test_nothing_wrong_means_no_notification(self):
        healthy = {
            "ip": "192.168.1.9", "name": "Cam", "location": "", "online": 1,
            "storage_state": "ok", "storage_message": "", "last_online_at": None,
        }
        self.assertFalse(self.manager.notify([], [healthy]))


if __name__ == "__main__":
    unittest.main()
