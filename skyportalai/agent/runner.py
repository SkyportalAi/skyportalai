"""AgentRunner — the scan -> enqueue -> ship loop with graceful shutdown.

Wires the ported P0 scanners + catalog to the P1 queue + shipper. The loop is
resilient by construction: a single scanner failure is contained to that scanner
(the cycle still ships everything else), and a failed cycle is logged without
killing the daemon. On shutdown (SIGTERM/SIGINT, surfaced via the stop event) it
breaks the interval wait and drains the queue one final time so in-flight runs
are not stranded.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .catalog import assign_run_indices, load_catalog, save_catalog, upsert_runs
from .queue import SpoolQueue
from .scrapers.base_scanner import redact_sensitive_values
from .shipper import ShipResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleResult:
    new_runs: int
    ship: ShipResult


class AgentRunner:
    """Owns the agent lifecycle: scan, enqueue, ship, repeat, flush on stop."""

    def __init__(
        self,
        *,
        scanners: list,
        catalog_path: Path,
        queue: SpoolQueue,
        shipper,
        interval_seconds: float,
        roots: Mapping[str, Path | None] | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.scanners = scanners
        self.catalog_path = Path(catalog_path)
        self.queue = queue
        self.shipper = shipper
        self.interval_seconds = interval_seconds
        self.roots = dict(roots or {})
        self._stop = stop_event or threading.Event()

    def run_once(self) -> CycleResult:
        """One cycle: scan every scanner, enqueue, persist the catalog, ship."""
        catalog = load_catalog(self.catalog_path)
        new_runs: list[dict] = []
        for scanner in self.scanners:
            new_runs.extend(self._scan(scanner, catalog))
        # Make the runs durable in the queue BEFORE the catalog records them as
        # seen. The catalog is what scanners diff against to skip finished runs,
        # so if it were saved first a crash before the enqueue would lose those
        # runs for good (next scan skips them, yet they never reached the queue).
        # Enqueuing first degrades that window to harmless idempotent re-delivery
        # instead — the server merges by run_id, so a rediscovered run can't dup.
        if new_runs:
            self.queue.enqueue(new_runs)
        save_catalog(self.catalog_path, catalog)
        ship = self.shipper.ship(self.queue)
        return CycleResult(new_runs=len(new_runs), ship=ship)

    def run_forever(self) -> None:
        """Loop until the stop event is set, then flush the queue once more."""
        while not self._stop.is_set():
            try:
                result = self.run_once()
                logger.info(
                    "cycle: %d new run(s), %d batch(es) shipped, %d remaining",
                    result.new_runs,
                    result.ship.batches_shipped,
                    result.ship.batches_remaining,
                )
            except Exception:
                logger.exception("Agent cycle failed; continuing")
            # Interruptible sleep: a stop set by the signal handler wakes us.
            if self._stop.wait(self.interval_seconds):
                break
        self._flush()

    def stop(self) -> None:
        self._stop.set()

    # ── internals ─────────────────────────────────────────────────────

    def _scan(self, scanner, catalog) -> list[dict]:
        source = getattr(scanner, "source_name", "?")
        try:
            if not scanner.is_available():
                logger.debug("Scanner %s unavailable; skipping", source)
                return []
            root_dirs = scanner.find_root_dirs(self.roots.get(source))
            found = scanner.discover_runs(root_dirs, catalog)
            found = [redact_sensitive_values(run) for run in found]
        except Exception:
            logger.exception("Scanner %s failed; skipping this cycle", source)
            return []
        if found:
            assign_run_indices(catalog, found)
            upsert_runs(catalog, found)
        return found

    def _flush(self) -> None:
        try:
            result = self.shipper.ship(self.queue)
            logger.info(
                "shutdown flush: %d batch(es) shipped, %d remaining",
                result.batches_shipped,
                result.batches_remaining,
            )
        except Exception:
            logger.exception("Shutdown flush failed")
