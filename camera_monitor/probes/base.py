"""Shared types and the HTTP helper used by every vendor probe."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

import requests
import urllib3
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

# Cameras almost always present a self-signed certificate on HTTPS.  We skip
# verification for them specifically and silence the resulting noise.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class StorageState(str, enum.Enum):
    """Health of a camera's recording media (SD card or internal disk)."""

    OK = "ok"
    FAILED = "failed"        # card reports an error / is read-only / unformatted
    MISSING = "missing"      # camera answered, but no card is present
    UNKNOWN = "unknown"      # could not determine (no credentials, unsupported, error)

    @property
    def is_problem(self) -> bool:
        return self in (StorageState.FAILED, StorageState.MISSING)


@dataclass
class StorageInfo:
    """Result of an SD card / storage health check."""

    state: StorageState = StorageState.UNKNOWN
    message: str = ""
    brand: str = "unknown"
    # One entry per physical card/disk: name, status, capacity_mb, free_mb.
    disks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.message:
            return self.message
        return self.state.value


class ProbeError(Exception):
    """A vendor endpoint could not be reached or understood."""


class UnsupportedDevice(ProbeError):
    """The device answered but does not speak this vendor's API.

    Raised so that brand auto-detection can move on to the next vendor
    instead of reporting a real fault.
    """


def camera_get(
    ip: str,
    port: int,
    path: str,
    username: str,
    password: str,
    timeout: float,
    use_https: bool = False,
) -> requests.Response:
    """GET a camera endpoint, trying digest auth first and basic as a fallback.

    Hikvision and Dahua both default to digest; some older firmware and some
    OEM rebadges only accept basic.  Trying both costs one extra request and
    removes a whole class of "why does it say unauthorised" support tickets.
    """
    scheme = "https" if use_https or port == 443 else "http"
    url = f"{scheme}://{ip}:{port}{path}"
    session = requests.Session()
    session.trust_env = False  # never send camera traffic through an HTTP proxy

    attempts = [HTTPDigestAuth(username, password), HTTPBasicAuth(username, password)]
    last_response: requests.Response | None = None
    for auth in attempts:
        try:
            response = session.get(url, auth=auth, timeout=timeout, verify=False)
        except requests.exceptions.RequestException as exc:
            raise ProbeError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code != 401:
            return response
        last_response = response
    # Both auth styles were rejected.
    assert last_response is not None
    return last_response
