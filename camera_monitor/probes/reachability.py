"""Is the camera answering on the network?

We use a TCP connect rather than ICMP ping.  Cameras are frequently configured
to drop ping while still serving video, and a TCP connect needs no special
privileges, so the tool can run as an ordinary user.

"Online" means *the camera service answered*, not merely that something exists
at the address.  A host that actively refuses a connection on every camera port
is reported as offline with a note that the address itself responded - the
camera may be rebooting, its web service may have crashed, or the IP may have
been handed to a different device.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass


@dataclass
class Reachability:
    """Outcome of the online/offline check for one camera."""

    online: bool
    latency_ms: float | None = None
    open_port: int | None = None
    error: str = ""
    # True when the address answered at all, even if it refused the connection.
    # Useful for telling "camera crashed" apart from "cable unplugged".
    host_responded: bool = False


def tcp_probe(ip: str, port: int, timeout: float) -> tuple[bool, float | None, str]:
    """Try to open a TCP connection.  Returns (accepted, latency_ms, error)."""
    started = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            latency = (time.perf_counter() - started) * 1000
            return True, round(latency, 1), ""
    except socket.timeout:
        return False, None, "timed out"
    except ConnectionRefusedError:
        latency = (time.perf_counter() - started) * 1000
        return False, round(latency, 1), "connection refused"
    except OSError as exc:
        return False, None, str(exc)


def check_reachable(ip: str, ports: list[int], timeout: float) -> Reachability:
    """A camera is online when one of its ports accepts a connection.

    Ports are tried in order and we stop at the first success, so listing the
    web port first keeps the common case to a single connect.
    """
    errors: list[str] = []
    refused = False

    for port in ports:
        accepted, latency, error = tcp_probe(ip, port, timeout)
        if accepted:
            return Reachability(
                online=True, latency_ms=latency, open_port=port, host_responded=True
            )
        if error == "connection refused":
            refused = True
        errors.append(f"port {port}: {error}")

    detail = "; ".join(errors)
    if refused:
        detail = (
            f"address responded but the camera is not serving on its ports ({detail})"
        )
    return Reachability(online=False, error=detail, host_responded=refused)
