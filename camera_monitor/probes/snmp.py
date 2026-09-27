"""A small SNMP v1/v2c client, implemented directly on UDP.

Why not a library: the tool's whole dependency list is ``requests`` and
``PyYAML``, and a monitoring box often has no internet access to install more.
SNMP GET and GETNEXT are a short, stable, well-specified slice of the protocol,
so they are implemented here rather than pulled in.

Only the pieces a monitoring poll needs are supported: GetRequest, GetNextRequest
and the value types cameras actually return.  No SET, no v3, no traps.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Any, Iterable

# --- BER tags --------------------------------------------------------------
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30

TAG_IP_ADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_OPAQUE = 0x44
TAG_COUNTER64 = 0x46

# Exception markers a v2c agent returns instead of a value.
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82

PDU_GET = 0xA0
PDU_GET_NEXT = 0xA1
PDU_RESPONSE = 0xA2

VERSIONS = {"1": 0, "2c": 1, "v1": 0, "v2c": 1}

# Standard MIB-II values every SNMP agent serves.  Useful defaults, and the
# reason a camera needs no vendor MIB to be polled for liveness.
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"

ERROR_STATUS = {
    0: "",
    1: "response too big",
    2: "no such name (the OID does not exist on this device)",
    3: "bad value",
    4: "read only",
    5: "general error",
    6: "access denied",
}


class SnmpError(Exception):
    """The agent could not be reached, or answered with an error."""


class SnmpTimeout(SnmpError):
    """No reply within the timeout - usually unreachable, or wrong community."""


# --- encoding --------------------------------------------------------------

def _length(size: int) -> bytes:
    """BER length: short form below 128, long form above."""
    if size < 0x80:
        return bytes([size])
    body = b""
    while size:
        body = bytes([size & 0xFF]) + body
        size >>= 8
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _length(len(value)) + value


def _int_bytes(value: int) -> bytes:
    """Smallest two's-complement encoding that holds the value."""
    size = 1
    while True:
        try:
            return value.to_bytes(size, "big", signed=True)
        except OverflowError:
            size += 1


def encode_integer(value: int) -> bytes:
    return _tlv(TAG_INTEGER, _int_bytes(value))


def encode_octet_string(value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _tlv(TAG_OCTET_STRING, value)


def _base128(value: int) -> bytes:
    """One OID subidentifier: base-128, high group first, continuation bit set
    on every byte but the last."""
    chunk = bytearray([value & 0x7F])
    value >>= 7
    while value:
        chunk.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(chunk))


def encode_oid(oid: str) -> bytes:
    """Dotted OID to BER.  '1.3.6.1.2.1.1.1.0' -> 06 08 2b 06 01 02 01 01 01 00"""
    text = oid.strip().lstrip(".")
    if not text:
        raise ValueError("empty OID")
    try:
        arcs = [int(part) for part in text.split(".")]
    except ValueError as exc:
        raise ValueError(f"'{oid}' is not a numeric OID") from exc
    if len(arcs) < 2:
        raise ValueError(f"'{oid}' is too short to be an OID")
    if any(arc < 0 for arc in arcs):
        raise ValueError(f"'{oid}' has a negative arc")
    if arcs[0] > 2 or (arcs[0] < 2 and arcs[1] > 39):
        raise ValueError(f"'{oid}' does not start with a valid arc pair")

    # The first two arcs share one subidentifier. Under arc 2 the second arc is
    # unbounded, so that combined value can exceed one byte and needs the same
    # base-128 encoding as every other arc.
    body = bytearray(_base128(arcs[0] * 40 + arcs[1]))
    for arc in arcs[2:]:
        body.extend(_base128(arc))
    return _tlv(TAG_OID, bytes(body))


def build_request(
    oids: Iterable[str], community: str, version: str = "2c",
    request_id: int | None = None, pdu_type: int = PDU_GET,
) -> tuple[bytes, int]:
    """Build a GetRequest (or GetNextRequest).  Returns (packet, request_id)."""
    if version not in VERSIONS:
        raise ValueError(f"unsupported SNMP version '{version}' (use 1 or 2c)")
    # Random id so a late reply to a previous poll is not mistaken for this one.
    request_id = request_id if request_id is not None else int.from_bytes(os.urandom(3), "big")

    bindings = b"".join(
        _tlv(TAG_SEQUENCE, encode_oid(oid) + _tlv(TAG_NULL, b"")) for oid in oids
    )
    pdu = _tlv(
        pdu_type,
        encode_integer(request_id)
        + encode_integer(0)              # error-status
        + encode_integer(0)              # error-index
        + _tlv(TAG_SEQUENCE, bindings),
    )
    message = _tlv(
        TAG_SEQUENCE,
        encode_integer(VERSIONS[version]) + encode_octet_string(community) + pdu,
    )
    return message, request_id


