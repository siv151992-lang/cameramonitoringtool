"""Tests for the SNMP client and its use in the health check."""

import copy
import tempfile
import unittest
from pathlib import Path

from camera_monitor.config import DEFAULTS, Config, _merge
from camera_monitor.database import Database
from camera_monitor.health import check_camera
from camera_monitor.inventory import Camera
from camera_monitor.probes import snmp
from camera_monitor.probes.base import StorageState
from tests.fake_camera import FakeCamera
from tests.fake_snmp import FakeAgent, integer, octet, timeticks


def make_config(**overrides) -> Config:
    return Config(_merge(copy.deepcopy(DEFAULTS), overrides), None)


class EncodingTests(unittest.TestCase):
    """Checked against the byte sequences the SNMP/BER specs define."""

    def test_sysdescr_oid_encodes_to_the_documented_bytes(self):
        self.assertEqual(
            snmp.encode_oid("1.3.6.1.2.1.1.1.0").hex(), "06082b06010201010100"
        )

    def test_first_two_arcs_are_packed_into_one_byte(self):
        # 1.3 -> 1*40 + 3 = 43 = 0x2b
        self.assertTrue(snmp.encode_oid("1.3.6.1").hex().startswith("06032b06"))

    def test_large_arcs_use_base_128_continuation(self):
        # 39165 in base 128 is 2, 49, 125 -> bytes 82 b1 7d
        # (2*128^2 + 49*128 + 125 = 39165)
        encoded = snmp.encode_oid("1.3.6.1.4.1.39165").hex()
        self.assertTrue(encoded.endswith("82b17d"), encoded)
        self.assertEqual(snmp.decode_oid(bytes.fromhex(encoded[4:])), "1.3.6.1.4.1.39165")

    def test_oid_round_trip(self):
        # 2.999 exercises a first subidentifier (40*2+999) too big for one byte.
        for oid in ("1.3.6.1.2.1.1.3.0", "1.3.6.1.4.1.39165.1.2.3", "0.0", "2.999.1"):
            raw = snmp.encode_oid(oid)
            self.assertEqual(snmp.decode_oid(raw[2:]), oid, oid)

    def test_malformed_oids_are_rejected(self):
        for bad in ("", "abc", "1", "1.x.3", "-1.2", "3.1.2"):
            with self.assertRaises(ValueError, msg=bad):
                snmp.encode_oid(bad)

    def test_long_length_form_for_big_payloads(self):
        # Over 127 bytes the length switches to the long form (0x81 <n>).
        packet = snmp._tlv(0x04, b"x" * 200)
        self.assertEqual(packet[1], 0x81)
        self.assertEqual(packet[2], 200)

    def test_get_request_has_the_expected_shape(self):
        packet, request_id = snmp.build_request(
            ["1.3.6.1.2.1.1.3.0"], "public", "2c", request_id=1
        )
        self.assertEqual(
            packet.hex(),
            "302602010104067075626c6963a019020101020100020100300e300c06082b060102010103000500",
        )
        self.assertEqual(request_id, 1)

    def test_version_1_is_encoded_as_zero(self):
        packet, _ = snmp.build_request(["1.3.6.1.2.1.1.3.0"], "public", "1", request_id=1)
        self.assertIn("02010004", packet.hex())   # INTEGER 0, then community

    def test_unknown_version_is_refused(self):
        with self.assertRaises(ValueError):
            snmp.build_request(["1.3.6.1.2.1.1.3.0"], "public", "3")


