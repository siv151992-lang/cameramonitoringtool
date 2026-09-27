#!/usr/bin/env python3
"""Camera monitoring tool - command line entry point.

Run ``python3 camtool.py --help`` to see every command.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Allow running the script directly from a checkout without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera_monitor import __version__, discovery
from camera_monitor.alerts import AlertManager
from camera_monitor.config import Config, ConfigError, load_config
from camera_monitor.database import Database
from camera_monitor.health import apply_results, run_checks, summarise
from camera_monitor.inventory import (
    KNOWN_BRANDS,
    Camera,
    InventoryError,
    load_cameras,
    merge_discovered,
    save_cameras,
)
from camera_monitor.probes import sdcard, snmp

STOP = threading.Event()


# --------------------------------------------------------------- formatting

def print_progress(done: int, total: int) -> None:
    percent = (done / total * 100) if total else 100
    print(f"\r  {done}/{total} ({percent:.0f}%)", end="", flush=True)
    if done >= total:
        print()


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    """Print an aligned table without pulling in a dependency."""
    if not rows:
        print("  (nothing to show)")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print("  " + line)
    print("  " + "  ".join("-" * width for width in widths))
    for row in rows:
        print("  " + "  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))


def print_summary(counts: dict[str, int]) -> None:
    print()
    print(f"  Cameras           {counts['total']}")
    print(f"  Online            {counts['online']}")
    print(f"  Offline           {counts['offline']}")
    print(f"  SD card healthy   {counts['sd_ok']}")
    print(f"  SD card problems  {counts['sd_failed']}")
    print(f"  SD card unknown   {counts['sd_unknown']}")
    print()


def open_database(config: Config) -> Database:
    return Database(config.path_for("database.file"))


def read_inventory(config: Config) -> list[Camera]:
    return load_cameras(config.path_for("inventory.file"))


# ---------------------------------------------------------------- commands

def cmd_discover(args: argparse.Namespace, config: Config) -> int:
    """Sweep the LAN and add anything new to the camera list."""
    subnets = args.subnet or config.get("discovery.subnets", [])
    if not subnets:
        print(
            "No subnets to scan.\n"
            "Either add them under 'discovery.subnets' in config.yaml, or pass\n"
            "  python3 camtool.py discover --subnet 192.168.1.0/24"
        )
        return 1

    ports = config.get("discovery.ports")
    timeout = float(args.timeout or config.get("discovery.timeout", 1.0))
    workers = int(config.get("discovery.workers", 256))
    use_onvif = config.get("discovery.onvif_probe", True) and not args.no_onvif

    print(f"Scanning {', '.join(str(subnet) for subnet in subnets)} ...")
    if use_onvif:
        print("  (also listening for ONVIF announcements)")

    hosts = discovery.discover_all(
        subnets=subnets,
        ports=ports,
        timeout=timeout,
        workers=workers,
        use_onvif=use_onvif,
        progress=print_progress,
    )
    print(f"\nFound {len(hosts)} device(s) answering on camera ports.")
    if not hosts:
        return 0

    found = discovery.to_cameras(hosts)
    inventory_path = config.path_for("inventory.file")

    try:
        existing = load_cameras(inventory_path) if inventory_path.exists() else []
    except InventoryError as exc:
        print(f"Could not read the existing camera list: {exc}")
        return 1

    merged, added = merge_discovered(existing, found)

    print_table(
        [
            [
                host.ip,
                ",".join(str(port) for port in host.open_ports) or "-",
                host.source,
                host.name or "-",
                "NEW" if any(camera.ip == host.ip for camera in added) else "known",
            ]
            for host in hosts
        ],
        ["IP address", "Open ports", "Found by", "ONVIF name", "In list?"],
    )

    if args.dry_run:
        print(f"\nDry run: {len(added)} new camera(s) would be added to {inventory_path}.")
        return 0

    save_cameras(inventory_path, merged)
    print(f"\n{len(added)} new camera(s) added. {inventory_path} now holds {len(merged)}.")
    if added:
        print("Open that file and fill in the name/location columns so alerts are readable.")
    return 0


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    """Check every camera once and report."""
    cameras = read_inventory(config)
    if not cameras:
        print("The camera list is empty. Run 'discover' first.")
        return 1

    with_storage = config.get("checks.storage_check", True) and not args.no_storage
    print(f"Checking {len([c for c in cameras if c.enabled])} camera(s) ...")

    started = time.monotonic()
    results = run_checks(cameras, config, with_storage=with_storage, progress=print_progress)
    elapsed = time.monotonic() - started

    with open_database(config) as db:
        db.remove_missing(camera.ip for camera in cameras)
        records, events = apply_results(db, results, config)
        counts = summarise(records)

        _write_back_detected_brands(config, cameras, results)

        print(f"Completed in {elapsed:.1f}s.")
        print_summary(counts)

        problems = [
            record
            for record in records
            if not record["online"] or record["storage_state"] in ("failed", "missing")
        ]
        display = records if args.all else problems
        if display:
            print("All cameras:" if args.all else "Cameras needing attention:")
            print_table(
                [
                    [
                        record["ip"],
                        record["name"][:28],
                        record["location"][:18] or "-",
                        "online" if record["online"] else "OFFLINE",
                        record["storage_state"],
                        (record["storage_message"] or "")[:44],
                    ]
                    for record in display
                ],
                ["IP address", "Name", "Location", "Status", "SD card", "Detail"],
            )
        else:
            print("Every camera is online with a healthy SD card.")

        if not args.no_alerts:
            AlertManager(config, db).notify(events, records)

    return 0


def _write_back_detected_brands(
    config: Config, cameras: list[Camera], results: list[Any]
) -> None:
    """Remember which vendor API answered, so later checks skip the guessing."""
    by_ip = {camera.ip: camera for camera in cameras}
    changed = False
    for result in results:
        detected = getattr(result, "detected_brand", "")
        camera = by_ip.get(result.camera.ip)
        if not camera or not detected or detected in ("unknown", camera.brand):
            continue
        camera.brand = detected
        changed = True
    if changed:
        try:
            save_cameras(config.path_for("inventory.file"), list(by_ip.values()))
        except OSError as exc:
            print(f"  ! could not update the camera list with detected brands: {exc}")


def cmd_monitor(args: argparse.Namespace, config: Config) -> int:
    """Check on a loop, alerting when something changes."""
    interval = int(args.interval or config.get("checks.interval_seconds", 300))
    storage_every = max(1, int(config.get("checks.storage_every_n_cycles", 1)))
    retention = int(config.get("checks.history_retention_days", 30))

    db = open_database(config)
    alerts = AlertManager(config, db)
    server = None

    if args.serve:
        from camera_monitor.web.app import create_server

        server = create_server(config, db)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address[0], server.server_address[1]
        shown = "localhost" if host in ("0.0.0.0", "::") else host
        print(f"Dashboard: http://{shown}:{port}/")

    print(f"Monitoring every {interval}s. Press Ctrl+C to stop.")
    cycle = 0
    try:
        while not STOP.is_set():
            cycle += 1
            with_storage = config.get("checks.storage_check", True) and (
                cycle == 1 or cycle % storage_every == 0
            )
            try:
                cameras = read_inventory(config)  # re-read so edits apply live
            except InventoryError as exc:
                print(f"  ! {exc}")
                STOP.wait(interval)
                continue

            started = time.monotonic()
            results = run_checks(cameras, config, with_storage=with_storage)
            db.remove_missing(camera.ip for camera in cameras)
            records, events = apply_results(db, results, config)
            _write_back_detected_brands(config, cameras, results)
            counts = summarise(records)
            elapsed = time.monotonic() - started

            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"[{stamp}] cycle {cycle}: {counts['online']}/{counts['total']} online, "
                f"{counts['sd_failed']} SD problem(s), {elapsed:.1f}s"
                + ("" if with_storage else " (reachability only)")
            )

            alerts.notify(events, records)

            if cycle % 100 == 0:
                removed = db.prune(retention)
                if removed:
                    print(f"  pruned {removed} old history/event row(s)")

            # Subtract the time already spent so the period stays steady.
            STOP.wait(max(5, interval - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
        db.close()
    print("\nStopped.")
    return 0


def cmd_serve(args: argparse.Namespace, config: Config) -> int:
    """Run only the dashboard, against whatever the last scan recorded."""
    from camera_monitor.web.app import create_server

    db = open_database(config)
    if args.port:
        config.data.setdefault("web", {})["port"] = args.port
    server = create_server(config, db, quiet=not args.verbose)
    host, port = server.server_address[0], server.server_address[1]
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    print(f"Dashboard on http://{shown}:{port}/  (Ctrl+C to stop)")
    if config.get("web.username"):
        print("Basic authentication is enabled.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        db.close()
    return 0


def _read_ip_list(path: str) -> list[dict[str, str]]:
    """Read a plain list of cameras for bulk adding.

    One camera per line: ``ip``, ``ip,name`` or ``ip,name,location``.
    Blank lines and lines starting with # are ignored, so a list exported
    from an NVR can usually be used as-is.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        entries.append(
            {
                "ip": parts[0],
                "name": parts[1] if len(parts) > 1 else "",
                "location": parts[2] if len(parts) > 2 else "",
            }
        )
    return entries


