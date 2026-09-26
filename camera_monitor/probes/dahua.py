"""SD card health for Dahua cameras and Dahua-based OEMs (CP Plus, Amcrest, Lorex).

Dahua's CGI returns flat ``key=value`` lines rather than XML, e.g.::

    list.info[0].Name=SD0
    list.info[0].State=Active
    list.info[0].Detail[0].Type=Read-Write
    list.info[0].Detail[0].TotalBytes=31254904832
"""

from __future__ import annotations

import re

from camera_monitor.probes.base import (
    ProbeError,
    StorageInfo,
    StorageState,
    UnsupportedDevice,
    camera_get,
)

NAME = "dahua"
STORAGE_PATH = "/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo"
DEVICE_INFO_PATH = "/cgi-bin/magicBox.cgi?action=getSystemInfo"
DEVICE_TYPE_PATH = "/cgi-bin/magicBox.cgi?action=getDeviceType"

HEALTHY_STATE = {"active", "running", "normal", "sleeping"}
FAILED_STATE = {"error", "abnormal", "unformatted", "offline", "failed", "damaged", "nodisk"}

_INDEX = re.compile(r"list\.info\[(\d+)\]\.(.+)")


def parse_kv(text: str) -> dict[str, str]:
    """Turn the CGI's key=value body into a dictionary."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def group_by_device(values: dict[str, str]) -> dict[int, dict[str, str]]:
    """Collect ``list.info[N].*`` keys into one dict per storage device."""
    devices: dict[int, dict[str, str]] = {}
    for key, value in values.items():
        match = _INDEX.match(key)
        if not match:
            continue
        index, field = int(match.group(1)), match.group(2)
        devices.setdefault(index, {})[field] = value
    return devices


def _classify(state: str, media_type: str) -> tuple[StorageState, str]:
    normalised = state.strip().lower().replace(" ", "").replace("_", "")
    if normalised in FAILED_STATE:
        return StorageState.FAILED, f"SD card state '{state}'"
    if normalised in HEALTHY_STATE:
        if media_type.strip().lower().replace("-", "") == "readonly":
            # Dahua flips a dying card to Read-Only; recordings stop silently.
            return StorageState.FAILED, "card has gone read-only (likely worn out)"
        return StorageState.OK, ""
    return StorageState.UNKNOWN, f"unrecognised state '{state}'"


def fetch_storage(
    ip: str, port: int, username: str, password: str, timeout: float
) -> StorageInfo:
    """Ask a Dahua-style camera about its SD card."""
    response = camera_get(ip, port, STORAGE_PATH, username, password, timeout)

    if response.status_code == 401:
        raise ProbeError("authentication failed (check username/password)")
    if response.status_code == 404:
        raise UnsupportedDevice("no Dahua storage CGI (not a Dahua-style device)")
    if response.status_code != 200:
        raise ProbeError(f"HTTP {response.status_code} from storageDevice.cgi")

    body = response.text
    if "Error" in body and "=" not in body:
        raise ProbeError(f"camera returned an error: {body.strip()[:120]}")

    devices = group_by_device(parse_kv(body))
    if not devices:
        return StorageInfo(
            state=StorageState.MISSING,
            message="no SD card detected in the camera",
            brand=NAME,
        )

    disks: list[dict] = []
    states: list[tuple[StorageState, str]] = []

    for index in sorted(devices):
        fields = devices[index]
        name = fields.get("Name", f"SD{index}")
        state_text = fields.get("State", "")
        media_type = fields.get("Detail[0].Type", fields.get("Type", ""))
        state, reason = _classify(state_text, media_type)
        total = _bytes_to_mb(fields.get("Detail[0].TotalBytes"))
        used = _bytes_to_mb(fields.get("Detail[0].UsedBytes"))
        disks.append(
            {
                "name": name,
                "type": media_type,
                "status": state_text,
                "capacity_mb": total,
                "free_mb": (total - used) if (total is not None and used is not None) else None,
            }
        )
        states.append((state, f"{name}: {reason}" if reason else ""))

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
    """Read model/serial from magicBox.cgi."""
    response = camera_get(ip, port, DEVICE_INFO_PATH, username, password, timeout)
    if response.status_code != 200:
        raise ProbeError(f"HTTP {response.status_code} from magicBox.cgi")
    fields = parse_kv(response.text)
    return {
        "brand": NAME,
        "model": fields.get("deviceType", ""),
        "serial": fields.get("serialNumber", ""),
        "firmware": fields.get("version", ""),
        "device_name": "",
    }


def _bytes_to_mb(value: str | None) -> int | None:
    try:
        return int(int(str(value).strip()) / (1024 * 1024))
    except (TypeError, ValueError):
        return None
