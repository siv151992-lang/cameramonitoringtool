"""Tests for the vendor response parsers and the brand dispatcher."""

import unittest

from camera_monitor.probes import dahua, hikvision, sdcard
from camera_monitor.probes.base import StorageState
from camera_monitor.probes.onvif import _parse_probe_match, name_from_scopes
from camera_monitor.probes.reachability import check_reachable
from tests.fake_camera import FakeCamera


class HikvisionParsingTests(unittest.TestCase):
    def test_healthy_card(self):
        self.assertEqual(hikvision._classify("ok", False)[0], StorageState.OK)

    def test_error_states_are_failures(self):
        for status in ("error", "unformatted", "abnormal", "offline", "notExist"):
            self.assertEqual(
                hikvision._classify(status, False)[0], StorageState.FAILED, status
            )

    def test_read_only_card_is_a_failure(self):
        state, reason = hikvision._classify("ok", True)
        self.assertEqual(state, StorageState.FAILED)
        self.assertIn("read-only", reason)

    def test_unrecognised_status_is_unknown_not_a_false_alarm(self):
        self.assertEqual(hikvision._classify("rebuilding", False)[0], StorageState.UNKNOWN)


class DahuaParsingTests(unittest.TestCase):
    def test_key_value_body_is_grouped_per_device(self):
        body = (
            "list.info[0].Name=SD0\nlist.info[0].State=Active\n"
            "list.info[0].Detail[0].TotalBytes=1048576\n"
            "list.info[1].Name=SD1\nlist.info[1].State=Error\n"
        )
        devices = dahua.group_by_device(dahua.parse_kv(body))
        self.assertEqual(set(devices), {0, 1})
        self.assertEqual(devices[1]["State"], "Error")

    def test_read_only_is_a_failure(self):
        state, reason = dahua._classify("Active", "Read-Only")
        self.assertEqual(state, StorageState.FAILED)
        self.assertIn("read-only", reason)

    def test_blank_body_means_no_card(self):
        self.assertEqual(dahua.group_by_device(dahua.parse_kv("")), {})


class OnvifParsingTests(unittest.TestCase):
    REPLY = """<?xml version="1.0"?>
    <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
                xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
      <e:Body><d:ProbeMatches><d:ProbeMatch>
        <d:Scopes>onvif://www.onvif.org/name/Lobby%20Camera</d:Scopes>
        <d:XAddrs>http://192.168.1.64/onvif/device_service</d:XAddrs>
      </d:ProbeMatch></d:ProbeMatches></e:Body>
    </e:Envelope>"""

    def test_advertised_address_wins_over_sender(self):
        match = _parse_probe_match(self.REPLY, "10.0.0.1")
        self.assertEqual(match["ip"], "192.168.1.64")

    def test_friendly_name_is_url_decoded(self):
        match = _parse_probe_match(self.REPLY, "10.0.0.1")
        self.assertEqual(name_from_scopes(match["scopes"]), "Lobby Camera")

    def test_garbage_reply_is_ignored(self):
        self.assertIsNone(_parse_probe_match("not xml at all", "10.0.0.1"))