def cmd_add(args: argparse.Namespace, config: Config) -> int:
    """Add cameras to the list by hand, without running discovery."""
    inventory_path = config.path_for("inventory.file")
    existing = load_cameras(inventory_path) if inventory_path.exists() else []
    by_ip = {camera.ip: camera for camera in existing}

    # Name and location come from the file when bulk adding, and from the
    # command line for a single camera. Everything else applies to all of them.
    requested: list[dict[str, str]] = []
    if args.from_file:
        try:
            requested.extend(_read_ip_list(args.from_file))
        except OSError as exc:
            print(f"Could not read {args.from_file}: {exc}")
            return 1
    if args.ip:
        requested.append(
            {"ip": args.ip, "name": args.name or "", "location": args.location or ""}
        )
    if not requested:
        print(
            "Nothing to add.\n"
            "  python3 camtool.py add 10.10.12.64 --name 'Reception' --location 'Lobby'\n"
            "  python3 camtool.py add --from-file camera-ips.txt"
        )
        return 1

    added: list[Camera] = []
    updated: list[Camera] = []
    skipped: list[str] = []
    invalid: list[str] = []

    for entry in requested:
        try:
            camera = Camera(
                ip=entry["ip"],
                name=entry.get("name", ""),
                location=entry.get("location", ""),
                brand=args.brand or "auto",
                http_port=args.http_port or 80,
                rtsp_port=args.rtsp_port or 554,
                username=args.username or "",
                password=args.password or "",
                enabled=not args.disabled,
                notes=args.notes or "added manually",
            )
        except InventoryError as exc:
            invalid.append(f"{entry['ip']}: {exc}")
            continue

        if camera.ip in by_ip:
            if not args.update:
                skipped.append(camera.ip)
                continue
            updated.append(camera)
        else:
            added.append(camera)
        by_ip[camera.ip] = camera

    for problem in invalid:
        print(f"  ! skipped {problem}")
    if skipped:
        print(
            f"  ! {len(skipped)} already in the list, left untouched "
            f"({', '.join(skipped[:5])}{', ...' if len(skipped) > 5 else ''})."
        )
        print("    Use --update to overwrite them.")

    if added or updated:
        save_cameras(inventory_path, list(by_ip.values()))
        print(
            f"Added {len(added)}, updated {len(updated)}. "
            f"{inventory_path} now holds {len(by_ip)} camera(s)."
        )
    else:
        print("No changes made.")
        return 1 if invalid else 0

    if args.check:
        targets = [camera for camera in added + updated if camera.enabled]
        if not targets:
            print("\nNothing to check (the camera was added as disabled).")
            return 0
        print(f"\nChecking {len(targets)} camera(s) ...")
        results = run_checks(targets, config, with_storage=True)
        print_table(
            [
                [
                    result.camera.ip,
                    result.camera.name[:24],
                    "online" if result.online else "OFFLINE",
                    result.storage.state.value if result.storage_checked else "-",
                    (result.storage.summary if result.storage_checked else result.error)[:46],
                ]
                for result in results
            ],
            ["IP address", "Name", "Status", "SD card", "Detail"],
        )
        if any(not result.online for result in results):
            print(
                "\nA camera reported offline here is not reachable from this machine.\n"
                "Check the IP and web port, and that this server is on the same network."
            )
    return 0


