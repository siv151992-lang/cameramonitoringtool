"""A stand-in SNMP agent for the tests.

It decodes requests by hand rather than reusing the client's parser, so a bug
that is symmetric in the client's encoder and decoder still shows up here.
"""

from __future__ import annotations

import socket
import threading

from camera_monitor.probes.snmp import (
    PDU_GET_NEXT,
    PDU_RESPONSE,
    TAG_INTEGER,
    TAG_OCTET_STRING,
    TAG_SEQUENCE,
    _read_tlv,
    _tlv,
    decode_oid,
    encode_integer,
    encode_octet_string,
    encode_oid,
)

TAG_TIMETICKS = 0x43


class FakeAgent:
    """Serves a fixed OID table over UDP."""

    def __init__(self, values: dict[str, tuple[int, bytes]], community: str = "public") -> None:
        # values: oid -> (BER tag, raw value bytes)
        self.values = values
        self.community = community
        self.requests: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.3)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    def __enter__(self) -> "FakeAgent":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._running = False
        self._thread.join(timeout=2)
        self._sock.close()

    def _sorted_oids(self) -> list[str]:
        return sorted(self.values, key=lambda oid: tuple(int(p) for p in oid.split(".")))

    def _serve(self) -> None:
        while self._running:
            try:
                data, sender = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reply = self._handle(data)
            except Exception:
                continue
            if reply:
                self._sock.sendto(reply, sender)

    def _handle(self, packet: bytes) -> bytes | None:
        _, body, _ = _read_tlv(packet, 0)
        offset = 0
        _, raw_version, offset = _read_tlv(body, offset)
        _, raw_community, offset = _read_tlv(body, offset)
        if raw_community.decode() != self.community:
            return None  # a real agent stays silent on a bad community
        pdu_tag, pdu, _ = _read_tlv(body, offset)

        pos = 0
        _, raw_id, pos = _read_tlv(pdu, pos)
        _, _, pos = _read_tlv(pdu, pos)
        _, _, pos = _read_tlv(pdu, pos)
        _, bindings_body, _ = _read_tlv(pdu, pos)

        requested: list[str] = []
        cursor = 0
        while cursor < len(bindings_body):
            _, binding, cursor = _read_tlv(bindings_body, cursor)
            _, raw_oid, _ = _read_tlv(binding, 0)
            requested.append(decode_oid(raw_oid))
        self.requests.extend(requested)

        answers: list[tuple[str, int, bytes]] = []
        error_status = 0
        for index, oid in enumerate(requested, start=1):
            if pdu_tag == PDU_GET_NEXT:
                later = [item for item in self._sorted_oids()
                         if tuple(int(p) for p in item.split(".")) > tuple(int(p) for p in oid.split("."))]
                if not later:
                    answers.append((oid, 0x82, b""))   # endOfMibView
                    continue
                target = later[0]
                tag, value = self.values[target]
                answers.append((target, tag, value))
            elif oid in self.values:
                tag, value = self.values[oid]
                answers.append((oid, tag, value))
            else:
                error_status = 2   # noSuchName
                answers.append((oid, 0x05, b""))
                break

        bindings = b"".join(
            _tlv(TAG_SEQUENCE, encode_oid(oid) + _tlv(tag, value))
            for oid, tag, value in answers
        )
        response_pdu = _tlv(
            PDU_RESPONSE,
            _tlv(TAG_INTEGER, raw_id)
            + encode_integer(error_status)
            + encode_integer(1 if error_status else 0)
            + _tlv(TAG_SEQUENCE, bindings),
        )
        return _tlv(
            TAG_SEQUENCE,
            _tlv(TAG_INTEGER, raw_version) + encode_octet_string(self.community) + response_pdu
        )


def octet(text: str) -> tuple[int, bytes]:
    return (TAG_OCTET_STRING, text.encode())


def integer(value: int) -> tuple[int, bytes]:
    size = 1
    while True:
        try:
            return (TAG_INTEGER, value.to_bytes(size, "big", signed=True))
        except OverflowError:
            size += 1


def timeticks(value: int) -> tuple[int, bytes]:
    return (TAG_TIMETICKS, value.to_bytes(4, "big"))
