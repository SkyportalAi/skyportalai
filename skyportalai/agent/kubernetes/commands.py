"""Answer chat's on-demand kubectl: lease a queued command, run it, post the result.

Chat on a cluster reached only through this agent queues its command on the
server. This loop (cluster role only) picks each one up over outbound HTTPS. The
command is checked against the read-only allowlist here as well as on the server.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable

import requests

from ..._version import __version__
from .kubectl import DEFAULT_TIMEOUT_SECONDS, run_kubectl

logger = logging.getLogger(__name__)

LEASE_PATH = "/agent/api/kubernetes/commands/lease/"
RESULT_PATH = "/agent/api/kubernetes/commands/{id}/result/"
# How long a chat command can wait before the agent notices it. The server waits for each
# command's own timeout (sent with the lease), so this short poll only keeps chat latency
# to a couple of seconds.
POLL_SECONDS = 2
_HTTP_TIMEOUT = 30
# Mirrors the server's longest per-command timeout.
MAX_COMMAND_TIMEOUT_SECONDS = 1800
# Seconds to wait before each retry of a result post that failed transiently: about 77 s
# in all, enough to ride out a web restart.
RESULT_RETRY_DELAYS = (2, 5, 10, 20, 40)


class CommandPoller:
    """Polls for chat commands until stopped."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: requests.Session | None = None,
        run: Callable[[list[str], float], dict] = run_kubectl,
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
            except Exception:  # noqa: BLE001 — /healthz can't see this thread, so it must not die
                logger.exception("Command poll failed")
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
        command_id = _command_id(command)
        if command_id is None:
            logger.warning("Ignored a leased command with no usable id")
            return False
        # run_kubectl refuses an argv that isn't a read-only kubectl list, so a malformed
        # one still gets a result back and chat fails fast instead of waiting it out.
        result = self._run(command.get("argv"), _command_timeout(command))
        self._post_result(command_id, {key: result[key] for key in ("exit_code", "stdout", "stderr")})
        return True

    def _post_result(self, command_id: int, payload: dict) -> None:
        """Post a command's result, retrying what a brief SkyPortal outage would explain."""
        url = self.base_url + RESULT_PATH.format(id=command_id)
        attempts = len(RESULT_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                resp = self.session.post(url, json=payload, headers=self.headers, timeout=_HTTP_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                failure = type(exc).__name__
            else:
                if resp.status_code < 400:
                    return
                failure = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # a 404 means the server no longer wants this result; resending can't change that
            if attempt == attempts - 1 or self._stop.wait(RESULT_RETRY_DELAYS[attempt]):
                break
        logger.warning("Gave up posting the result of command %s: %s", command_id, failure)


def _command_id(command) -> int | None:
    if not isinstance(command, dict):
        return None
    try:
        return int(command.get("id"))
    except (TypeError, ValueError):
        return None


def _command_timeout(command: dict) -> float:
    value = command.get("timeout_seconds")
    # A bool is an int, and NaN and infinity fail the range check. An old server sends none.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < math.inf:
        return DEFAULT_TIMEOUT_SECONDS
    return min(value, MAX_COMMAND_TIMEOUT_SECONDS)
