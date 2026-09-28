"""The camera list: a plain CSV file you can edit in Excel.

One row per camera.  ``ip`` is the only required column; everything else has a
sensible default so a minimal file with just IP addresses works fine.
"""

from __future__ import annotations

import csv
import ipaddress
from dataclasses import asdict, dataclass
from pathlib import Path

FIELDNAMES = [
    "ip",
    "name",
    "location",
    "brand",
    "http_port",
    "rtsp_port",
    "username",
    "password",
    "enabled",
    "notes",
]

# Brands the tool knows how to ask about SD card health.  "auto" tries each in
# turn and remembers what answered.
KNOWN_BRANDS = {"auto", "hikvision", "dahua", "axis", "unknown"}


class InventoryError(Exception):
    """Raised when the camera CSV cannot be read."""


def ip_sort_key(ip: str) -> tuple[int, int]:
    """Order IP addresses numerically, mixing IPv4 and IPv6 safely.

    Returns (version, numeric value), so every key is a pair of ints. Sorting
    on per-part tuples looks simpler but breaks the moment one IPv6 address
    turns up beside IPv4 ones - ONVIF discovery readily produces that mix, and
    comparing an int against a str raises TypeError.
    """
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        # Should not happen (Camera validates on construction), but a sort must
        # never be the thing that crashes a scan.
        return (0, 0)
    return (address.version, int(address))


def _to_int(value: str | int | None, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if 0 < parsed < 65536 else default


def _to_bool(value: str | bool | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class Camera:
    """A single camera from the inventory."""

    ip: str
    name: str = ""
    location: str = ""
    brand: str = "auto"
    http_port: int = 80
    rtsp_port: int = 554
    username: str = ""
    password: str = ""
    enabled: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        self.ip = str(self.ip).strip()
        try:
            ipaddress.ip_address(self.ip)
        except ValueError as exc:
            raise InventoryError(f"'{self.ip}' is not a valid IP address") from exc
        slug = self.ip.replace(".", "-").replace(":", "-")
        self.name = (self.name or "").strip() or f"camera-{slug}"
        self.location = (self.location or "").strip()
        self.brand = (self.brand or "auto").strip().lower()
        if self.brand not in KNOWN_BRANDS:
            self.brand = "auto"
        self.http_port = _to_int(self.http_port, 80)
        self.rtsp_port = _to_int(self.rtsp_port, 554)
        self.enabled = _to_bool(self.enabled, True)

    @property
    def sort_key(self) -> tuple[int, int]:
        """Sort by IP numerically rather than as text, so .2 precedes .10."""
        return ip_sort_key(self.ip)

    def to_row(self) -> dict[str, str]:
        row = asdict(self)
        row["enabled"] = "yes" if self.enabled else "no"
        return {key: str(value) for key, value in row.items()}


def load_cameras(path: str | Path) -> list[Camera]:
    """Read the inventory CSV.  Invalid rows are reported, not silently dropped."""
    csv_path = Path(path)
    if not csv_path.exists():
        raise InventoryError(
            f"Camera list not found: {csv_path}\n"
            "Create it with 'python3 camtool.py discover' or copy cameras.example.csv."
        )

    cameras: list[Camera] = []
    seen: set[str] = set()
    problems: list[str] = []

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "ip" not in [f.strip().lower() for f in reader.fieldnames]:
            raise InventoryError(f"{csv_path} must have a header row containing an 'ip' column.")
        for line_number, raw in enumerate(reader, start=2):
            row = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in raw.items()
                if key
            }
            if not row.get("ip") or row["ip"].startswith("#"):
                continue
            try:
                camera = Camera(**{k: v for k, v in row.items() if k in FIELDNAMES})
            except InventoryError as exc:
                problems.append(f"  line {line_number}: {exc}")
                continue
            if camera.ip in seen:
                problems.append(f"  line {line_number}: duplicate IP {camera.ip} (kept the first)")
                continue
            seen.add(camera.ip)
            cameras.append(camera)

    if problems:
        print(f"Skipped {len(problems)} row(s) in {csv_path}:")
        for problem in problems:
            print(problem)

    cameras.sort(key=lambda cam: cam.sort_key)
    return cameras


def save_cameras(path: str | Path, cameras: list[Camera]) -> None:
    """Write the inventory back out, preserving column order."""
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(cameras, key=lambda cam: cam.sort_key)
    # Write to a temp file first so an interrupted run cannot truncate the list.
    temp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for camera in ordered:
            writer.writerow(camera.to_row())
    temp_path.replace(csv_path)


def merge_discovered(
    existing: list[Camera], discovered: list[Camera]
) -> tuple[list[Camera], list[Camera]]:
    """Add newly found cameras to the list without touching existing entries.

    Returns the merged list and just the new cameras, so the caller can report
    what changed.
    """
    by_ip = {camera.ip: camera for camera in existing}
    added: list[Camera] = []
    for camera in discovered:
        if camera.ip in by_ip:
            # Keep the operator's edits (name, location, credentials) intact but
            # fill in a brand if discovery managed to identify one.
            current = by_ip[camera.ip]
            if current.brand in {"auto", "unknown"} and camera.brand not in {"auto", "unknown"}:
                current.brand = camera.brand
            continue
        by_ip[camera.ip] = camera
        added.append(camera)
    merged = sorted(by_ip.values(), key=lambda cam: cam.sort_key)
    return merged, added