def cmd_remove(args: argparse.Namespace, config: Config) -> int:
    """Remove cameras from the list, and drop their recorded status."""
    inventory_path = config.path_for("inventory.file")
    cameras = load_cameras(inventory_path)
    targets = set(args.ip)

    remaining = [camera for camera in cameras if camera.ip not in targets]
    removed = [camera for camera in cameras if camera.ip in targets]
    missing = targets - {camera.ip for camera in cameras}

    for ip in sorted(missing):
        print(f"  ! {ip} is not in the camera list")
    if not removed:
        return 1

    save_cameras(inventory_path, remaining)
    for camera in removed:
        print(f"  removed {camera.ip}  {camera.name}")
    # Keep the dashboard in step: drop the status rows for deleted cameras.
    with open_database(config) as db:
        db.remove_missing(camera.ip for camera in remaining)
    print(f"\n{len(removed)} removed. {inventory_path} now holds {len(remaining)} camera(s).")
    return 0


def cmd_list(args: argparse.Namespace, config: Config) -> int:
    """Print the inventory."""
    cameras = read_inventory(config)
    print_table(
        [
            [
                camera.ip,
                camera.name,
                camera.location or "-",
                camera.brand,
                str(camera.http_port),
                "yes" if camera.enabled else "no",
                "set" if (camera.username or camera.password) else "from config",
            ]
            for camera in cameras
        ],
        ["IP address", "Name", "Location", "Brand", "Web port", "Enabled", "Credentials"],
    )
    print(f"\n{len(cameras)} camera(s) in {config.path_for('inventory.file')}")
    return 0


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    """Show the last recorded status without checking anything."""
    with open_database(config) as db:
        records = db.all_status()
        if not records:
            print("No results recorded yet. Run 'scan' first.")
            return 1
        uptime = db.uptime_percent(24)
        counts = summarise(records)
        print_summary(counts)

        selected = records
        if args.offline_only:
            selected = [record for record in records if not record["online"]]
        if args.sd_only:
            selected = [
                record for record in records if record["storage_state"] in ("failed", "missing")
            ]

        print_table(
            [
                [
                    record["ip"],
                    record["name"][:28],
                    record["location"][:18] or "-",
                    "online" if record["online"] else "OFFLINE",
                    record["storage_state"],
                    f"{uptime.get(record['ip'], 0):.1f}%" if record["ip"] in uptime else "-",
                    (record["storage_message"] or record["error"] or "")[:40],
                ]
                for record in selected
            ],
            ["IP address", "Name", "Location", "Status", "SD card", "Uptime 24h", "Detail"],
        )

        if args.csv:
            from camera_monitor.web.app import status_csv

            Path(args.csv).write_text(status_csv(db), encoding="utf-8")
            print(f"\nWritten to {args.csv}")

        if args.events:
            print("\nRecent events:")
            print_table(
                [
                    [event["ts"][:19].replace("T", " "), event["ip"], event["kind"],
                     event["message"][:50]]
                    for event in db.recent_events(limit=args.events)
                ],
                ["When (UTC)", "IP address", "Event", "Detail"],
            )
    return 0