# --- decoding --------------------------------------------------------------

def _read_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read one tag/length/value.  Returns (tag, value, next offset)."""
    if offset >= len(data):
        raise SnmpError("truncated response")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise SnmpError("truncated response (no length)")
    first = data[offset]
    offset += 1
    if first < 0x80:
        size = first
    else:
        count = first & 0x7F
        if count == 0 or offset + count > len(data):
            raise SnmpError("bad length field in response")
        size = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    if offset + size > len(data):
        raise SnmpError("response shorter than its declared length")
    return tag, data[offset:offset + size], offset + size


def decode_oid(value: bytes) -> str:
    if not value:
        raise SnmpError("empty OID in response")

    # Read the base-128 subidentifiers first; splitting the leading one comes
    # after, because it can span several bytes just like any other.
    subids: list[int] = []
    current = 0
    for byte in value:
        current = (current << 7) | (byte & 0x7F)
        if not byte & 0x80:
            subids.append(current)
            current = 0
    if current or not subids:
        raise SnmpError("truncated OID in response")

    head = subids[0]
    if head < 40:
        arcs = [0, head]
    elif head < 80:
        arcs = [1, head - 40]
    else:
        # Arc 2 has no upper bound on its second element.
        arcs = [2, head - 80]
    arcs.extend(subids[1:])
    return ".".join(str(arc) for arc in arcs)


def decode_value(tag: int, value: bytes) -> Any:
    """Turn a BER value into something printable."""
    if tag == TAG_INTEGER:
        return int.from_bytes(value, "big", signed=True) if value else 0
    if tag in (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS, TAG_COUNTER64):
        return int.from_bytes(value, "big") if value else 0
    if tag == TAG_OCTET_STRING:
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
        # Some agents pad strings with control characters.
        return text.strip("\x00").strip()
    if tag == TAG_OID:
        return decode_oid(value)
    if tag == TAG_IP_ADDRESS and len(value) == 4:
        return ".".join(str(byte) for byte in value)
    if tag == TAG_NULL:
        return None
    if tag == TAG_NO_SUCH_OBJECT:
        return "(no such object)"
    if tag == TAG_NO_SUCH_INSTANCE:
        return "(no such instance)"
    if tag == TAG_END_OF_MIB_VIEW:
        return "(end of MIB)"
    if tag == TAG_OPAQUE:
        return value.hex()
    return value.hex()


@dataclass
class SnmpReply:
    """The variable bindings from one response, in the order they arrived."""

    request_id: int
    bindings: list[tuple[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.bindings)


def parse_response(packet: bytes, expect_request_id: int | None = None) -> SnmpReply:
    """Decode a Response PDU into its variable bindings."""
    tag, body, _ = _read_tlv(packet, 0)
    if tag != TAG_SEQUENCE:
        raise SnmpError("reply is not an SNMP message")

    offset = 0
    tag, value, offset = _read_tlv(body, offset)     # version
    if tag != TAG_INTEGER:
        raise SnmpError("malformed SNMP version field")
    tag, value, offset = _read_tlv(body, offset)     # community
    if tag != TAG_OCTET_STRING:
        raise SnmpError("malformed community field")

    tag, pdu, offset = _read_tlv(body, offset)
    if tag != PDU_RESPONSE:
        raise SnmpError(f"expected a Response PDU, got tag 0x{tag:02x}")

    pos = 0
    _, raw_id, pos = _read_tlv(pdu, pos)
    request_id = int.from_bytes(raw_id, "big", signed=True) if raw_id else 0
    _, raw_status, pos = _read_tlv(pdu, pos)
    error_status = int.from_bytes(raw_status, "big", signed=True) if raw_status else 0
    _, raw_index, pos = _read_tlv(pdu, pos)
    error_index = int.from_bytes(raw_index, "big", signed=True) if raw_index else 0

    if expect_request_id is not None and request_id != expect_request_id:
        raise SnmpError("reply belongs to a different request")
    if error_status:
        detail = ERROR_STATUS.get(error_status, f"error {error_status}")
        raise SnmpError(f"agent reported: {detail} (at variable {error_index})")

    tag, bindings_body, _ = _read_tlv(pdu, pos)
    if tag != TAG_SEQUENCE:
        raise SnmpError("malformed variable bindings")

    bindings: list[tuple[str, Any]] = []
    cursor = 0
    while cursor < len(bindings_body):
        tag, binding, cursor = _read_tlv(bindings_body, cursor)
        if tag != TAG_SEQUENCE:
            raise SnmpError("malformed variable binding")
        inner = 0
        tag, raw_oid, inner = _read_tlv(binding, inner)
        if tag != TAG_OID:
            raise SnmpError("variable binding does not start with an OID")
        tag, raw_value, inner = _read_tlv(binding, inner)
        bindings.append((decode_oid(raw_oid), decode_value(tag, raw_value)))

    return SnmpReply(request_id=request_id, bindings=bindings)


# --- the network bit -------------------------------------------------------

def _exchange(
    ip: str, port: int, packet: bytes, request_id: int, timeout: float, retries: int
) -> SnmpReply:
    """Send one datagram and wait for its reply, retrying on timeout."""
    last_error: Exception = SnmpTimeout("no reply")
    for _ in range(max(1, retries + 1)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(packet, (ip, port))
                # Ignore stray datagrams from other sources or older requests.
                deadline_attempts = 3
                for _ in range(deadline_attempts):
                    data, sender = sock.recvfrom(65535)
                    if sender[0] != ip:
                        continue
                    return parse_response(data, expect_request_id=request_id)
                raise SnmpTimeout("only unrelated replies arrived")
        except socket.timeout:
            last_error = SnmpTimeout(f"no reply within {timeout}s")
        except OSError as exc:
            raise SnmpError(f"network error: {exc}") from exc
        except SnmpError as exc:
            # A protocol-level error will not improve on a retry.
            raise exc
    raise last_error


def get(
    ip: str,
    oids: list[str],
    community: str = "public",
    version: str = "2c",
    port: int = 161,
    timeout: float = 2.0,
    retries: int = 1,
) -> dict[str, Any]:
    """Fetch one or more OIDs.  Returns {oid: value} in the agent's order."""
    if not oids:
        return {}
    packet, request_id = build_request(oids, community, version, pdu_type=PDU_GET)
    return _exchange(ip, port, packet, request_id, timeout, retries).as_dict()


