"""
A small MJPEG server, so you can watch the camera from a laptop.

There is no monitor on a flying drone. If the Pi is already running as a Wi-Fi
access point for the badge, joining that network and opening a browser is the
only practical way to see what the camera sees.

MJPEG is a deliberately dumb choice: every frame is a standalone JPEG and the
browser needs no player, no plugin and no JavaScript. It costs more bandwidth
than H.264 but it survives a flaky link, because a dropped frame never corrupts
the ones after it the way a dropped H.264 keyframe does.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("mjpeg")

BOUNDARY = "frameboundary"

_PAGE = b"""<!doctype html>
<title>OAK-1</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#111; color:#ddd;
         font:14px system-ui,-apple-system,sans-serif;
         display:flex; flex-direction:column; align-items:center; gap:12px;
         padding:16px; }
  img { max-width:100%; height:auto; border-radius:8px; background:#000; }
  p { margin:0; opacity:.6 }
</style>
<h3>OAK-1</h3>
<img src="/stream.mjpg" alt="camera stream">
<p>Live stream. Reload if it stalls.</p>
"""


class _Broadcaster:
    """Holds the newest frame and wakes every waiting client when it changes.

    Frames carry a sequence number so a client that has not seen the current
    one gets it straight away instead of waiting for the next publish. Without
    that, a browser connecting just after a frame went out shows nothing until
    the next one, and if the camera has stalled it shows nothing at all.
    """

    def __init__(self) -> None:
        self._frame: bytes | None = None
        self._sequence = 0
        self._condition = threading.Condition()

    def publish(self, jpeg: bytes) -> None:
        with self._condition:
            self._frame = jpeg
            self._sequence += 1
            self._condition.notify_all()

    def wait(self, seen: int, timeout: float = 5.0) -> tuple[int, bytes | None]:
        """Return the newest frame once it is newer than `seen`.

        Returns (sequence, None) on timeout so the caller can keep the
        connection alive rather than treating a quiet camera as a disconnect.
        """
        with self._condition:
            if self._sequence != seen and self._frame is not None:
                return self._sequence, self._frame
            self._condition.wait(timeout=timeout)
            if self._sequence != seen and self._frame is not None:
                return self._sequence, self._frame
            return self._sequence, None


class _Handler(BaseHTTPRequestHandler):
    broadcaster: _Broadcaster  # injected by MjpegServer

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802  (name fixed by the base class)
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)
            return

        if self.path != "/stream.mjpg":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}"
        )
        self.end_headers()

        seen = -1
        try:
            while True:
                seen, jpeg = self.broadcaster.wait(seen)
                if jpeg is None:
                    continue
                self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # Someone closed the tab. Entirely normal, not worth a traceback.
            log.debug("client %s disconnected", self.client_address[0])

    def log_message(self, *args) -> None:
        # The default handler prints a line per request to stderr, which buries
        # everything else once a browser is connected.
        pass


class MjpegServer:
    """Serve the newest frame to any browser that asks.

    Call update() as often as you like; slow clients simply miss frames rather
    than backing up and stalling the camera loop.
    """

    def __init__(self, port: int = 8080, host: str = "0.0.0.0") -> None:
        self.port = port
        self.host = host
        self._broadcaster = _Broadcaster()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = type("Handler", (_Handler,), {"broadcaster": self._broadcaster})

        # ThreadingHTTPServer already mixes in ThreadingMixIn; naming both as
        # bases is an unresolvable MRO, not merely redundant.
        class Server(ThreadingHTTPServer):
            daemon_threads = True      # do not hold up shutdown for open streams
            allow_reuse_address = True # rebind straight after a restart

        self._server = Server((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("mjpeg server on http://%s:%d/", self.host, self.port)

    def update(self, jpeg: bytes) -> None:
        self._broadcaster.publish(jpeg)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "MjpegServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