def cmd_identify(args: argparse.Namespace, config: Config) -> int:
    """Ask cameras for their model and firmware, and record the brand."""
    cameras = read_inventory(config)
    if args.ip:
        cameras = [camera for camera in cameras if camera.ip in set(args.ip)]
        if not cameras:
            print("None of those IP addresses are in the camera list.")
            return 1

    timeout = float(config.get("checks.http_timeout", 6.0))
    rows: list[list[str]] = []
    changed = False

    for camera in cameras:
        username = camera.username or config.credentials_for(camera.ip)[0]
        password = camera.password or config.credentials_for(camera.ip)[1]
        info = sdcard.fetch_device_info(
            camera.ip, camera.http_port, camera.brand, username, password, timeout
        )
        rows.append(
            [
                camera.ip,
                camera.name[:24],
                info["brand"],
                info["model"] or "-",
                info["firmware"] or "-",
                info["serial"][:20] or "-",
            ]
        )
        if info["brand"] != "unknown" and camera.brand != info["brand"]:
            camera.brand = info["brand"]
            changed = True

    print_table(rows, ["IP address", "Name", "Brand", "Model", "Firmware", "Serial"])
    if changed and not args.ip:
        save_cameras(config.path_for("inventory.file"), cameras)
        print("\nBrand column updated in the camera list.")
    return 0


