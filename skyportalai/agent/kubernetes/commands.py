"""Answer chat's on-demand kubectl: lease a queued command, run it, post the result.

Chat on a cluster reached only through this agent queues its command on the
server. This loop (cluster role only) picks each one up over outbound HTTPS. The
command is checked against the read-only allowlist here as well as on the server.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import requests

from ..._version import __version__
from .kubectl import run_kubectl

logger = logging.getLogger(__name__)

LEASE_PATH = "/agent/api/kubernetes/commands/lease/"
RESULT_PATH = "/agent/api/kubernetes/commands/{id}/result/"
# How long a chat command can wait before the agent notices it. The server gives up
# after a minute, so this keeps chat latency to a couple of seconds.
POLL_SECONDS = 2
_HTTP_TIMEOUT = 30


class CommandPoller:
    """Polls for chat commands until stopped."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: requests.Session | None = None,
        run: Callable[[list[str]], dict] = run_kubectl,
        stop_event: threading.Event | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": f"skyportalai-agent/{__version__}"}
        self._run = run
        self._stop = stop_event or threading.Event()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                handled = self.poll_once()
            except requests.RequestException as exc:
                logger.warning("Command poll failed: %s", type(exc).__name__)
                handled = False
            if not handled:
                self._stop.wait(POLL_SECONDS)

    def poll_once(self) -> bool:
        """Run one queued command if there is one; True if a command was handled."""
        resp = self.session.post(self.base_url + LEASE_PATH, headers=self.headers, timeout=_HTTP_TIMEOUT)
        if resp.status_code == 204:
            return False
        resp.raise_for_status()
        command = resp.json()
        result = self._run(command["argv"])
        self.session.post(
            self.base_url + RESULT_PATH.format(id=int(command["id"])),
            json={key: result[key] for key in ("exit_code", "stdout", "stderr")},
            headers=self.headers,
            timeout=_HTTP_TIMEOUT,
        ).raise_for_status()
        return True
