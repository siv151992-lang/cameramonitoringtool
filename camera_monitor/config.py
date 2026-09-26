"""Load and validate the tool's YAML configuration.

The config file is plain YAML.  Any string value may reference an environment
variable using ``${NAME}`` or ``${NAME:-fallback}`` so that passwords never
have to be written into the file itself.
"""

from __future__ import annotations

import copy
import ipaddress
import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Every setting the tool understands, with the value used when the config file
# leaves it out.  Keeping the full shape here means the rest of the code can
# read any key without defensive `.get()` chains.
DEFAULTS: dict[str, Any] = {
    "site": {
        "name": "Camera Monitoring",
        "timezone_note": "",
    },
    "inventory": {
        "file": "cameras.csv",
    },
    "discovery": {
        "subnets": [],
        "ports": [80, 554, 8000, 37777, 443, 8080, 8899],
        "onvif_probe": True,
        "timeout": 1.0,
        "workers": 256,
    },
    "credentials": {
        # Used for every camera that has no username/password of its own.
        "default": {"username": "", "password": ""},
        # Optional per-subnet credentials, e.g. one password per floor.
        "overrides": [],
    },
    "checks": {
        "workers": 100,
        "tcp_timeout": 2.0,
        "http_timeout": 6.0,
        "interval_seconds": 300,
        "offline_after_failures": 2,
        "storage_check": True,
        "storage_every_n_cycles": 1,
        "history_retention_days": 30,
    },
    "database": {
        "file": "data/monitor.db",
    },
    "alerts": {
        "enabled": True,
        "min_repeat_hours": 6,
        "console": True,
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "use_tls": True,
            "username": "",
            "password": "",
            "from_address": "",
            "to_addresses": [],
        },
        "webhook": {
            "enabled": False,
            "url": "",
            "message_field": "text",
        },
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "refresh_seconds": 30,
        "username": "",
        "password": "",
    },
}


class ConfigError(Exception):
    """Raised when the configuration file cannot be used as written."""


def _expand_env(value: Any) -> Any:
    """Replace ``${VAR}`` references inside any nested string value."""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, fallback = match.group(1), match.group(2)
            return os.environ.get(name, fallback if fallback is not None else "")

        return _ENV_REF.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay user settings on top of the defaults."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """Dotted-path access to the merged configuration."""

    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        self.data = data
        self.path = path
        # Relative paths in the config are resolved against the config file's
        # directory, so the tool works no matter where it is launched from.
        self.base_dir = path.parent.resolve() if path else Path.cwd()

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path_for(self, dotted: str) -> Path:
        """Resolve a configured file path relative to the config file."""
        raw = self.get(dotted)
        if not raw:
            raise ConfigError(f"Missing required path setting: {dotted}")
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = self.base_dir / candidate
        return candidate

    def credentials_for(self, ip: str) -> tuple[str, str]:
        """Return the (username, password) that applies to this IP address.

        A subnet override wins over the global default; a camera's own
        credentials (from the CSV) win over both and are applied by the caller.
        """
        for override in self.get("credentials.overrides", []) or []:
            subnet = override.get("subnet")
            if not subnet:
                continue
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(subnet, strict=False):
                    return override.get("username", ""), override.get("password", "")
            except ValueError:
                continue
        default = self.get("credentials.default", {}) or {}
        return default.get("username", ""), default.get("password", "")


def load_config(path: str | Path | None = None) -> Config:
    """Read the config file, apply defaults and expand environment variables."""
    if path is None:
        for candidate in ("config.yaml", "config.yml"):
            if Path(candidate).exists():
                path = candidate
                break
    if path is None:
        # No config file at all is fine: the defaults are a working setup.
        return Config(copy.deepcopy(DEFAULTS), None)

    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            "Copy config.example.yaml to config.yaml and edit it."
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level.")

    return Config(_merge(DEFAULTS, _expand_env(raw)), config_path)