def cmd_snmp(args: argparse.Namespace, config: Config) -> int:
    """Read OIDs from a camera, or list everything it exposes.

    This is the answer to "I do not have the vendor MIB file": a MIB only puts
    names to numbers, and the agent itself will tell you which numbers it
    serves.
    """
    community = args.community or str(config.get("snmp.community", "public"))
    version = args.version or str(config.get("snmp.version", "2c"))
    port = args.port or int(config.get("snmp.port", 161))
    timeout = args.timeout or float(config.get("snmp.timeout", 2.0))
    retries = int(config.get("snmp.retries", 1))

    if args.walk:
        root = args.walk if isinstance(args.walk, str) else "1.3.6.1.2.1"
        print(f"Walking {args.ip} from {root} (community '{community}', SNMP v{version}) ...")
        try:
            entries = snmp.walk(
                args.ip, root, community, version, port, timeout, retries, limit=args.limit
            )
        except snmp.SnmpError as exc:
            print(f"\nSNMP failed: {exc}")
            print(_snmp_hint(exc))
            return 1
        if not entries:
            print("\nThe agent answered but returned nothing under that OID.")
            return 1
        print_table(
            [[oid, str(value)[:70]] for oid, value in entries],
            ["OID", "Value"],
        )
        print(f"\n{len(entries)} value(s).", end=" ")
        if len(entries) >= args.limit:
            print(f"Stopped at the --limit of {args.limit}; raise it to see more.")
        else:
            print("Copy the ones you want into 'snmp.oids' in config.yaml.")
        return 0

    named = dict(config.get("snmp.oids", {}) or {})
    if args.oid:
        named = {oid: oid for oid in args.oid}
    if not named:
        named = {
            "description": snmp.SYS_DESCR,
            "name": snmp.SYS_NAME,
            "uptime": snmp.SYS_UPTIME,
            "object id": snmp.SYS_OBJECT_ID,
        }

    by_oid = {oid: label for label, oid in named.items()}
    try:
        answers = snmp.get(args.ip, list(by_oid), community, version, port, timeout, retries)
    except snmp.SnmpError as exc:
        print(f"SNMP failed: {exc}")
        print(_snmp_hint(exc))
        return 1

    rows = []
    for oid, value in answers.items():
        label = by_oid.get(oid, "")
        shown = snmp.format_uptime(value) if oid == snmp.SYS_UPTIME else str(value)
        rows.append([label, oid, shown[:60]])
    print_table(rows, ["Name", "OID", "Value"])
    return 0


