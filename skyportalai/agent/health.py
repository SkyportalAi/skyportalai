"""HealthServer — a minimal /healthz liveness endpoint for Kubernetes probes.

Runs a tiny stdlib HTTP server on a daemon thread so the run loop stays on the
main thread (where signal handlers must live). Liveness-only: it returns 200
while the process is up; readiness/last-cycle reporting is deferred.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            body = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args) -> None:  # silence per-request stderr logging
        pass


class HealthServer:
    """Serves /healthz on a background daemon thread."""

    def __init__(self, port: int, host: str = "0.0.0.0"):
        self._server = ThreadingHTTPServer((host, port), _HealthHandler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound port (resolves the OS-assigned port when constructed with 0)."""
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="healthz"
        )
        self._thread.start()

    def stop(self) -> None:
        # shutdown() blocks on an event only set by a running serve_forever(), so
        # calling it before start() would hang forever — guard it behind a live
        # thread. server_close() always runs to release the socket bound in
        # __init__, even if the server never served.
        if self._thread is not None and self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=2)
        self._server.server_close()
