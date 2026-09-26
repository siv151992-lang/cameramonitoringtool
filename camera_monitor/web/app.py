"""A small read-only dashboard served from the Python standard library.

Deliberately no web framework: on a monitoring box that may have no internet
access, "pip install" is a liability.  ``http.server`` is enough for a handful
of operators on a LAN.
"""

from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from camera_monitor.config import Config
from camera_monitor.database import Database
from camera_monitor.health import summarise

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
    return html.encode("utf-8")


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
