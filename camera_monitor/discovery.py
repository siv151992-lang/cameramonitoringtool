"""Find cameras on the LAN.

Two independent methods, used together:

1. ONVIF WS-Discovery - cameras announce themselves, no scanning needed.
2. TCP port sweep - connect to camera ports across the configured subnets,
   which also finds older cameras that do not speak ONVIF.
"""

from __future__ import annotations

import ipaddress
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

from camera_monitor.inventory import Camera, ip_sort_key
from camera_monitor.probes import onvif
from camera_monitor.probes.reachability import tcp_probe

# Ports that indicate "this is probably a camera or NVR", in the order we care
# about them.  554 is RTSP, 37777 is the Dahua private port, 8000 is Hikvision's.
CAMERA_PORTS = [80, 554, 8000, 37777, 443, 8080, 8899]


@dataclass
class DiscoveredHost:
    """A host that answered on at least one camera port."""

    ip: str
    open_ports: list[int] = field(default_factory=list)
    source: str = "scan"
    name: str = ""
    brand: str = "auto"


def expand_subnets(subnets: Iterable[str]) -> list[str]:
    """Turn CIDR ranges into individual host addresses.

    Accepts ``192.168.1.0/24``, a plain ``192.168.1.50``, or a
    ``192.168.1.10-192.168.1.60`` range.
    """
    addresses: list[str] = []
    seen: set[str] = set()

    for entry in subnets:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            if "-" in entry:
                start_text, _, end_text = entry.partition("-")
                start = ipaddress.ip_address(start_text.strip())
                end = ipaddress.ip_address(end_text.strip())
                if int(end) < int(start):
                    raise ValueError(f"range end before start: {entry}")
                if int(end) - int(start) > 65535:
                    raise ValueError(f"range too large (max 65536 addresses): {entry}")
                current = start
                while int(current) <= int(end):
                    candidates = [str(current)]
                    current += 1
                    for address in candidates:
                        if address not in seen:
                            seen.add(address)
                            addresses.append(address)
            elif "/" in entry:
                network = ipaddress.ip_network(entry, strict=False)
                if network.num_addresses > 65536:
                    raise ValueError(f"subnet too large (max /16): {entry}")
                hosts = network.hosts() if network.num_addresses > 2 else network
                for host in hosts:
                    address = str(host)
                    if address not in seen:
                        seen.add(address)
                        addresses.append(address)
            else:
                address = str(ipaddress.ip_address(entry))
                if address not in seen:
                    seen.add(address)
                    addresses.append(address)
        except ValueError as exc:
            print(f"  ! skipping '{entry}': {exc}")

    return addresses


def scan_host(ip: str, ports: list[int], timeout: float) -> DiscoveredHost | None:
    """Return the host if any camera port is open, else None."""
    open_ports = [port for port in ports if tcp_probe(ip, port, timeout)[0]]
    return DiscoveredHost(ip=ip, open_ports=open_ports) if open_ports else None


def scan_subnets(
    subnets: Iterable[str],
    ports: list[int] | None = None,
    timeout: float = 1.0,
    workers: int = 256,
    progress: Callable[[int, int], None] | None = None,
) -> list[DiscoveredHost]:
    """Sweep every address in the given subnets for open camera ports."""
    ports = ports or CAMERA_PORTS
    addresses = expand_subnets(subnets)
    if not addresses:
        return []

    results: list[DiscoveredHost] = []
    done = 0
    total = len(addresses)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(scan_host, ip, ports, timeout): ip for ip in addresses}
        for future in as_completed(futures):
            done += 1
            try:
                host = future.result()
            except Exception:  # a single bad address must not stop the sweep
                host = None
            if host:
                results.append(host)
            if progress and (done % 50 == 0 or done == total):
                progress(done, total)

    results.sort(key=lambda host: ip_sort_key(host.ip))
    return results


def discover_all(
    subnets: Iterable[str],
    ports: list[int] | None = None,
    timeout: float = 1.0,
    workers: int = 256,
    use_onvif: bool = True,
    onvif_timeout: float = 4.0,
    progress: Callable[[int, int], None] | None = None,
) -> list[DiscoveredHost]:
    """Run both discovery methods and merge the results by IP address."""
    hosts: dict[str, DiscoveredHost] = {}

    if use_onvif:
        for match in onvif.discover(timeout=onvif_timeout, subnets=subnets):
            hosts[match["ip"]] = DiscoveredHost(
                ip=match["ip"],
                open_ports=[],
                source="onvif",
                name=onvif.name_from_scopes(match.get("scopes", "")),
            )

    for host in scan_subnets(subnets, ports, timeout, workers, progress):
        if host.ip in hosts:
            existing = hosts[host.ip]
            existing.open_ports = host.open_ports
            existing.source = "onvif+scan"
        else:
            hosts[host.ip] = host

    return sorted(hosts.values(), key=lambda host: ip_sort_key(host.ip))


def to_cameras(hosts: list[DiscoveredHost]) -> list[Camera]:
    """Convert discovery results into inventory rows."""
    cameras: list[Camera] = []
    for host in hosts:
        http_port = 80
        for candidate in (80, 8080, 443, 8000):
            if candidate in host.open_ports:
                http_port = candidate
                break
        cameras.append(
            Camera(
                ip=host.ip,
                name=host.name,
                brand=host.brand,
                http_port=http_port,
                rtsp_port=554,
                notes=f"found by {host.source}"
                + (f" (ports {','.join(map(str, host.open_ports))})" if host.open_ports else ""),
            )
        )
    return cameras
