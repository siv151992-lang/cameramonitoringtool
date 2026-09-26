"""Pick the right vendor probe for a camera and run the SD card check.

Cameras rarely advertise their brand in a machine-readable way, so a camera
whose brand is ``auto`` is tried against each supported vendor API in turn.
The brand that answers is returned to the caller, which writes it back into the
inventory - so the guessing happens once per camera, not once per check.
"""

from __future__ import annotations

from camera_monitor.probes import axis, dahua, hikvision
from camera_monitor.probes.base import (
    ProbeError,
    StorageInfo,
    StorageState,
    UnsupportedDevice,
)

# Ordered by how common they are on Indian office installs: Hikvision first,
# then Dahua (which also covers CP Plus and other Dahua OEM rebadges).
VENDORS = {
    hikvision.NAME: hikvision,
    dahua.NAME: dahua,
    axis.NAME: axis,
}
AUTO_ORDER = [hikvision.NAME, dahua.NAME, axis.NAME]


def check_storage(
    ip: str,
    port: int,
    brand: str,
    username: str,
    password: str,
    timeout: float,
) -> tuple[StorageInfo, str]:
    """Return (storage info, brand that answered).

    The returned brand is ``unknown`` when nothing recognised the camera, and
    the caller should leave the inventory alone in that case.
    """
    if not username and not password:
        return (
            StorageInfo(
                state=StorageState.UNKNOWN,
                message="no credentials configured, cannot read SD card status",
                brand=brand,
            ),
            brand,
        )

    if brand in VENDORS:
        module = VENDORS[brand]
        try:
            return module.fetch_storage(ip, port, username, password, timeout), brand
        except UnsupportedDevice:
            # The stored brand is wrong (camera replaced?), so re-detect below.
            pass
        except ProbeError as exc:
            return (
                StorageInfo(state=StorageState.UNKNOWN, message=str(exc), brand=brand),
                brand,
            )

    errors: list[str] = []
    for candidate in AUTO_ORDER:
        module = VENDORS[candidate]
        try:
            return module.fetch_storage(ip, port, username, password, timeout), candidate
        except UnsupportedDevice:
            continue
        except ProbeError as exc:
            message = str(exc)
            # An auth failure means we found the right vendor but the wrong
            # password - that is worth reporting instead of trying other brands.
            if "authentication failed" in message or "forbidden" in message:
                return (
                    StorageInfo(
                        state=StorageState.UNKNOWN, message=message, brand=candidate
                    ),
                    candidate,
                )
            errors.append(f"{candidate}: {message}")

    detail = "; ".join(errors) if errors else "no supported storage API found"
    return (
        StorageInfo(state=StorageState.UNKNOWN, message=detail, brand="unknown"),
        "unknown",
    )


def fetch_device_info(
    ip: str, port: int, brand: str, username: str, password: str, timeout: float
) -> dict[str, str]:
    """Best-effort model/firmware lookup, used by the 'identify' command."""
    order = [brand] if brand in VENDORS else AUTO_ORDER
    for candidate in order:
        try:
            return VENDORS[candidate].fetch_device_info(ip, port, username, password, timeout)
        except ProbeError:
            continue
    return {"brand": "unknown", "model": "", "serial": "", "firmware": "", "device_name": ""}
