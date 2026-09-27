"""The web dashboard, served from the Python standard library.

Deliberately no web framework: on a monitoring box that may have no internet
access, "pip install" is a liability.  ``http.server`` is enough for a handful
of operators on a LAN.

Reading status is always allowed.  Adding and removing cameras is guarded by
``web.allow_editing`` so a site can keep the dashboard strictly read-only.
"""

from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from camera_monitor.config import Config
from camera_monitor.database import Database
from camera_monitor.health import apply_results, check_camera, summarise
from camera_monitor.inventory import (
    KNOWN_BRANDS,
    Camera,
    InventoryError,
    load_cameras,
    save_cameras,
)

# Serialises read-modify-write of the camera CSV across request threads.
_inventory_lock = threading.Lock()

# A camera list is small; anything larger than this is not a real request.
MAX_BODY_BYTES = 64 * 1024

TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"

CSV_COLUMNS = [
    "ip", "name", "location", "brand", "status", "sd_card", "sd_detail",
    "uptime_24h_percent", "response_ms", "last_seen", "last_checked", "error",
]


def render_dashboard(config: Config) -> bytes:
    """Fill the two placeholders in the HTML template."""
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    site = str(config.get("site.name", "Camera Monitoring"))
    # The site name lands inside a <title> and an <h1>; escape it so a stray
    # '<' in the config cannot break the page.
    safe_site = (
        site.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    html = html.replace("__SITE_NAME__", safe_site)
    html = html.replace("__REFRESH_SECONDS__", str(int(config.get("web.refresh_seconds", 30))))
    editing = "true" if config.get("web.allow_editing", True) else "false"
    html = html.replace("__ALLOW_EDITING__", editing)
    return html.encode("utf-8")


def _port(value: Any, default: int, label: str) -> int:
    """Validate a port from the form.

    The CSV loader silently falls back to a default for a bad port, which is
    right for a bulk file but wrong for a form: a typo should be pointed out,
    not quietly changed.
    """
    if value in (None, ""):
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if not 0 < port < 65536:
        raise ValueError(f"{label} must be between 1 and 65535")
    return port


def status_payload(db: Database) -> dict[str, Any]:
    """Everything the dashboard needs in one request."""
    records = db.all_status()
    uptime = db.uptime_percent(24)
    cameras = []
    last_checked = None
    for record in records:
        cameras.append(
            {
                "ip": record["ip"],
                "name": record["name"],
                "location": record["location"],
                "brand": record["brand"],
                "online": bool(record["online"]),
                "latency_ms": record["latency_ms"],
                "error": record["error"],
                "storage_state": record["storage_state"],
                "storage_message": record["storage_message"],
                "storage_checked_at": record["storage_checked_at"],
                "last_online_at": record["last_online_at"],
                "last_checked_at": record["last_checked_at"],
                "uptime_24h": uptime.get(record["ip"]),
            }
        )
        if record["last_checked_at"] and (
            last_checked is None or record["last_checked_at"] > last_checked
        ):
            last_checked = record["last_checked_at"]

    return {
        "summary": summarise(records),
        "cameras": cameras,
        "last_checked_at": last_checked,
    }


def _persist_detected_brand(config: Config, ip: str, detected: str) -> None:
    """Record the vendor that answered, matching what the CLI does.

    Without this the camera list keeps saying "auto" for a camera we have
    already identified, and every later check re-runs the guessing.
    """
    if not detected or detected in ("unknown", "auto"):
        return
    with _inventory_lock:
        path = config.path_for("inventory.file")
        if not path.exists():
            return
        cameras = load_cameras(path)
        changed = False
        for camera in cameras:
            if camera.ip == ip and camera.brand != detected:
                camera.brand = detected
                changed = True
        if changed:
            save_cameras(path, cameras)


def inventory_payload(config: Config) -> list[dict[str, Any]]:
    """The camera list as the edit dialog needs it.

    Passwords are never sent to the browser; the form reports only whether one
    is set, and an empty password field on save means "leave it alone".
    """
    path = config.path_for("inventory.file")
    if not path.exists():
        return []
    return [
        {
            "ip": camera.ip,
            "name": camera.name,
            "location": camera.location,
            "brand": camera.brand,
            "http_port": camera.http_port,
            "rtsp_port": camera.rtsp_port,
            "username": camera.username,
            "has_password": bool(camera.password),
            "enabled": camera.enabled,
            "notes": camera.notes,
        }
        for camera in load_cameras(path)
    ]


def status_csv(db: Database) -> str:
    """The same data as a spreadsheet-friendly export."""
    payload = status_payload(db)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for camera in payload["cameras"]:
        writer.writerow(
            {
                "ip": camera["ip"],
                "name": camera["name"],
                "location": camera["location"],
                "brand": camera["brand"],
                "status": "online" if camera["online"] else "offline",
                "sd_card": camera["storage_state"],
                "sd_detail": camera["storage_message"],
                "uptime_24h_percent": camera["uptime_24h"] if camera["uptime_24h"] is not None else "",
                "response_ms": camera["latency_ms"] if camera["latency_ms"] is not None else "",
                "last_seen": camera["last_online_at"] or "",
                "last_checked": camera["last_checked_at"] or "",
                "error": camera["error"],
            }
        )
    return buffer.getvalue()


class DashboardHandler(BaseHTTPRequestHandler):
    """Routes for the dashboard.  Everything here is read-only."""

    server_version = "CameraMonitor"
    config: Config
    db: Database
    quiet: bool = True

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.quiet:
            super().log_message(fmt, *args)

    def _authorised(self) -> bool:
        expected_user = str(self.config.get("web.username", "") or "")
        expected_pass = str(self.config.get("web.password", "") or "")
        if not expected_user:
            return True  # authentication not configured
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        username, _, password = decoded.partition(":")
        # compare_digest on both halves so neither can be guessed by timing.
        return hmac.compare_digest(username, expected_user) and hmac.compare_digest(
            password, expected_pass
        )

    def _send(self, body: bytes, content_type: str, status: int = 200, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The dashboard only ever renders its own data; block framing anyway.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send(json.dumps(payload, default=str).encode("utf-8"), "application/json", status)

    # ----------------------------------------------------------- write guard

    @property
    def editing_allowed(self) -> bool:
        return bool(self.config.get("web.allow_editing", True))

    def _same_origin(self) -> bool:
        """Reject cross-site writes.

        A page on another site can make the browser POST here using the
        operator's session.  Browsers always attach Origin to such a request,
        so a mismatch against our own Host is a cross-site attempt.  Requests
        with no Origin at all (curl, the CLI) are allowed through - they carry
        no ambient authority to abuse.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    def _read_json(self) -> dict[str, Any]:
        """Parse the request body, refusing anything oversized or not JSON."""
        # Requiring JSON blocks HTML form posts, which browsers send
        # cross-site without a preflight.
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type != "application/json":
            raise ValueError("expected Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length <= 0:
            raise ValueError("empty request body")
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload

    # -------------------------------------------------------- write handlers

    def _add_camera(self, payload: dict[str, Any]) -> None:
        """Append one camera to the inventory CSV."""
        text = lambda key: str(payload.get(key) or "").strip()

        brand = text("brand").lower() or "auto"
        if brand not in KNOWN_BRANDS:
            self._json({"error": f"unknown brand '{brand}'"}, status=400)
            return

        try:
            http_port = _port(payload.get("http_port"), 80, "web port")
            rtsp_port = _port(payload.get("rtsp_port"), 554, "video port")
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return

        try:
            camera = Camera(
                ip=text("ip"),
                name=text("name"),
                location=text("location"),
                brand=brand,
                http_port=http_port,
                rtsp_port=rtsp_port,
                username=text("username"),
                password=str(payload.get("password") or ""),
                enabled=payload.get("enabled", True) is not False,
                notes=text("notes") or "added from the dashboard",
            )
        except InventoryError as exc:
            self._json({"error": str(exc)}, status=400)
            return

        inventory_path = self.config.path_for("inventory.file")
        # One writer at a time, so two operators adding at once cannot make
        # one of the two entries disappear.
        with _inventory_lock:
            existing = load_cameras(inventory_path) if inventory_path.exists() else []
            if any(item.ip == camera.ip for item in existing):
                self._json(
                    {"error": f"{camera.ip} is already in the camera list"}, status=409
                )
                return
            existing.append(camera)
            save_cameras(inventory_path, existing)

        result: dict[str, Any] = {
            "ok": True,
            "camera": {"ip": camera.ip, "name": camera.name, "location": camera.location},
        }

        # Optionally probe it straight away, so the operator gets the same
        # confirmation the CLI's --check gives.
        if payload.get("check"):
            outcome = check_camera(camera, self.config, True)
            # Record it like any other check, so the new camera appears in the
            # table immediately instead of waiting for the next monitor cycle.
            apply_results(self.db, [outcome], self.config)
            _persist_detected_brand(self.config, camera.ip, outcome.detected_brand)
            result["check"] = {
                "online": outcome.online,
                "latency_ms": outcome.latency_ms,
                "error": outcome.error,
                "storage_state": outcome.storage.state.value if outcome.storage_checked else None,
                "storage_message": outcome.storage.summary if outcome.storage_checked else "",
                "brand": outcome.detected_brand or None,
            }
        else:
            # Nothing has checked it yet, so there is no row to show. Say so
            # rather than leaving the operator wondering where it went.
            result["pending"] = True
        self._json(result)

    def _update_camera(self, payload: dict[str, Any]) -> None:
        """Change an existing camera's details.

        The IP address is the key for status, history and events, so it is not
        editable here - a camera at a new address is a different camera, and
        the operator removes and re-adds it.
        """
        ip = str(payload.get("ip") or "").strip()
        if not ip:
            self._json({"error": "no IP address given"}, status=400)
            return

        text = lambda key: str(payload.get(key) or "").strip()
        brand = text("brand").lower() or "auto"
        if brand not in KNOWN_BRANDS:
            self._json({"error": f"unknown brand '{brand}'"}, status=400)
            return

        try:
            http_port = _port(payload.get("http_port"), 80, "web port")
            rtsp_port = _port(payload.get("rtsp_port"), 554, "video port")
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return

        inventory_path = self.config.path_for("inventory.file")
        with _inventory_lock:
            if not inventory_path.exists():
                self._json({"error": "the camera list does not exist yet"}, status=404)
                return
            cameras = load_cameras(inventory_path)
            current = next((item for item in cameras if item.ip == ip), None)
            if current is None:
                self._json({"error": f"{ip} is not in the camera list"}, status=404)
                return

            # An empty password means "leave it as it is", not "clear it" -
            # the browser is never sent the existing one to put back.
            new_password = str(payload.get("password") or "")
            try:
                updated = Camera(
                    ip=ip,
                    name=text("name"),
                    location=text("location"),
                    brand=brand,
                    http_port=http_port,
                    rtsp_port=rtsp_port,
                    username=text("username"),
                    password=new_password or current.password,
                    enabled=payload.get("enabled", True) is not False,
                    notes=text("notes") or current.notes,
                )
            except InventoryError as exc:
                self._json({"error": str(exc)}, status=400)
                return

            cameras = [updated if item.ip == ip else item for item in cameras]
            save_cameras(inventory_path, cameras)
            self.db.update_identity(ip, updated.name, updated.location, updated.brand)

        result: dict[str, Any] = {
            "ok": True,
            "camera": {"ip": updated.ip, "name": updated.name, "location": updated.location},
        }
        if payload.get("check") and updated.enabled:
            outcome = check_camera(updated, self.config, True)
            apply_results(self.db, [outcome], self.config)
            _persist_detected_brand(self.config, updated.ip, outcome.detected_brand)
            result["check"] = {
                "online": outcome.online,
                "latency_ms": outcome.latency_ms,
                "error": outcome.error,
                "storage_state": outcome.storage.state.value if outcome.storage_checked else None,
                "storage_message": outcome.storage.summary if outcome.storage_checked else "",
                "brand": outcome.detected_brand or None,
            }
        self._json(result)

    def _remove_camera(self, payload: dict[str, Any]) -> None:
        """Delete one camera and forget its recorded status."""
        ip = str(payload.get("ip") or "").strip()
        if not ip:
            self._json({"error": "no IP address given"}, status=400)
            return

        inventory_path = self.config.path_for("inventory.file")
        with _inventory_lock:
            if not inventory_path.exists():
                self._json({"error": "the camera list does not exist yet"}, status=404)
                return
            existing = load_cameras(inventory_path)
            remaining = [item for item in existing if item.ip != ip]
            if len(remaining) == len(existing):
                self._json({"error": f"{ip} is not in the camera list"}, status=404)
                return
            save_cameras(inventory_path, remaining)
            # Keep the dashboard honest: a deleted camera should not linger
            # in the table as permanently offline.
            self.db.remove_missing(item.ip for item in remaining)
        self._json({"ok": True, "removed": ip})

    def do_POST(self) -> None:
        if not self._authorised():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Camera Monitoring"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        route = urlparse(self.path).path.rstrip("/") or "/"
        if route not in ("/api/cameras", "/api/cameras/update", "/api/cameras/remove"):
            self._json({"error": "not found"}, status=404)
            return

        if not self.editing_allowed:
            self._json(
                {"error": "editing is disabled (set web.allow_editing: true in config.yaml)"},
                status=403,
            )
            return
        if not self._same_origin():
            self._json({"error": "cross-site request refused"}, status=403)
            return

        try:
            payload = self._read_json()
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return

        try:
            if route == "/api/cameras":
                self._add_camera(payload)
            elif route == "/api/cameras/update":
                self._update_camera(payload)
            else:
                self._remove_camera(payload)
        except (InventoryError, OSError) as exc:
            self._json({"error": f"could not update the camera list: {exc}"}, status=500)
        except Exception as exc:  # keep the server alive for the next request
            self._json({"error": f"internal error: {exc}"}, status=500)

    # --------------------------------------------------------------- routes

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._authorised():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Camera Monitoring"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        try:
            if route in ("/", "/index.html"):
                self._send(render_dashboard(self.config), "text/html; charset=utf-8")
            elif route == "/api/status":
                self._json(status_payload(self.db))
            elif route == "/api/events":
                params = parse_qs(parsed.query)
                limit = min(int(params.get("limit", ["100"])[0] or 100), 1000)
                ip = params.get("ip", [None])[0]
                if ip and not re.fullmatch(r"[0-9a-fA-F.:]{1,45}", ip):
                    ip = None  # reject anything that is not an IP-shaped string
                self._json({"events": self.db.recent_events(limit=limit, ip=ip)})
            elif route == "/api/summary":
                self._json(summarise(self.db.all_status()))
            elif route == "/export.csv":
                self._send(
                    status_csv(self.db).encode("utf-8"),
                    "text/csv; charset=utf-8",
                    extra={"Content-Disposition": 'attachment; filename="camera-status.csv"'},
                )
            elif route == "/favicon.ico":
                # Browsers always ask; answer quietly rather than log a 404.
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif route == "/healthz":
                self._json({"ok": True})
            elif route == "/api/cameras":
                self._json({"cameras": inventory_payload(self.config)})
            elif route == "/api/config":
                self._json(
                    {
                        "allow_editing": self.editing_allowed,
                        "brands": sorted(KNOWN_BRANDS - {"unknown"}),
                    }
                )
            else:
                self._json({"error": "not found"}, status=404)
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
        except Exception as exc:  # keep the server alive for the next request
            self._json({"error": f"internal error: {exc}"}, status=500)


def create_server(config: Config, db: Database, quiet: bool = True) -> ThreadingHTTPServer:
    """Build (but do not start) the dashboard server."""
    handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"config": config, "db": db, "quiet": quiet},
    )
    host = str(config.get("web.host", "0.0.0.0"))
    port = int(config.get("web.port", 8080))
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