class DecodingTests(unittest.TestCase):
    def test_value_types(self):
        self.assertEqual(snmp.decode_value(snmp.TAG_INTEGER, b"\x2a"), 42)
        self.assertEqual(snmp.decode_value(snmp.TAG_INTEGER, b"\xff"), -1)
        self.assertEqual(snmp.decode_value(snmp.TAG_COUNTER32, b"\xff\xff"), 65535)
        self.assertEqual(snmp.decode_value(snmp.TAG_OCTET_STRING, b"hello"), "hello")
        self.assertEqual(snmp.decode_value(snmp.TAG_IP_ADDRESS, b"\x0a\x0a\x0c\x40"), "10.10.12.64")
        self.assertIsNone(snmp.decode_value(snmp.TAG_NULL, b""))

    def test_binary_strings_fall_back_to_hex(self):
        self.assertEqual(snmp.decode_value(snmp.TAG_OCTET_STRING, b"\xff\xfe"), "fffe")

    def test_v2c_exception_markers_are_readable(self):
        self.assertEqual(snmp.decode_value(snmp.TAG_NO_SUCH_OBJECT, b""), "(no such object)")
        self.assertEqual(snmp.decode_value(snmp.TAG_END_OF_MIB_VIEW, b""), "(end of MIB)")

    def test_truncated_packets_are_rejected_not_crashed(self):
        for bad in (b"", b"\x30", b"\x30\x82", b"\x30\x05\x02\x01\x01"):
            with self.assertRaises(snmp.SnmpError):
                snmp.parse_response(bad)

    def test_garbage_is_rejected(self):
        with self.assertRaises(snmp.SnmpError):
            snmp.parse_response(b"this is not BER at all")

    def test_uptime_is_formatted_from_hundredths_of_a_second(self):
        self.assertEqual(snmp.format_uptime(360000), "1h 0m")
        self.assertEqual(snmp.format_uptime(8640000), "1d 0h 0m")
        self.assertEqual(snmp.format_uptime(6000), "1m 0s")


TABLE = {
    "1.3.6.1.2.1.1.1.0": octet("Hikvision IP Camera DS-2CD2143G0-I"),
    "1.3.6.1.2.1.1.3.0": timeticks(987654),
    "1.3.6.1.2.1.1.5.0": octet("Reception Entrance"),
    "1.3.6.1.4.1.39165.1.1.0": integer(1),
}