class DispatcherTests(unittest.TestCase):
    def test_hikvision_is_detected_automatically(self):
        with FakeCamera(brand="hikvision", storage="ok") as camera:
            info, brand = sdcard.check_storage(
                "127.0.0.1", camera.port, "auto", "admin", "secret", 5.0
            )
        self.assertEqual(brand, "hikvision")
        self.assertEqual(info.state, StorageState.OK)

    def test_dahua_is_detected_after_hikvision_returns_404(self):
        with FakeCamera(brand="dahua", storage="failed") as camera:
            info, brand = sdcard.check_storage(
                "127.0.0.1", camera.port, "auto", "admin", "secret", 5.0
            )
        self.assertEqual(brand, "dahua")
        self.assertEqual(info.state, StorageState.FAILED)

    def test_empty_hdd_list_reports_a_missing_card(self):
        with FakeCamera(brand="hikvision", storage="missing") as camera:
            info, _ = sdcard.check_storage(
                "127.0.0.1", camera.port, "hikvision", "admin", "secret", 5.0
            )
        self.assertEqual(info.state, StorageState.MISSING)

    def test_wrong_password_is_reported_not_guessed_around(self):
        with FakeCamera(brand="hikvision") as camera:
            info, _ = sdcard.check_storage(
                "127.0.0.1", camera.port, "auto", "admin", "wrong", 5.0
            )
        self.assertEqual(info.state, StorageState.UNKNOWN)
        self.assertIn("authentication failed", info.message)

    def test_no_credentials_is_unknown_not_a_failure(self):
        info, _ = sdcard.check_storage("127.0.0.1", 9, "auto", "", "", 1.0)
        self.assertEqual(info.state, StorageState.UNKNOWN)
        self.assertIn("no credentials", info.message)

    def test_stale_brand_falls_back_to_detection(self):
        # Inventory says Hikvision but the camera was swapped for a Dahua.
        with FakeCamera(brand="dahua", storage="ok") as camera:
            info, brand = sdcard.check_storage(
                "127.0.0.1", camera.port, "hikvision", "admin", "secret", 5.0
            )
        self.assertEqual(brand, "dahua")
        self.assertEqual(info.state, StorageState.OK)


class ReachabilityTests(unittest.TestCase):
    def test_open_port_means_online(self):
        with FakeCamera() as camera:
            result = check_reachable("127.0.0.1", [camera.port], 2.0)
        self.assertTrue(result.online)
        self.assertEqual(result.open_port, camera.port)
        self.assertIsNotNone(result.latency_ms)

    def test_refused_port_is_offline_but_flagged_as_a_live_address(self):
        # Nothing listens here. The address answers (refuses) rather than
        # dropping the packet, so the camera is offline but the note says the
        # device itself is on the network - that distinction speeds up repairs.
        result = check_reachable("127.0.0.1", [9], 2.0)
        self.assertFalse(result.online)
        self.assertTrue(result.host_responded)
        self.assertIn("not serving on its ports", result.error)

    def test_dropped_packets_are_offline_with_a_timeout_note(self):
        result = check_reachable("127.0.0.1", [9], 2.0)
        self.assertFalse(result.online)


if __name__ == "__main__":
    unittest.main()


class OnvifAddressChoiceTests(unittest.TestCase):
    """A camera often advertises both an IPv4 and an IPv6 service address.

    Picking the IPv6 one lists the camera under an address nothing else on the
    LAN uses, so the port sweep finds the same device again under its IPv4
    address and the inventory ends up with two rows for one camera.
    """

    @staticmethod
    def reply(xaddrs: str) -> str:
        return f"""<?xml version="1.0"?>
        <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
                    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
          <e:Body><d:ProbeMatches><d:ProbeMatch>
            <d:XAddrs>{xaddrs}</d:XAddrs>
          </d:ProbeMatch></d:ProbeMatches></e:Body>
        </e:Envelope>"""

    def test_ipv4_is_preferred_even_when_listed_second(self):
        match = _parse_probe_match(
            self.reply("http://[fe80::1]/onvif/device_service"
                       " http://192.168.1.64/onvif/device_service"),
            "192.168.1.99",
        )
        self.assertEqual(match["ip"], "192.168.1.64")
        self.assertIn("192.168.1.64", match["xaddr"])

    def test_the_sender_is_used_when_only_ipv6_is_advertised(self):
        # An fe80:: address cannot be connected to without a scope id, so the
        # address the datagram actually came from is more useful.
        match = _parse_probe_match(
            self.reply("http://[fe80::1]/onvif/device_service"), "192.168.1.99"
        )
        self.assertEqual(match["ip"], "192.168.1.99")

    def test_a_single_ipv4_address_is_used_as_is(self):
        match = _parse_probe_match(
            self.reply("http://192.168.1.64/onvif/device_service"), "192.168.1.99"
        )
        self.assertEqual(match["ip"], "192.168.1.64")

    def test_no_addresses_falls_back_to_the_sender(self):
        match = _parse_probe_match(self.reply(""), "192.168.1.99")
        self.assertEqual(match["ip"], "192.168.1.99")
