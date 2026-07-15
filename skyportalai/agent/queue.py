"""SpoolQueue — disk-backed, bounded delivery buffer.

The containerized successor to the generated uploader's ``pending_runs.json``.
Each scan cycle's new runs are written as one *batch*: a single JSON file named
by a stable, monotonic ``batch_id``. Batches are just files, so they survive
process restarts and crashes; a batch is removed only once the shipper confirms
a 2xx.

The stable batch_id is what makes redelivery safe: the ingest endpoint merges by
``run_id``, so re-POSTing a batch after a crash mid-ship cannot duplicate data.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .scrapers.base_scanner import iso_now

logger = logging.getLogger(__name__)

DEFAULT_MAX_BATCHES = 1000


@dataclass(frozen=True)
class Batch:
    """One spooled unit of work: the runs from a single scan cycle."""

    batch_id: str
    runs: list[dict]
    path: Path


class SpoolQueue:
    """A bounded, disk-backed FIFO of run batches awaiting delivery."""

    def __init__(self, spool_dir: Path, max_batches: int = DEFAULT_MAX_BATCHES):
        self.spool_dir = Path(spool_dir)
        # enqueue() writes the batch first and only then enforces the bound, so a
        # non-positive max_batches would evict the batch just persisted (excess =
        # len(files) - max_batches >= len(files)). Fail fast instead of dropping data.
        if max_batches < 1:
            raise ValueError(f"max_batches must be >= 1, got {max_batches}")
        self.max_batches = max_batches

    def enqueue(self, runs: list[dict]) -> str | None:
        """Persist *runs* as a new batch; return its batch_id (None if empty)."""
        if not runs:
            return None
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        batch_id = f"{self._next_seq():012d}-{uuid.uuid4().hex[:8]}"
        self._write_atomic(batch_id, runs)
        self._enforce_bound()
        return batch_id

    def batches(self) -> list[Batch]:
        """Pending batches, oldest first. Unreadable files are skipped."""
        result: list[Batch] = []
        for path in self._batch_files():
            batch = self._read_batch(path)
            if batch is not None:
                result.append(batch)
        return result

    def remove(self, batch_id: str) -> None:
        """Delete a delivered batch. Missing files are ignored (idempotent)."""
        # Defense in depth: batch_id originates from file content, so refuse any
        # path separator / traversal before it reaches unlink().
        if batch_id in {"", ".", ".."} or batch_id != Path(batch_id).name:
            logger.error("Refusing to remove batch with unsafe id %r", batch_id)
            return
        try:
            (self.spool_dir / f"{batch_id}.json").unlink()
        except FileNotFoundError:
            pass

    def total_runs(self) -> int:
        return sum(len(b.runs) for b in self.batches())

    def is_empty(self) -> bool:
        return len(self) == 0

    def __len__(self) -> int:
        return len(self._batch_files())

    # ── internals ─────────────────────────────────────────────────────

    def _batch_files(self) -> list[Path]:
        if not self.spool_dir.exists():
            return []
        # Names are zero-padded, seq-prefixed, so a lexicographic sort is FIFO.
        return sorted(self.spool_dir.glob("*.json"))

    def _next_seq(self) -> int:
        highest = 0
        for path in self._batch_files():
            try:
                highest = max(highest, int(path.name.split("-", 1)[0]))
            except ValueError:
                continue
        return highest + 1

    def _write_atomic(self, batch_id: str, runs: list[dict]) -> None:
        final = self.spool_dir / f"{batch_id}.json"
        tmp = self.spool_dir / f"{batch_id}.json.tmp"
        payload = {"batch_id": batch_id, "created_at": iso_now(), "runs": runs}
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, final)  # atomic on POSIX; never leaves a partial *.json

    def _read_batch(self, path: Path) -> Batch | None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as exc:  # transient read failure: skip, retry next cycle
            logger.warning("Skipping unreadable batch file %s: %s", path.name, exc)
            return None
        except json.JSONDecodeError as exc:  # deterministic corruption: quarantine
            return self._quarantine(path, str(exc))
        if not isinstance(data, dict):
            return self._quarantine(path, f"top-level {type(data).__name__}, expected object")
        batch_id, runs = data.get("batch_id"), data.get("runs")
        if not isinstance(batch_id, str) or not isinstance(runs, list):
            return self._quarantine(
                path, f"batch_id={type(batch_id).__name__}, runs={type(runs).__name__}"
            )
        # The content-supplied batch_id is later fed to remove(); it must match the
        # trusted filename so a crafted id like "../VICTIM" can't escape the spool dir.
        if batch_id != path.stem:
            return self._quarantine(path, f"batch_id {batch_id!r} != filename stem {path.stem!r}")
        return Batch(batch_id=batch_id, runs=runs, path=path)

    def _quarantine(self, path: Path, reason: str) -> None:
        """Rename a malformed batch aside so it stops matching *.json and wedging delivery."""
        bad = path.with_suffix(".bad")
        try:
            os.replace(path, bad)
            logger.error("Quarantined corrupt batch %s -> %s: %s", path.name, bad.name, reason)
        except OSError as exc:
            logger.warning("Could not quarantine corrupt batch %s: %s", path.name, exc)
        return None

    def _enforce_bound(self) -> None:
        files = self._batch_files()
        excess = len(files) - self.max_batches
        for path in files[: max(0, excess)]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            logger.warning(
                "SpoolQueue full (max_batches=%d); evicted oldest batch %s",
                self.max_batches,
                path.stem,
            )
