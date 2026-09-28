"""Cluster role: run the server's plan of read-only kubectl commands and upload the raw output.

The agent carries no list of its own. The server answers every upload with the
commands its gather asked for, and the next cycle runs exactly those. A new agent
starts with an empty plan, uploads nothing, and has the full plan within two
cycles. The output is sent as kubectl printed it; the server parses it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..scrapers.base_scanner import iso_now
from .kubectl import is_readonly, run_kubectl

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ROLE = "cluster"
# Until the server has sent a plan there is nothing to collect, so ask again soon
# rather than waiting a full interval.
BOOTSTRAP_RETRY_SECONDS = 5


class ClusterRole:
    """Collects one cycle of cluster-wide state by running the server's plan."""

    kind = ROLE

    def __init__(self, run: Callable[[list[str]], dict] = run_kubectl):
        self._run = run
        self.plan: list[list[str]] = []

    def collect(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "role": ROLE,
            "collected_at": iso_now(),
            "commands": [self._run(argv) for argv in self.plan],
        }

    def apply_reply(self, reply: dict) -> None:
        plan = reply.get("plan")
        if not isinstance(plan, list):
            return
        accepted = [argv for argv in plan if is_readonly(argv)]
        if len(accepted) != len(plan):
            logger.warning("Ignored %d non-read-only command(s) in the server's plan", len(plan) - len(accepted))
        self.plan = accepted

    def next_delay(self, interval_seconds: float) -> float:
        return interval_seconds if self.plan else min(interval_seconds, BOOTSTRAP_RETRY_SECONDS)
