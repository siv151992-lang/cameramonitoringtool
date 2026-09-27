"""Run the online/offline and SD card checks across the whole camera list.

With 1000 cameras a serial scan would take far longer than the check interval,
so checks run in a thread pool.  Each check is network-bound, not CPU-bound,
which is exactly what threads are good at.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from camera_monitor.config import Config
from camera_monitor.database import Database, utc_now
from camera_monitor.inventory import Camera
from camera_monitor.probes import sdcard, snmp
from camera_monitor.probes.base import StorageInfo, StorageState
from camera_monitor.probes.reachability import check_reachable


@dataclass
class CheckResult:
    """What one camera reported during one cycle."""

    camera: Camera
    online: bool
    latency_ms: float | None = None
    error: str = ""
    storage: StorageInfo = field(default_factory=StorageInfo)
    storage_checked: bool = False
    detected_brand: str = ""
    # Readings collected over SNMP, when it is enabled and answering.
    snmp_values: dict[str, Any] = field(default_factory=dict)
    snmp_error: str = ""


def check_camera(
    camera: Camera,
    config: Config,
    with_storage: bool,
) -> CheckResult:
    """Check a single camera: reachability first, then storage if it is up."""
    tcp_timeout = float(config.get("checks.tcp_timeout", 2.0))
    http_timeout = float(config.get("checks.http_timeout", 6.0))

    # Web port first: it is the one we need for the storage check anyway.
    ports = [camera.http_port, camera.rtsp_port]
    ports = list(dict.fromkeys(port for port in ports if port))
    reach = check_reachable(camera.ip, ports, tcp_timeout)

    result = CheckResult(
        camera=camera,
        online=reach.online,
        latency_ms=reach.latency_ms,
        error=reach.error,
    )

    snmp_enabled = bool(config.get("snmp.enabled", False))
    if snmp_enabled:
        _collect_snmp(result, camera, config, tried_because_offline=not reach.online)

    if not result.online or not with_storage:
        return result

    username = camera.username or config.credentials_for(camera.ip)[0]
    password = camera.password or config.credentials_for(camera.ip)[1]

    try:
        info, detected = sdcard.check_storage(
            camera.ip, camera.http_port, camera.brand, username, password, http_timeout
        )
    except Exception as exc:  # never let one camera abort the cycle
        info = StorageInfo(
            state=StorageState.UNKNOWN, message=f"check failed: {exc}", brand=camera.brand
        )
        detected = camera.brand

    result.storage = info
    result.storage_checked = True
    result.detected_brand = detected

    if info.state is StorageState.UNKNOWN and snmp_enabled:
        _storage_from_snmp(result, config)
    return result


def _collect_snmp(
    result: CheckResult, camera: Camera, config: Config, tried_because_offline: bool
) -> None:
    """Read the configured OIDs, and optionally use SNMP as a liveness signal."""
    community = str(config.get("snmp.community", "public"))
    version = str(config.get("snmp.version", "2c"))
    port = int(config.get("snmp.port", 161))
    timeout = float(config.get("snmp.timeout", 2.0))
    retries = int(config.get("snmp.retries", 1))

    named_oids: dict[str, str] = dict(config.get("snmp.oids", {}) or {})
    sd_oid = str(config.get("snmp.sd_card_oid", "") or "")
    if sd_oid:
        named_oids.setdefault("_sd_card", sd_oid)
    # Always ask for uptime, so there is something to prove the agent is alive
    # even when no OIDs have been configured yet.
    if not named_oids:
        named_oids["uptime"] = snmp.SYS_UPTIME

    by_oid = {oid: name for name, oid in named_oids.items()}
    try:
        answers = snmp.get(
            camera.ip, list(by_oid), community, version, port, timeout, retries
        )
    except snmp.SnmpError as exc:
        result.snmp_error = str(exc)
        return

    result.snmp_values = {by_oid.get(oid, oid): value for oid, value in answers.items()}

    # The agent answered, so the device is on the network even if its camera
    # ports are not listening.
    if tried_because_offline and config.get("snmp.use_for_reachability", True):
        result.online = True
        result.error = (
            "camera ports did not answer, but the device replied to SNMP"
            + (f" ({result.error})" if result.error else "")
        )


def _storage_from_snmp(result: CheckResult, config: Config) -> None:
    """Map an SNMP reading onto an SD card state, if one is configured."""
    if "_sd_card" not in result.snmp_values:
        return
    raw = result.snmp_values["_sd_card"]
    ok_values = {
        str(value).strip().lower()
        for value in (config.get("snmp.sd_card_ok_values", []) or [])
    }
    if not ok_values:
        return
    healthy = str(raw).strip().lower() in ok_values
    result.storage = StorageInfo(
        state=StorageState.OK if healthy else StorageState.FAILED,
        message=f"SNMP reported '{raw}'",
        brand=result.storage.brand,
    )
    result.storage_checked = True


def run_checks(
    cameras: list[Camera],
    config: Config,
    with_storage: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> list[CheckResult]:
    """Check every enabled camera, in parallel."""
    active = [camera for camera in cameras if camera.enabled]
    if not active:
        return []

    workers = max(1, int(config.get("checks.workers", 100)))
    results: list[CheckResult] = []
    done = 0
    total = len(active)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(check_camera, camera, config, with_storage): camera
            for camera in active
        }
        for future in as_completed(futures):
            done += 1
            camera = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    CheckResult(camera=camera, online=False, error=f"internal error: {exc}")
                )
            if progress and (done % 25 == 0 or done == total):
                progress(done, total)

    results.sort(key=lambda item: item.camera.sort_key)
    return results


def apply_results(
    db: Database, results: list[CheckResult], config: Config
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persist results and work out what changed.

    Returns (status records written, transition events).  A camera is only
    declared offline after ``checks.offline_after_failures`` consecutive failed
    checks, so a single dropped packet does not raise an alert.
    """
    threshold = max(1, int(config.get("checks.offline_after_failures", 2)))
    now = utc_now()

    # Read every previous status in one query: with 1000 cameras, a lookup per
    # camera would mean 1000 round trips through the connection lock.
    previous_by_ip = {record["ip"]: record for record in db.all_status()}

    status_records: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    history: list[tuple[str, str, bool, float | None]] = []

    for result in results:
        camera = result.camera
        previous = previous_by_ip.get(camera.ip)

        # --- online / offline, with flap suppression -----------------------
        if result.online:
            failures = 0
            confirmed_online = True
        else:
            failures = int(previous["consecutive_failures"]) + 1 if previous else 1
            if previous is None:
                # First ever check: believe it immediately, nothing to debounce.
                confirmed_online = False
            elif failures >= threshold:
                confirmed_online = False
            else:
                confirmed_online = bool(previous["online"])

        was_online = bool(previous["online"]) if previous else None
        changed = was_online is not None and was_online != confirmed_online

        if changed and not confirmed_online:
            events.append(
                {
                    "ts": now, "ip": camera.ip, "name": camera.name,
                    "location": camera.location, "kind": "offline", "severity": "critical",
                    "message": result.error or "camera stopped responding",
                }
            )
        elif changed and confirmed_online:
            events.append(
                {
                    "ts": now, "ip": camera.ip, "name": camera.name,
                    "location": camera.location, "kind": "online", "severity": "info",
                    "message": "camera is responding again",
                }
            )
        elif previous is None and not confirmed_online:
            events.append(
                {
                    "ts": now, "ip": camera.ip, "name": camera.name,
                    "location": camera.location, "kind": "offline", "severity": "critical",
                    "message": result.error or "camera did not respond to the first check",
                }
            )

        # --- SD card -------------------------------------------------------
        if result.storage_checked:
            storage_state = result.storage.state.value
            storage_message = result.storage.summary
            storage_checked_at = now
        elif previous:
            # Not checked this cycle: keep what we knew before.
            storage_state = previous["storage_state"]
            storage_message = previous["storage_message"]
            storage_checked_at = previous["storage_checked_at"]
        else:
            storage_state = StorageState.UNKNOWN.value
            storage_message = "not checked yet"
            storage_checked_at = None

        previous_storage = previous["storage_state"] if previous else None
        if result.storage_checked and previous_storage != storage_state:
            if result.storage.state.is_problem:
                events.append(
                    {
                        "ts": now, "ip": camera.ip, "name": camera.name,
                        "location": camera.location, "kind": "sd_card",
                        "severity": "critical",
                        "message": f"SD card {storage_state}: {storage_message}",
                    }
                )
            elif (
                previous_storage in (StorageState.FAILED.value, StorageState.MISSING.value)
                and result.storage.state is StorageState.OK
            ):
                events.append(
                    {
                        "ts": now, "ip": camera.ip, "name": camera.name,
                        "location": camera.location, "kind": "sd_card_ok",
                        "severity": "info", "message": "SD card is healthy again",
                    }
                )

        last_change_at = now if changed else (previous["last_change_at"] if previous else now)
        last_online_at = (
            now if confirmed_online else (previous["last_online_at"] if previous else None)
        )

        status_records.append(
            {
                "ip": camera.ip,
                "name": camera.name,
                "location": camera.location,
                "brand": result.detected_brand or camera.brand,
                "online": 1 if confirmed_online else 0,
                "consecutive_failures": failures,
                "latency_ms": result.latency_ms,
                "error": result.error,
                "storage_state": storage_state,
                "storage_message": storage_message,
                "storage_checked_at": storage_checked_at,
                "last_checked_at": now,
                "last_online_at": last_online_at,
                "last_change_at": last_change_at,
            }
        )
        history.append((now, camera.ip, confirmed_online, result.latency_ms))

    db.upsert_status(status_records)
    db.add_history(history)
    db.add_events(events)
    return status_records, events


def summarise(status_records: list[dict[str, Any]]) -> dict[str, int]:
    """Counts for the dashboard tiles and the console report."""
    total = len(status_records)
    online = sum(1 for record in status_records if record["online"])
    sd_failed = sum(
        1
        for record in status_records
        if record["storage_state"] in (StorageState.FAILED.value, StorageState.MISSING.value)
    )
    sd_unknown = sum(
        1 for record in status_records if record["storage_state"] == StorageState.UNKNOWN.value
    )
    return {
        "total": total,
        "online": online,
        "offline": total - online,
        "sd_failed": sd_failed,
        "sd_ok": total - sd_failed - sd_unknown,
        "sd_unknown": sd_unknown,
    }