def _snmp_hint(error: Exception) -> str:
    """Point at the usual causes rather than leaving a bare error."""
    text = str(error).lower()
    if "no reply" in text:
        return (
            "  No reply usually means one of:\n"
            "    - SNMP is not enabled on the camera (Configuration > Network >\n"
            "      Advanced Settings > SNMP in its web interface)\n"
            "    - the community string is wrong (try the one set on the camera)\n"
            "    - UDP port 161 is blocked, or this machine cannot reach the camera"
        )
    if "no such name" in text:
        return "  That OID does not exist on this device. Use --walk to see what does."
    return ""


def cmd_test_alert(args: argparse.Namespace, config: Config) -> int:
    """Send a sample notification to prove the email/webhook settings work."""
    with open_database(config) as db:
        manager = AlertManager(config, db)
        subject = f"[{manager.site}] Test alert"
        body = (
            f"{subject}\n\n"
            "This is a test message from the camera monitoring tool.\n"
            "If you can read this, alert delivery is configured correctly.\n"
        )
        print(body)
        failures = 0
        if config.get("alerts.email.enabled", False):
            try:
                manager.send_email(subject, body)
                print("Email sent.")
            except Exception as exc:
                print(f"Email FAILED: {exc}")
                failures += 1
        else:
            print("Email alerts are disabled in config.yaml.")
        if config.get("alerts.webhook.enabled", False):
            try:
                manager.send_webhook(body)
                print("Webhook sent.")
            except Exception as exc:
                print(f"Webhook FAILED: {exc}")
                failures += 1
        else:
            print("Webhook alerts are disabled in config.yaml.")
    return 1 if failures else 0


