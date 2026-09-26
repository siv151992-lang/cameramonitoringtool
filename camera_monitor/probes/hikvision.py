"""SD card health for Hikvision cameras (and ISAPI-compatible OEM rebadges).

Hikvision exposes storage over ISAPI at ``/ISAPI/ContentMgmt/Storage``, which
returns an ``<hddList>`` describing every card or disk in the device.
"""

from __future__ import annotations

from camera_monitor.probes import xmlutil
from camera_monitor.probes.base import (
    ProbeError,
    StorageInfo,
    StorageState,
    UnsupportedDevice,
    camera_get,
)

NAME = "hikvision"
STORAGE_PATH = "/ISAPI/ContentMgmt/Storage"
DEVICE_INFO_PATH = "/ISAPI/System/deviceInfo"

# Status strings ISAPI reports, grouped by what an operator should do about them.
HEALTHY_STATUS = {"ok", "idle", "sleeping", "normal"}
FAILED_STATUS = {
    "error", "unformatted", "abnormal", "failed", "damaged",
    "mismatch", "offline", "smarterror", "notexist",
}


def _classify(status: str, read_only: bool) -> tuple[StorageState, str]:
    normalised = status.strip().lower().replace(" ", "").replace("_", "")
    if normalised in FAILED_STATUS:
        return StorageState.FAILED, f"SD/disk status '{status}'"
    if normalised in HEALTHY_STATUS:
        if read_only:
            # A card that has gone read-only is wearing out: recording will fail.
            return StorageState.FAILED, "card is read-only (write protected or worn out)"
        return StorageState.OK, ""
    return StorageState.UNKNOWN, f"unrecognised status '{status}'"


def fetch_storage(
    ip: str, port: int, username: str, password: str, timeout: float
) -> StorageInfo:
    """Ask a Hikvision camera about its recording media."""
    response = camera_get(ip, port, STORAGE_PATH, username, password, timeout)

    if response.status_code == 401:
        raise ProbeError("authentication failed (check username/password)")
    if response.status_code == 403:
        raise ProbeError("access forbidden - the account may lack storage permissions")
    if response.status_code == 404:
        raise UnsupportedDevice("no ISAPI storage endpoint (not a Hikvision-style device)")
    if response.status_code != 200:
        raise ProbeError(f"HTTP {response.status_code} from {STORAGE_PATH}")

    try:
        root = xmlutil.parse(response.text)
    except ValueError as exc:
        raise ProbeError(str(exc)) from exc

    disks: list[dict] = []
    states: list[tuple[StorageState, str]] = []

    for hdd in xmlutil.find_all(root, "hdd"):
        fields = xmlutil.children_as_dict(hdd)
        status = fields.get("status", "")
        # 'property' is 'RW' or 'RO' on the firmware versions that report it.
        read_only = fields.get("property", "").upper() == "RO"
        state, reason = _classify(status, read_only)
        name = fields.get("hddName") or fields.get("id") or "hdd"
        disks.append(
            {
                "name": name,
                "type": fields.get("hddType", ""),
                "status": status,
                "capacity_mb": _as_int(fields.get("capacity")),
                "free_mb": _as_int(fields.get("freeSpace")),
            }
        )
        states.append((state, f"{name}: {reason}" if reason else ""))

    if not disks:
        return StorageInfo(
            state=StorageState.MISSING,
            message="no SD card or disk detected in the camera",
            brand=NAME,
        )

    # The worst disk decides the camera's storage state.
    for target in (StorageState.FAILED, StorageState.UNKNOWN):
        for state, reason in states:
            if state is target:
                return StorageInfo(state=target, message=reason, brand=NAME, disks=disks)

    healthy = disks[0]
    detail = ""
    if healthy["capacity_mb"] and healthy["free_mb"] is not None:
        detail = f"{healthy['free_mb']} MB free of {healthy['capacity_mb']} MB"
    return StorageInfo(state=StorageState.OK, message=detail, brand=NAME, disks=disks)


def fetch_device_info(
    ip: str, port: int, username: str, password: str, timeout: float
) -> dict[str, str]:
    """Read model/firmware, used to confirm the brand and enrich the inventory."""
    response = camera_get(ip, port, DEVICE_INFO_PATH, username, password, timeout)
    if response.status_code != 200:
        raise ProbeError(f"HTTP {response.status_code} from {DEVICE_INFO_PATH}")
    root = xmlutil.parse(response.text)
    fields = xmlutil.children_as_dict(root)
    return {
        "brand": NAME,
        "model": fields.get("model", ""),
        "serial": fields.get("serialNumber", ""),
        "firmware": fields.get("firmwareVersion", ""),
        "device_name": fields.get("deviceName", ""),
    }


def _as_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
