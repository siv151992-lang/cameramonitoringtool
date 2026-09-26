"""ONVIF WS-Discovery: find cameras that announce themselves on the LAN.

Most IP cameras made in the last decade answer a multicast ONVIF Probe on
UDP 3702.  This finds cameras even on addresses you did not think to scan, and
needs no credentials.
"""

from __future__ import annotations

import socket
import time
import uuid
from urllib.parse import urlparse

from camera_monitor.probes import xmlutil

MULTICAST_GROUP = "239.255.255.250"
MULTICAST_PORT = 3702

PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{message_id}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


def _local_addresses() -> list[str]:
    """Best-effort list of this server's own IPv4 addresses.

    A server with one NIC per VLAN must send the probe from each of them, or
    cameras on the other VLANs never see it.
    """
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass
    try:
        # Reveals the address used for the default route without sending traffic.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    addresses.discard("127.0.0.1")
    return sorted(addresses) or ["0.0.0.0"]


def discover(timeout: float = 4.0) -> list[dict[str, str]]:
    """Send an ONVIF Probe and collect the replies.

    Returns one entry per camera: ``{ip, xaddr, types, scopes}``.
    """
    found: dict[str, dict[str, str]] = {}

    for source in _local_addresses():
        message = PROBE_TEMPLATE.format(message_id=uuid.uuid4()).encode("utf-8")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if source != "0.0.0.0":
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source)
                )
                sock.bind((source, 0))
            sock.settimeout(0.5)
        except OSError:
            continue

        with sock:
            try:
                sock.sendto(message, (MULTICAST_GROUP, MULTICAST_PORT))
            except OSError:
                continue

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                entry = _parse_probe_match(data.decode("utf-8", errors="replace"), addr[0])
                if entry:
                    found.setdefault(entry["ip"], entry)

    return sorted(found.values(), key=lambda item: item["ip"])


def _parse_probe_match(payload: str, sender_ip: str) -> dict[str, str] | None:
    """Pull the service address and scopes out of a ProbeMatch reply."""
    try:
        root = xmlutil.parse(payload)
    except ValueError:
        return None

    xaddrs = ""
    types = ""
    scopes = ""
    for element in root.iter():
        name = xmlutil.local_name(element.tag)
        text = (element.text or "").strip()
        if name == "XAddrs" and text:
            xaddrs = text
        elif name == "Types" and text:
            types = text
        elif name == "Scopes" and text:
            scopes = text

    # Prefer the address the camera advertises; fall back to the UDP sender.
    ip = sender_ip
    first_xaddr = xaddrs.split()[0] if xaddrs else ""
    if first_xaddr:
        host = urlparse(first_xaddr).hostname
        if host:
            ip = host

    return {"ip": ip, "xaddr": first_xaddr, "types": types, "scopes": scopes}


def name_from_scopes(scopes: str) -> str:
    """ONVIF scopes carry a friendly name, e.g. ``.../name/Lobby%20Camera``."""
    from urllib.parse import unquote

    for scope in scopes.split():
        if "/name/" in scope:
            return unquote(scope.rsplit("/name/", 1)[-1]).strip()
    return ""
