"""SD card health for Axis cameras.

Axis reports each disk as an XML attribute set from ``/axis-cgi/disks/list.cgi``.
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

NAME = "axis"
STORAGE_PATH = "/axis-cgi/disks/list.cgi?diskid=all"
DEVICE_INFO_PATH = "/axis-cgi/param.cgi?action=list&group=Brand"

HEALTHY_STATUS = {"ok", "mounted", "connected"}
FAILED_STATUS = {"failed", "disconnected", "error", "full", "unmounted", "nomedia"}


def fetch_storage(
    ip: str, port: int, username: str, password: str, timeout: float
) -> StorageInfo:
    """Ask an Axis camera about its SD card."""
    response = camera_get(ip, port, STORAGE_PATH, username, password, timeout)

    if response.status_code == 401:
        raise ProbeError("authentication failed (check username/password)")
    if response.status_code == 404:
        raise UnsupportedDevice("no Axis disk CGI (not an Axis device)")
    if response.status_code != 200:
        raise ProbeError(f"HTTP {response.status_code} from disks/list.cgi")

    try:
        root = xmlutil.parse(response.text)
    except ValueError as exc:
        raise ProbeError(str(exc)) from exc

    disks: list[dict] = []
    states: list[tuple[StorageState, str]] = []

    for disk in xmlutil.find_all(root, "disk"):
        attrs = disk.attrib
        name = attrs.get("diskid", "SD_DISK")
        status = attrs.get("status", "")
        normalised = status.strip().lower()
        read_only = attrs.get("readonly", "no").strip().lower() == "yes"
        if normalised in FAILED_STATUS:
            state, reason = StorageState.FAILED, f"disk status '{status}'"
        elif normalised in HEALTHY_STATUS:
            if read_only:
                state, reason = StorageState.FAILED, "card is read-only (likely worn out)"
            else:
                state, reason = StorageState.OK, ""
        else:
            state, reason = StorageState.UNKNOWN, f"unrecognised status '{status}'"

        disks.append(
            {
                "name": name,
                "type": "SD",
                "status": status,
                "capacity_mb": _kb_to_mb(attrs.get("totalsize")),
                "free_mb": _kb_to_mb(attrs.get("freesize")),
            }
        )
        states.append((state, f"{name}: {reason}" if reason else ""))

    if not disks:
        return StorageInfo(
            state=StorageState.MISSING,
            message="no SD card detected in the camera",
            brand=NAME,
        )

    for target in (StorageState.FAILED, StorageState.UNKNOWN):
        for state, reason in states:
            if state is target:
                return StorageInfo(state=target, message=reason, brand=NAME, disks=disks)

    first = disks[0]
    detail = ""
    if first["capacity_mb"] and first["free_mb"] is not None:
        detail = f"{first['free_mb']} MB free of {first['capacity_mb']} MB"
    return StorageInfo(state=StorageState.OK, message=detail, brand=NAME, disks=disks)


def fetch_device_info(
    ip: str, port: int, username: str, password: str, timeout: float
) -> dict[str, str]:
    """Read the Brand parameter group."""
    response = camera_get(ip, port, DEVICE_INFO_PATH, username, password, timeout)
    if response.status_code != 200:
        raise ProbeError(f"HTTP {response.status_code} from param.cgi")
    fields = {}
    for line in response.text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip().split(".")[-1]] = value.strip()
    return {
        "brand": NAME,
        "model": fields.get("ProdNbr", ""),
        "serial": "",
        "firmware": "",
        "device_name": fields.get("ProdFullName", ""),
    }


def _kb_to_mb(value: str | None) -> int | None:
    try:
        return int(int(str(value).strip()) / 1024)
    except (TypeError, ValueError):
        return None