class ClientTests(unittest.TestCase):
    def test_get_returns_the_requested_oids(self):
        with FakeAgent(TABLE) as agent:
            values = snmp.get(
                "127.0.0.1", ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.5.0"], port=agent.port
            )
        self.assertEqual(values["1.3.6.1.2.1.1.1.0"], "Hikvision IP Camera DS-2CD2143G0-I")
        self.assertEqual(values["1.3.6.1.2.1.1.5.0"], "Reception Entrance")

    def test_unknown_oid_reports_the_agent_error(self):
        with FakeAgent(TABLE) as agent:
            with self.assertRaises(snmp.SnmpError) as ctx:
                snmp.get("127.0.0.1", ["1.3.6.1.2.1.99.99.0"], port=agent.port)
        self.assertIn("no such name", str(ctx.exception))

    def test_wrong_community_times_out_like_a_real_agent(self):
        with FakeAgent(TABLE, community="secret") as agent:
            with self.assertRaises(snmp.SnmpTimeout):
                snmp.get("127.0.0.1", ["1.3.6.1.2.1.1.1.0"], port=agent.port,
                         timeout=0.4, retries=0)

    def test_walk_lists_a_whole_subtree(self):
        with FakeAgent(TABLE) as agent:
            entries = snmp.walk("127.0.0.1", "1.3.6.1.2.1", port=agent.port)
        self.assertEqual([oid for oid, _ in entries],
                         ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.3.0", "1.3.6.1.2.1.1.5.0"])

    def test_walk_stays_inside_the_requested_subtree(self):
        with FakeAgent(TABLE) as agent:
            entries = snmp.walk("127.0.0.1", "1.3.6.1.4.1", port=agent.port)
        self.assertEqual([oid for oid, _ in entries], ["1.3.6.1.4.1.39165.1.1.0"])

    def test_walk_respects_the_limit(self):
        with FakeAgent(TABLE) as agent:
            entries = snmp.walk("127.0.0.1", "1.3.6.1", port=agent.port, limit=2)
        self.assertEqual(len(entries), 2)

    def test_no_agent_times_out_with_a_clear_message(self):
        with self.assertRaises(snmp.SnmpTimeout):
            # Nothing is listening on this loopback port.
            snmp.get("127.0.0.1", ["1.3.6.1.2.1.1.1.0"], port=9, timeout=0.4, retries=0)


class HealthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.dir.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_snmp_is_not_touched_when_disabled(self):
        with FakeAgent(TABLE) as agent:
            config = make_config(snmp={"enabled": False, "port": agent.port})
            camera = Camera(ip="127.0.0.1", http_port=9, rtsp_port=9)
            result = check_camera(camera, config, True)
            self.assertEqual(agent.requests, [])
        self.assertEqual(result.snmp_values, {})

    def test_configured_oids_are_collected(self):
        with FakeCamera(brand="hikvision", storage="ok") as cam, FakeAgent(TABLE) as agent:
            config = make_config(
                credentials={"default": {"username": "admin", "password": "secret"}},
                snmp={"enabled": True, "port": agent.port,
                      "oids": {"model": "1.3.6.1.2.1.1.1.0", "uptime": "1.3.6.1.2.1.1.3.0"}},
            )
            camera = Camera(ip="127.0.0.1", http_port=cam.port, rtsp_port=cam.port)
            result = check_camera(camera, config, True)
        self.assertEqual(result.snmp_values["model"], "Hikvision IP Camera DS-2CD2143G0-I")
        self.assertEqual(result.snmp_values["uptime"], 987654)

    def test_snmp_can_keep_a_camera_online_when_its_ports_are_shut(self):
        # The web service has crashed but the device is still on the network.
        with FakeAgent(TABLE) as agent:
            config = make_config(
                snmp={"enabled": True, "port": agent.port, "use_for_reachability": True},
            )
            camera = Camera(ip="127.0.0.1", http_port=9, rtsp_port=9)
            result = check_camera(camera, config, True)
        self.assertTrue(result.online)
        self.assertIn("replied to SNMP", result.error)

    def test_reachability_fallback_can_be_turned_off(self):
        with FakeAgent(TABLE) as agent:
            config = make_config(
                snmp={"enabled": True, "port": agent.port, "use_for_reachability": False},
            )
            camera = Camera(ip="127.0.0.1", http_port=9, rtsp_port=9)
            result = check_camera(camera, config, True)
        self.assertFalse(result.online)

    def test_an_unreachable_agent_does_not_break_the_check(self):
        with FakeCamera(brand="hikvision", storage="ok") as cam:
            config = make_config(
                credentials={"default": {"username": "admin", "password": "secret"}},
                snmp={"enabled": True, "port": 9, "timeout": 0.3, "retries": 0},
            )
            camera = Camera(ip="127.0.0.1", http_port=cam.port, rtsp_port=cam.port)
            result = check_camera(camera, config, True)
        self.assertTrue(result.online, "a dead SNMP agent must not hide a working camera")
        self.assertEqual(result.storage.state, StorageState.OK)
        self.assertIn("no reply", result.snmp_error)

    def test_snmp_can_supply_sd_state_when_the_vendor_api_cannot(self):
        # No credentials, so the vendor check returns unknown and SNMP fills in.
        with FakeCamera(brand="hikvision", storage="ok") as cam, FakeAgent(TABLE) as agent:
            config = make_config(
                snmp={"enabled": True, "port": agent.port,
                      "sd_card_oid": "1.3.6.1.4.1.39165.1.1.0",
                      "sd_card_ok_values": ["1"]},
            )
            camera = Camera(ip="127.0.0.1", http_port=cam.port, rtsp_port=cam.port)
            result = check_camera(camera, config, True)
        self.assertEqual(result.storage.state, StorageState.OK)
        self.assertIn("SNMP reported", result.storage.message)

    def test_a_value_outside_the_ok_list_is_a_failure(self):
        with FakeCamera(brand="hikvision", storage="ok") as cam, FakeAgent(TABLE) as agent:
            config = make_config(
                snmp={"enabled": True, "port": agent.port,
                      "sd_card_oid": "1.3.6.1.4.1.39165.1.1.0",
                      "sd_card_ok_values": ["99"]},
            )
            camera = Camera(ip="127.0.0.1", http_port=cam.port, rtsp_port=cam.port)
            result = check_camera(camera, config, True)
        self.assertEqual(result.storage.state, StorageState.FAILED)

    def test_the_vendor_api_wins_over_snmp_when_it_can_answer(self):
        with FakeCamera(brand="hikvision", storage="failed") as cam, FakeAgent(TABLE) as agent:
            config = make_config(
                credentials={"default": {"username": "admin", "password": "secret"}},
                snmp={"enabled": True, "port": agent.port,
                      "sd_card_oid": "1.3.6.1.4.1.39165.1.1.0",
                      "sd_card_ok_values": ["1"]},
            )
            camera = Camera(ip="127.0.0.1", http_port=cam.port, rtsp_port=cam.port)
            result = check_camera(camera, config, True)
        self.assertEqual(result.storage.state, StorageState.FAILED)
        self.assertIn("status 'error'", result.storage.message)


if __name__ == "__main__":
    unittest.main()
