"""Answer chat's on-demand kubectl: lease a queued command, run it, post the result.

Chat on a cluster reached only through this agent queues its command on the
server. This loop (cluster role only) picks each one up over outbound HTTPS and
runs it on a thread of its own, up to COMMAND_SLOTS at a time. The command is
checked against the read-only allowlist here as well as on the server.

The skyportalai CLI reuses this loop to run the commands queued for the user's own
machine (skyportal-website#3665), with its own endpoints, credential and runner; the
lease, the slots and the result retries until report_by are the same.
"""

from __future__ import annotations

import itertools
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

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
# How many chat commands run at once. The server starts a command's clock when it is
# leased, so a command is leased only when a slot is free to run it.
COMMAND_SLOTS = 4
# Seconds to wait before each retry of a result post that failed transiently. When the lease
# carries report_by, RESULT_RETRY_MAX_DELAY follows these until that deadline. An older
# server sends none, and these alone (about 77 s in all) ride out a web restart.
RESULT_RETRY_DELAYS = (2, 5, 10, 20, 40)
RESULT_RETRY_MAX_DELAY = 60
# Matches MAX_COMMAND_OUTPUT_CHARS in skyportal-website's website/api/k8s_agent_api.py. The
# server keeps only this head of stdout and of stderr, so a worker holds no more than that
# while it posts and retries.
MAX_COMMAND_OUTPUT_CHARS = 1024 * 1024
# Tells the server this agent runs a command for the lease's timeout_seconds and retries its
# result until report_by. An agent that doesn't send it gets 30 s leases (skyportal-website#3709).
AGENT_CAPABILITIES = "command-timeouts"


@dataclass(frozen=True)
class _LeasedCommand:
    id: int
    # What the runner is handed: kubectl's argv here; the CLI's local machine takes the whole lease.
    argv: object
    timeout: float
    # When the server stops taking the result, on the poller's clock; None if it didn't say.
    report_deadline: float | None


class CommandPoller:
    """Leases chat commands and runs each on its own thread, up to COMMAND_SLOTS at a time, until stopped."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: requests.Session | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
        run: Callable[[list[str], float], dict] = run_kubectl,
        stop_event: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
        lease_path: str = LEASE_PATH,
        result_path: str = RESULT_PATH,
        extra_headers: Mapping[str, str] | None = None,
        # The lease field the runner is handed; None hands it the whole lease.
        command_field: str | None = "argv",
        thread_prefix: str = "kube-command",
    ):
        self.base_url = base_url.rstrip("/")
        self._lease_path = lease_path
        self._result_path = result_path
        self._command_field = command_field
        self._thread_prefix = thread_prefix
        # requests doesn't promise a Session is safe to share across threads. Leases use this
        # one, on the polling thread only; each command's thread posts its result on a session
        # of its own from session_factory.
        self.session = session or requests.Session()
        self._session_factory = session_factory
        self.headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"skyportalai-agent/{__version__}",
            "X-Agent-Capabilities": AGENT_CAPABILITIES,
            **(extra_headers or {}),
        }
        self._run = run
        self._stop = stop_event or threading.Event()
        self._clock = clock
        self._slots = threading.BoundedSemaphore(COMMAND_SLOTS)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self._slots.acquire(timeout=POLL_SECONDS):
                continue  # every slot is busy; look at the stop event again
            if self._start_next() is None:
                self._stop.wait(POLL_SECONDS)

    def poll_once(self) -> threading.Thread | None:
        """Start the next queued command if a slot is free; returns the thread running it."""
        if not self._slots.acquire(blocking=False):
            return None
        return self._start_next()

    def _start_next(self) -> threading.Thread | None:
        """Lease a command for the slot the caller holds and start it; the slot is freed here if none starts."""
        try:
            command = None if self._stop.is_set() else self._lease()
            if command is not None:
                worker = threading.Thread(
                    target=self._work, args=(command,), name=f"{self._thread_prefix}-{command.id}", daemon=True
                )
                worker.start()
                return worker
        except requests.RequestException as exc:
            logger.warning("Command poll failed: %s", type(exc).__name__)
        except Exception:  # noqa: BLE001 — /healthz can't see this thread, so it must not die
            logger.exception("Command poll failed")
        self._slots.release()
        return None

    def _lease(self) -> _LeasedCommand | None:
        resp = self.session.post(self.base_url + self._lease_path, headers=self.headers, timeout=_HTTP_TIMEOUT)
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        command = resp.json()
        command_id = _command_id(command)
        if command_id is None:
            logger.warning("Ignored a leased command with no usable id")
            return None
        handed = command if self._command_field is None else command.get(self._command_field)
        return _LeasedCommand(command_id, handed, _command_timeout(command), self._report_deadline(command))

    def _report_deadline(self, command: dict) -> float | None:
        try:
            left = datetime.fromisoformat(command.get("report_by")) - datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return None  # an older server sends none, and one without a UTC offset can't be placed
        return self._clock() + max(0.0, left.total_seconds())

    def _work(self, command: _LeasedCommand) -> None:
        try:
            # run_kubectl refuses an argv that isn't a read-only kubectl list, so a malformed
            # one still gets a result back and chat fails fast instead of waiting it out.
            payload = _result_payload(self._run(command.argv, command.timeout))
            with self._session_factory() as session:
                self._post_result(session, command, payload)
        except Exception:
            logger.exception("Command %s failed", command.id)
        finally:
            self._slots.release()

    def _post_result(self, session: requests.Session, command: _LeasedCommand, payload: dict) -> None:
        """Post a command's result, retrying what a brief SkyPortal outage would explain."""
        url = self.base_url + self._result_path.format(id=command.id)
        waits = self._retry_waits(command.report_deadline)
        while True:
            try:
                resp = session.post(url, json=payload, headers=self.headers, timeout=_HTTP_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                failure = type(exc).__name__
            else:
                if resp.status_code < 400:
                    return
                failure = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # a 404 means the server no longer wants this result; resending can't change that
            wait = next(waits, None)
            if wait is None or self._stop.wait(wait):
                break
        logger.warning("Gave up posting the result of command %s: %s", command.id, failure)

    def _retry_waits(self, deadline: float | None) -> Iterator[float]:
        """The waits between result posts: up to `deadline`, or the fixed schedule without one."""
        if deadline is None:
            yield from RESULT_RETRY_DELAYS
            return
        for delay in itertools.chain(RESULT_RETRY_DELAYS, itertools.repeat(RESULT_RETRY_MAX_DELAY)):
            left = deadline - self._clock()
            if left <= 0:
                return
            yield min(delay, left)
            if delay >= left:
                return  # that wait ran to the deadline, so the post after it was the last


def _result_payload(result: dict) -> dict:
    """The result fields the server takes, each stream cut to the head it keeps."""
    return {
        "exit_code": result["exit_code"],
        "stdout": result["stdout"][:MAX_COMMAND_OUTPUT_CHARS],
        "stderr": result["stderr"][:MAX_COMMAND_OUTPUT_CHARS],
    }


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
    return value