def get_next(
    ip: str,
    oid: str,
    community: str = "public",
    version: str = "2c",
    port: int = 161,
    timeout: float = 2.0,
    retries: int = 1,
) -> tuple[str, Any] | None:
    """One GETNEXT step.  Returns (oid, value), or None at the end of the MIB."""
    packet, request_id = build_request([oid], community, version, pdu_type=PDU_GET_NEXT)
    reply = _exchange(ip, port, packet, request_id, timeout, retries)
    if not reply.bindings:
        return None
    next_oid, value = reply.bindings[0]
    if value == "(end of MIB)":
        return None
    return next_oid, value


def _oid_tuple(oid: str) -> tuple[int, ...]:
    return tuple(int(part) for part in oid.strip().lstrip(".").split("."))


def walk(
    ip: str,
    root: str = "1.3.6.1.2.1",
    community: str = "public",
    version: str = "2c",
    port: int = 161,
    timeout: float = 2.0,
    retries: int = 1,
    limit: int = 500,
) -> list[tuple[str, Any]]:
    """Walk a subtree.

    This is how you find out what a camera exposes without owning its MIB file:
    the agent itself lists every OID it serves.
    """
    prefix = _oid_tuple(root)
    results: list[tuple[str, Any]] = []
    current = root
    seen: set[str] = set()

    while len(results) < limit:
        step = get_next(ip, current, community, version, port, timeout, retries)
        if step is None:
            break
        next_oid, value = step
        # A non-advancing agent would otherwise spin forever.
        if next_oid in seen or _oid_tuple(next_oid)[: len(prefix)] != prefix:
            break
        seen.add(next_oid)
        results.append((next_oid, value))
        current = next_oid

    return results


def format_uptime(ticks: Any) -> str:
    """TimeTicks are hundredths of a second; show something readable."""
    try:
        total = int(ticks) // 100
    except (TypeError, ValueError):
        return str(ticks)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"
