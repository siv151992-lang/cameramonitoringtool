"""SQLite storage for current status, history and the alert log.

One small file on disk, no database server to install.  All writes happen in
batches from the main thread, so a single connection guarded by a lock is
enough even though the checks themselves run in parallel.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS status (
    ip                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL DEFAULT '',
    location             TEXT NOT NULL DEFAULT '',
    brand                TEXT NOT NULL DEFAULT 'auto',
    online               INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    latency_ms           REAL,
    error                TEXT NOT NULL DEFAULT '',
    storage_state        TEXT NOT NULL DEFAULT 'unknown',
    storage_message      TEXT NOT NULL DEFAULT '',
    storage_checked_at   TEXT,
    last_checked_at      TEXT,
    last_online_at       TEXT,
    last_change_at       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    ip       TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_ip ON events (ip);

CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    ip         TEXT NOT NULL,
    online     INTEGER NOT NULL,
    latency_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_history_ip_ts ON history (ip, ts DESC);

CREATE TABLE IF NOT EXISTS alert_log (
    alert_key    TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    """Current time as an ISO 8601 string with an explicit UTC offset."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper around the SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets the web dashboard read while a scan is writing.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- status

    def get_status(self, ip: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM status WHERE ip = ?", (ip,)).fetchone()
        return dict(row) if row else None

    def all_status(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM status").fetchall()
        statuses = [dict(row) for row in rows]
        statuses.sort(key=_ip_sort_key)
        return statuses

    def upsert_status(self, records: Iterable[dict[str, Any]]) -> None:
        """Write the latest status for a batch of cameras in one transaction."""
        columns = [
            "ip", "name", "location", "brand", "online", "consecutive_failures",
            "latency_ms", "error", "storage_state", "storage_message",
            "storage_checked_at", "last_checked_at", "last_online_at", "last_change_at",
        ]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{col}=excluded.{col}" for col in columns if col != "ip")
        sql = (
            f"INSERT INTO status ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(ip) DO UPDATE SET {updates}"
        )
        rows = [tuple(record.get(col) for col in columns) for record in records]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def update_identity(self, ip: str, name: str, location: str, brand: str) -> bool:
        """Rename a camera in the status table without waiting for a check.

        Status rows carry the name and location so the dashboard can show them,
        so an edit has to reach here too or the table keeps the old label until
        the next cycle.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE status SET name = ?, location = ?, brand = ? WHERE ip = ?",
                (name, location, brand, ip),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def remove_missing(self, keep_ips: Iterable[str]) -> int:
        """Drop status rows for cameras no longer in the inventory CSV."""
        keep = set(keep_ips)
        with self._lock:
            existing = {row["ip"] for row in self._conn.execute("SELECT ip FROM status")}
            stale = existing - keep
            if stale:
                self._conn.executemany(
                    "DELETE FROM status WHERE ip = ?", [(ip,) for ip in stale]
                )
                self._conn.commit()
        return len(stale)

    # ---------------------------------------------------------------- events

    def add_events(self, events: Iterable[dict[str, Any]]) -> None:
        rows = [
            (
                event.get("ts") or utc_now(),
                event["ip"],
                event.get("name", ""),
                event.get("location", ""),
                event["kind"],
                event.get("severity", "info"),
                event.get("message", ""),
            )
            for event in events
        ]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO events (ts, ip, name, location, kind, severity, message)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def recent_events(self, limit: int = 200, ip: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if ip:
            query += " WHERE ip = ?"
            params.append(ip)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- history

    def add_history(self, samples: Iterable[tuple[str, str, bool, float | None]]) -> None:
        """Append (ts, ip, online, latency_ms) rows."""
        rows = [(ts, ip, 1 if online else 0, latency) for ts, ip, online, latency in samples]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO history (ts, ip, online, latency_ms) VALUES (?, ?, ?, ?)", rows
            )
            self._conn.commit()

    def uptime_percent(self, hours: int = 24) -> dict[str, float]:
        """Percentage of checks in the window where each camera was online."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._conn.execute(
                "SELECT ip, AVG(online) * 100.0 AS pct FROM history"
                " WHERE ts >= ? GROUP BY ip",
                (since,),
            ).fetchall()
        return {row["ip"]: round(row["pct"], 1) for row in rows}

    def prune(self, retention_days: int = 30) -> int:
        """Delete history and events older than the retention window."""
        if retention_days <= 0:
            return 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat(timespec="seconds")
        with self._lock:
            deleted = self._conn.execute("DELETE FROM history WHERE ts < ?", (cutoff,)).rowcount
            deleted += self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            self._conn.commit()
        return deleted

    # ------------------------------------------------------------ alert log

    def should_send_alert(self, alert_key: str, min_repeat_hours: float) -> bool:
        """Rate-limit repeated alerts about the same problem on the same camera.

        Without this, 40 offline cameras would send 40 emails every cycle.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            row = self._conn.execute(
                "SELECT last_sent_at FROM alert_log WHERE alert_key = ?", (alert_key,)
            ).fetchone()
            if row:
                try:
                    last_sent = datetime.fromisoformat(row["last_sent_at"])
                except ValueError:
                    last_sent = None
                if last_sent and now - last_sent < timedelta(hours=min_repeat_hours):
                    return False
            self._conn.execute(
                "INSERT INTO alert_log (alert_key, last_sent_at) VALUES (?, ?)"
                " ON CONFLICT(alert_key) DO UPDATE SET last_sent_at = excluded.last_sent_at",
                (alert_key, now.isoformat(timespec="seconds")),
            )
            self._conn.commit()
        return True

    def clear_alert(self, alert_key: str) -> None:
        """Forget an alert so that a recurrence is reported immediately."""
        with self._lock:
            self._conn.execute("DELETE FROM alert_log WHERE alert_key = ?", (alert_key,))
            self._conn.commit()


def _ip_sort_key(record: dict[str, Any]) -> tuple:
    ip = record.get("ip", "")
    try:
        return tuple(int(part) for part in ip.split("."))
    except ValueError:
        return (0, 0, 0, 0)
