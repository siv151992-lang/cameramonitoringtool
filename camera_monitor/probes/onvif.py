"""ONVIF WS-Discovery: find cameras that announce themselves on the LAN.

Most IP cameras made in the last decade answer a multicast ONVIF Probe on
UDP 3702.  This finds cameras even on addresses you did not think to scan, and
needs no credentials.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import sys
import time
import uuid
from typing import Iterable
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


def _source_address_for(target: str) -> str | None:
    """Which of our addresses the OS would use to reach this target.

    Connecting a UDP socket sends nothing; it just asks the routing table.
    This is the reliable way to find the right interface for a given network.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((target, MULTICAST_PORT))
            return probe.getsockname()[0]
    except OSError:
        return None


def _interface_addresses() -> list[str]:
    """Every IPv4 address on this machine's interfaces (Linux only).

    Needed because a camera server is often multi-homed - one NIC per VLAN -
    and the probe must go out of each of them.
    """
    if sys.platform != "linux":
        return []
    try:
        import fcntl
    except ImportError:
        return []

    SIOCGIFADDR = 0x8915
    found: list[str] = []
    for _, name in socket.if_nameindex():
        if name == "lo":
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                request = struct.pack("256s", name.encode("utf-8")[:15])
                reply = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, request)
                found.append(socket.inet_ntoa(reply[20:24]))
        except (OSError, ValueError):
            continue          # interface has no IPv4 address, or is down
    return found


def _local_addresses(subnets: Iterable[str] | None = None) -> list[str]:
    """Best-effort list of this server's own IPv4 addresses.

    A server with one NIC per VLAN must send the probe from each of them, or
    cameras on the other VLANs never see it.  Three sources, because no single
    one is reliable everywhere:

    1. The interface list, where the platform allows reading it.
    2. The address the OS would use to reach each configured subnet - this is
       what catches the camera VLAN when it is not the default route.
    3. The default route, and the hostname, as a last resort.  On Debian and
       Ubuntu the hostname usually resolves to loopback only, which is why it
       cannot be relied on alone.
    """
    addresses: set[str] = set(_interface_addresses())

    for entry in subnets or []:
        try:
            text = str(entry).strip()
            if "/" in text:
                target = str(next(ipaddress.ip_network(text, strict=False).hosts()))
            elif "-" in text:
                target = text.split("-")[0].strip()
            else:
                target = text
        except (ValueError, StopIteration):
            continue
        source = _source_address_for(target)
        if source:
            addresses.add(source)

    default_route = _source_address_for("8.8.8.8")
    if default_route:
        addresses.add(default_route)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass

    addresses = {address for address in addresses if not address.startswith("127.")}
    return sorted(addresses) or ["0.0.0.0"]


def discover(
    timeout: float = 4.0, subnets: Iterable[str] | None = None
) -> list[dict[str, str]]:
    """Send an ONVIF Probe and collect the replies.

    ``subnets`` are the networks being scanned; they are used to work out which
    of this machine's interfaces faces each one, so a multi-homed server probes
    the camera VLAN and not only its default route.

    Returns one entry per camera: ``{ip, xaddr, types, scopes}``.
    """
    found: dict[str, dict[str, str]] = {}

    for source in _local_addresses(subnets):
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

    # A camera often advertises several service addresses - commonly an IPv4
    # one and an IPv6 link-local one. Prefer IPv4: it is what the rest of the
    # LAN uses, an fe80:: address is not reachable without a scope id anyway,
    # and picking per-camera consistently stops the same device being listed
    # twice when the port sweep also finds it.
    candidates = [urlparse(addr).hostname for addr in xaddrs.split()]
    candidates = [host for host in candidates if host]

    chosen = ""
    for host in candidates:
        if _is_ipv4(host):
            chosen = host
            break
    if not chosen:
        chosen = sender_ip if _is_ipv4(sender_ip) else (candidates[0] if candidates else sender_ip)

    matching_xaddr = next(
        (addr for addr in xaddrs.split() if urlparse(addr).hostname == chosen), ""
    )
    return {"ip": chosen, "xaddr": matching_xaddr, "types": types, "scopes": scopes}


def _is_ipv4(host: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(host).version == 4
    except ValueError:
        return False


def name_from_scopes(scopes: str) -> str:
    """ONVIF scopes carry a friendly name, e.g. ``.../name/Lobby%20Camera``."""
    from urllib.parse import unquote

    for scope in scopes.split():
        if "/name/" in scope:
            return unquote(scope.rsplit("/name/", 1)[-1]).strip()
    return ""