# -------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camtool",
        description="Monitor LAN-connected IP cameras: online/offline and SD card health.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical first run:\n"
            "  python3 camtool.py discover --subnet 192.168.1.0/24\n"
            "  python3 camtool.py scan\n"
            "  python3 camtool.py monitor --serve\n"
        ),
    )
    parser.add_argument("--config", "-c", help="path to config.yaml")
    parser.add_argument("--version", action="version", version=f"camtool {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="find cameras on the LAN and add them to the list")
    discover.add_argument("--subnet", "-s", action="append", help="e.g. 192.168.1.0/24 (repeatable)")
    discover.add_argument("--timeout", type=float, help="per-port connect timeout in seconds")
    discover.add_argument("--no-onvif", action="store_true", help="skip the ONVIF announcement probe")
    discover.add_argument("--dry-run", action="store_true", help="show findings without saving")
    discover.set_defaults(func=cmd_discover)

    scan = sub.add_parser("scan", help="check every camera once")
    scan.add_argument("--all", action="store_true", help="list every camera, not just problems")
    scan.add_argument("--no-storage", action="store_true", help="skip the SD card check")
    scan.add_argument("--no-alerts", action="store_true", help="check without sending notifications")
    scan.set_defaults(func=cmd_scan)

    monitor = sub.add_parser("monitor", help="check continuously and alert on changes")
    monitor.add_argument("--interval", type=int, help="seconds between cycles")
    monitor.add_argument("--serve", action="store_true", help="also run the web dashboard")
    monitor.set_defaults(func=cmd_monitor)

    serve = sub.add_parser("serve", help="run only the web dashboard")
    serve.add_argument("--port", type=int, help="override the configured port")
    serve.add_argument("--verbose", action="store_true", help="log every HTTP request")
    serve.set_defaults(func=cmd_serve)

    add = sub.add_parser(
        "add",
        help="add a camera to the list by hand",
        description="Add cameras without running discovery - useful when the cameras "
                    "are on a network the server cannot sweep, or when you already "
                    "have a list of addresses.",
        epilog="Examples:\n"
               "  python3 camtool.py add 10.10.12.64 --name 'Reception' --location 'Lobby'\n"
               "  python3 camtool.py add 10.10.12.64 --check\n"
               "  python3 camtool.py add --from-file camera-ips.txt\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add.add_argument("ip", nargs="?", help="the camera's IP address")
    add.add_argument("--name", "-n", help="friendly name, e.g. 'Reception Entrance'")
    add.add_argument("--location", "-l", help="floor, block or room")
    add.add_argument("--brand", choices=sorted(KNOWN_BRANDS), help="default: auto-detect")
    add.add_argument("--http-port", type=int, help="web port (default 80)")
    add.add_argument("--rtsp-port", type=int, help="video port (default 554)")
    add.add_argument("--username", help="only if this camera differs from the config default")
    add.add_argument("--password", help="as above; prefer the config file to keep it out of shell history")
    add.add_argument("--notes", help="free text")
    add.add_argument("--disabled", action="store_true", help="add it but do not check it yet")
    add.add_argument("--update", action="store_true", help="overwrite an entry that already exists")
    add.add_argument(
        "--from-file",
        metavar="FILE",
        help="bulk add from a text file: one 'ip', 'ip,name' or 'ip,name,location' per line",
    )
    add.add_argument("--check", action="store_true", help="check the camera right after adding it")
    add.set_defaults(func=cmd_add)

    remove = sub.add_parser("remove", help="remove cameras from the list")
    remove.add_argument("ip", nargs="+", help="one or more IP addresses")
    remove.set_defaults(func=cmd_remove)

    listing = sub.add_parser("list", help="print the camera inventory")
    listing.set_defaults(func=cmd_list)

    report = sub.add_parser("report", help="show the last recorded status")
    report.add_argument("--offline-only", action="store_true")
    report.add_argument("--sd-only", action="store_true", help="only cameras with SD card problems")
    report.add_argument("--csv", help="also write the report to this CSV file")
    report.add_argument("--events", type=int, metavar="N", help="also show the last N events")
    report.set_defaults(func=cmd_report)

    identify = sub.add_parser("identify", help="read model/firmware and record each camera's brand")
    identify.add_argument("--ip", action="append", help="limit to these IPs (repeatable)")
    identify.set_defaults(func=cmd_identify)

    snmp_cmd = sub.add_parser(
        "snmp",
        help="read SNMP values from a camera, or list everything it exposes",
        description="Query a camera over SNMP. No vendor MIB file is needed: a MIB "
                    "only puts names to numeric OIDs, and --walk asks the camera "
                    "itself which OIDs it serves.",
        epilog="Examples:\n"
               "  python3 camtool.py snmp 10.10.12.64\n"
               "  python3 camtool.py snmp 10.10.12.64 --walk\n"
               "  python3 camtool.py snmp 10.10.12.64 --walk 1.3.6.1.4.1\n"
               "  python3 camtool.py snmp 10.10.12.64 --oid 1.3.6.1.2.1.1.3.0\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    snmp_cmd.add_argument("ip", help="the camera's IP address")
    snmp_cmd.add_argument(
        "--walk", nargs="?", const="1.3.6.1.2.1", metavar="OID",
        help="list every OID under this one (default: the standard MIB-II tree). "
             "Use 1.3.6.1.4.1 for the vendor's private tree.",
    )
    snmp_cmd.add_argument("--oid", action="append", help="read one OID (repeatable)")
    snmp_cmd.add_argument("--community", help="default: from config.yaml, else 'public'")
    snmp_cmd.add_argument("--version", choices=["1", "2c"], help="default: 2c")
    snmp_cmd.add_argument("--port", type=int, help="default: 161")
    snmp_cmd.add_argument("--timeout", type=float, help="seconds to wait for a reply")
    snmp_cmd.add_argument("--limit", type=int, default=200, help="max values from a walk")
    snmp_cmd.set_defaults(func=cmd_snmp)

    test = sub.add_parser("test-alert", help="send a test email/webhook notification")
    test.set_defaults(func=cmd_test_alert)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def handle_signal(signum: int, frame: Any) -> None:
        STOP.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration problem:\n{exc}")
        return 2

    try:
        return args.func(args, config)
    except (InventoryError, ConfigError) as exc:
        print(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
