"""A stand-in IP camera for the tests.

Serves the same endpoints a real Hikvision or Dahua camera does, protected by
HTTP basic authentication, so the whole pipeline can be exercised without
touching real hardware.
"""

from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HIKVISION_OK = """<?xml version="1.0" encoding="UTF-8"?>
<storage xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
  <hddList size="1">
    <hdd>
      <id>1</id><hddName>hdd1</hddName><hddType>SD</hddType>
      <status>ok</status><capacity>30436</capacity><freeSpace>28160</freeSpace>
      <property>RW</property>
    </hdd>
  </hddList>
</storage>"""

HIKVISION_FAILED = HIKVISION_OK.replace("<status>ok</status>", "<status>error</status>")
HIKVISION_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<storage xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
  <hddList size="0"/>
</storage>"""

HIKVISION_DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">
  <deviceName>Lobby Camera</deviceName><model>DS-2CD2143G0-I</model>
  <serialNumber>DS-2CD2143G01234</serialNumber><firmwareVersion>V5.6.3</firmwareVersion>
</DeviceInfo>"""

DAHUA_OK = """list.info[0].Detail[0].Path=/mnt/sd
list.info[0].Detail[0].Type=Read-Write
list.info[0].Detail[0].TotalBytes=31254904832
list.info[0].Detail[0].UsedBytes=15627452416
list.info[0].Name=SD0
list.info[0].State=Active
"""

DAHUA_READONLY = DAHUA_OK.replace("Type=Read-Write", "Type=Read-Only")


class FakeCamera:
    """Runs a throwaway HTTP server that behaves like a camera."""

    def __init__(
        self,
        brand: str = "hikvision",
        storage: str = "ok",
        username: str = "admin",
        password: str = "secret",
        host: str = "127.0.0.1",
    ) -> None:
        self.host = host
        self.brand = brand
        self.storage = storage
        self.username = username
        self.password = password
        self.requests: list[str] = []
        self._server = ThreadingHTTPServer((host, 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def __enter__(self) -> "FakeCamera":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _body_for(self, path: str) -> tuple[int, str, str]:
        """Return (status, content type, body) for a request path."""
        if self.brand == "hikvision":
            if path.startswith("/ISAPI/ContentMgmt/Storage"):
                body = {
                    "ok": HIKVISION_OK,
                    "failed": HIKVISION_FAILED,
                    "missing": HIKVISION_EMPTY,
                }[self.storage]
                return 200, "application/xml", body
            if path.startswith("/ISAPI/System/deviceInfo"):
                return 200, "application/xml", HIKVISION_DEVICE_INFO
        elif self.brand == "dahua":
            if path.startswith("/cgi-bin/storageDevice.cgi"):
                body = {"ok": DAHUA_OK, "failed": DAHUA_READONLY, "missing": ""}[self.storage]
                return 200, "text/plain", body
            if path.startswith("/cgi-bin/magicBox.cgi"):
                return 200, "text/plain", "deviceType=IPC-HFW2431S\nserialNumber=ABC123\nversion=2.8\n"
        return 404, "text/plain", "Not Found"

    def _make_handler(self):
        camera = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                camera.requests.append(self.path)
                expected = base64.b64encode(
                    f"{camera.username}:{camera.password}".encode()
                ).decode()
                if self.headers.get("Authorization", "") != f"Basic {expected}":
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="Camera"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, content_type, body = camera._body_for(self.path)
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler
