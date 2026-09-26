"""Turn status changes into notifications.

Two design choices matter here:

* **One digest per cycle.**  Forty cameras going offline when a switch dies
  should produce one message, not forty.
* **Rate limiting per camera.**  A camera that stays offline for a week is
  reported again every ``alerts.min_repeat_hours``, not every five minutes.
"""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from typing import Any

import requests

from camera_monitor.config import Config
from camera_monitor.database import Database
from camera_monitor.probes.base import StorageState

RECOVERY_KINDS = {"online": "offline", "sd_card_ok": "sd_card"}


def _format_time(value: str | None) -> str:
    """Render a stored UTC timestamp in the server's local time."""
    if not value:
        return "never"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d %b %Y %H:%M")
    except ValueError:
        return value


class AlertManager:
    """Decides what to notify about and sends it."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.enabled = bool(config.get("alerts.enabled", True))
        self.min_repeat_hours = float(config.get("alerts.min_repeat_hours", 6))
        self.site = config.get("site.name", "Camera Monitoring")

    # ------------------------------------------------------------- selection

    def collect(
        self, events: list[dict[str, Any]], status_records: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Choose the lines that belong in this cycle's notification."""
        offline: list[dict[str, Any]] = []
        sd_problems: list[dict[str, Any]] = []
        recovered: list[dict[str, Any]] = []

        # Recoveries are always worth saying, and they reset the rate limiter so
        # that a repeat failure is reported straight away.
        for event in events:
            if event["kind"] in RECOVERY_KINDS:
                self.db.clear_alert(f"{RECOVERY_KINDS[event['kind']]}:{event['ip']}")
                recovered.append(event)

        for record in status_records:
            if not record["online"]:
                if self.db.should_send_alert(
                    f"offline:{record['ip']}", self.min_repeat_hours
                ):
                    offline.append(record)
            if record["storage_state"] in (
                StorageState.FAILED.value,
                StorageState.MISSING.value,
            ):
                if self.db.should_send_alert(
                    f"sd_card:{record['ip']}", self.min_repeat_hours
                ):
                    sd_problems.append(record)

        return {"offline": offline, "sd_problems": sd_problems, "recovered": recovered}

    # -------------------------------------------------------------- message

    def build_message(self, groups: dict[str, list[dict[str, Any]]]) -> tuple[str, str]:
        """Return (subject, body) for the digest."""
        offline = groups["offline"]
        sd_problems = groups["sd_problems"]
        recovered = groups["recovered"]

        parts: list[str] = []
        if offline:
            parts.append(f"{len(offline)} offline")
        if sd_problems:
            parts.append(f"{len(sd_problems)} SD card problem{'s' if len(sd_problems) != 1 else ''}")
        if recovered:
            parts.append(f"{len(recovered)} recovered")
        subject = f"[{self.site}] Camera alert: " + ", ".join(parts)

        lines: list[str] = [subject, ""]

        if offline:
            lines.append(f"OFFLINE ({len(offline)})")
            for record in offline:
                location = f" | {record['location']}" if record.get("location") else ""
                lines.append(
                    f"  {record['ip']:<16} {record['name']}{location}"
                    f" | last seen {_format_time(record.get('last_online_at'))}"
                )
            lines.append("")

        if sd_problems:
            lines.append(f"SD CARD PROBLEMS ({len(sd_problems)})")
            for record in sd_problems:
                location = f" | {record['location']}" if record.get("location") else ""
                lines.append(
                    f"  {record['ip']:<16} {record['name']}{location}"
                    f" | {record['storage_state']}: {record['storage_message']}"
                )
            lines.append("")

        if recovered:
            lines.append(f"RECOVERED ({len(recovered)})")
            for event in recovered:
                lines.append(f"  {event['ip']:<16} {event['name']} | {event['message']}")
            lines.append("")

        lines.append(f"Generated {datetime.now().astimezone().strftime('%d %b %Y %H:%M %Z')}")
        return subject, "\n".join(lines)

    # ------------------------------------------------------------- dispatch

    def notify(
        self, events: list[dict[str, Any]], status_records: list[dict[str, Any]]
    ) -> bool:
        """Collect, format and send.  Returns True if anything was sent."""
        if not self.enabled:
            return False

        groups = self.collect(events, status_records)
        if not any(groups.values()):
            return False

        subject, body = self.build_message(groups)

        if self.config.get("alerts.console", True):
            print("\n" + body + "\n")

        if self.config.get("alerts.email.enabled", False):
            try:
                self.send_email(subject, body)
            except Exception as exc:
                print(f"  ! email alert failed: {exc}")

        if self.config.get("alerts.webhook.enabled", False):
            try:
                self.send_webhook(body)
            except Exception as exc:
                print(f"  ! webhook alert failed: {exc}")

        return True

    def send_email(self, subject: str, body: str) -> None:
        settings = self.config.get("alerts.email", {})
        recipients = settings.get("to_addresses") or []
        if isinstance(recipients, str):
            recipients = [recipients]
        if not settings.get("smtp_host") or not recipients:
            raise ValueError("email alerts need smtp_host and at least one to_address")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.get("from_address") or settings.get("username", "")
        message["To"] = ", ".join(recipients)
        message.set_content(body)

        host = settings["smtp_host"]
        port = int(settings.get("smtp_port", 587))
        timeout = 20

        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        with server:
            server.ehlo()
            if port != 465 and settings.get("use_tls", True):
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if settings.get("username"):
                server.login(settings["username"], settings.get("password", ""))
            server.send_message(message)

    def send_webhook(self, body: str) -> None:
        """POST the digest to a chat webhook (Zoho Cliq, Slack, Teams, ...)."""
        settings = self.config.get("alerts.webhook", {})
        url = settings.get("url")
        if not url:
            raise ValueError("webhook alerts need a url")
        field = settings.get("message_field", "text")
        response = requests.post(url, json={field: body}, timeout=15)
        if response.status_code >= 400:
            raise ValueError(f"webhook returned HTTP {response.status_code}")
