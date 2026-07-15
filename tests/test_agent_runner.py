"""Tests for AgentRunner — the scan -> enqueue -> ship loop and SIGTERM flush.

The runner wires the ported P0 scanners + catalog to the P1 queue + shipper, and
owns the lifecycle: an interruptible interval loop that drains one final time on
shutdown so a SIGTERM doesn't strand queued runs.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from skyportalai.agent.catalog import load_catalog, save_catalog
from skyportalai.agent.queue import SpoolQueue
from skyportalai.agent.runner import AgentRunner
from skyportalai.agent.shipper import ShipResult


def _runs(*ids: str) -> list[dict]:
    return [{"run_id": rid, "experiment_id": "exp", "source": "wandb"} for rid in ids]


class FakeScanner:
    def __init__(self, source_name, runs, *, available=True, raises=False, on_discover=None):
        self._source = source_name
        self._runs = runs
        self._available = available
        self._raises = raises
        self._on_discover = on_discover
        self.discover_calls = 0

    @property
    def source_name(self):
        return self._source

    def is_available(self):
        return self._available

    def find_root_dirs(self, search_root=None):
        return [Path("/fake")]

    def discover_runs(self, root_dirs, catalog, log_path=None):
        self.discover_calls += 1
        if self._on_discover is not None:
            self._on_discover()
        if self._raises:
            raise RuntimeError("scan boom")
        return list(self._runs)


class RecordingShipper:
    """A shipper that records ship() calls but never drains the queue."""

    def __init__(self, on_ship=None, raise_first=0):
        self.calls = 0
        self._on_ship = on_ship
        self._raise_first = raise_first

    def ship(self, queue):
        self.calls += 1
        if self._on_ship is not None:
            self._on_ship()
        if self.calls <= self._raise_first:
            raise RuntimeError("ship boom")
        return ShipResult(0, 0, len(queue))


def _runner(tmp_path, scanners, shipper, **kw):
    kw.setdefault("interval_seconds", 0.01)
    return AgentRunner(
        scanners=scanners,
        catalog_path=tmp_path / "cat.json",
        queue=SpoolQueue(tmp_path / "spool"),
        shipper=shipper,
        **kw,
    )


def test_run_once_enqueues_scanned_runs_and_ships(tmp_path: Path):
    scanner = FakeScanner("wandb", _runs("r1", "r2"))
    shipper = RecordingShipper()
    runner = _runner(tmp_path, [scanner], shipper)

    result = runner.run_once()

    assert result.new_runs == 2
    assert runner.queue.total_runs() == 2  # recording shipper left the batch
    assert shipper.calls == 1


def test_run_once_persists_catalog(tmp_path: Path):
    scanner = FakeScanner("wandb", _runs("r1"))
    runner = _runner(tmp_path, [scanner], RecordingShipper())

    runner.run_once()

    saved = load_catalog(tmp_path / "cat.json")
    run_ids = [r["run_id"] for exp in saved["experiments"] for r in exp["runs"]]
    assert "r1" in run_ids


def test_run_once_redacts_secrets_before_disk_persistence(tmp_path: Path):
    run = {
        "run_id": "r1",
        "experiment_id": "exp",
        "source": "wandb",
        "config": {
            "learning_rate": 0.1,
            "api_key": "sk-live-secret",
            "nested": {"oauthClientSecret": "oauth-secret", "epochs": 3},
        },
        "tags": {"authorization": "Bearer live-secret", "team": "ml"},
    }
    runner = _runner(tmp_path, [FakeScanner("wandb", [run])], RecordingShipper())

    runner.run_once()

    queued = runner.queue.batches()[0].runs[0]
    saved = load_catalog(tmp_path / "cat.json")["experiments"][0]["runs"][0]
    for persisted in (queued, saved):
        assert persisted["config"]["api_key"] == "[REDACTED]"
        assert persisted["config"]["nested"]["oauthClientSecret"] == "[REDACTED]"
        assert persisted["tags"]["authorization"] == "[REDACTED]"
        assert persisted["config"]["learning_rate"] == 0.1
        assert persisted["config"]["nested"]["epochs"] == 3
        assert persisted["tags"]["team"] == "ml"


def test_run_once_persists_catalog_on_cold_start_with_no_runs(tmp_path: Path):
    # Cold start: state_dir does not exist yet and the first cycle finds no runs.
    # enqueue() is skipped (nothing to spool), so it never creates the dir — but
    # save_catalog() must still succeed by creating the parent itself, otherwise the
    # empty startup raises FileNotFoundError every interval.
    state_dir = tmp_path / "fresh" / "state"  # nonexistent
    catalog_path = state_dir / "cat.json"
    runner = AgentRunner(
        scanners=[FakeScanner("wandb", [])],  # discovers nothing
        catalog_path=catalog_path,
        queue=SpoolQueue(state_dir / "spool"),
        shipper=RecordingShipper(),
        interval_seconds=0.01,
    )

    result = runner.run_once()  # must not raise FileNotFoundError

    assert result.new_runs == 0
    assert catalog_path.exists()
    load_catalog(catalog_path)  # persisted and readable back


def test_run_once_enqueues_before_saving_catalog(tmp_path: Path, monkeypatch):
    # Data-integrity invariant: a run must be durable in the queue BEFORE the
    # catalog records it as seen. If the catalog were saved first, a crash before
    # the enqueue would lose the run — the next scan skips it as already-seen, yet
    # it never reached the queue and so is never shipped.
    scanner = FakeScanner("wandb", _runs("r1"))
    runner = _runner(tmp_path, [scanner], RecordingShipper())

    order: list[str] = []
    real_enqueue = runner.queue.enqueue

    def spy_enqueue(runs):
        order.append("enqueue")
        return real_enqueue(runs)

    def spy_save(path, catalog):
        order.append("save_catalog")
        return save_catalog(path, catalog)

    monkeypatch.setattr(runner.queue, "enqueue", spy_enqueue)
    monkeypatch.setattr("skyportalai.agent.runner.save_catalog", spy_save)

    runner.run_once()

    assert order == ["enqueue", "save_catalog"]


def test_run_once_keeps_runs_durable_when_catalog_save_crashes(tmp_path: Path, monkeypatch):
    # Simulate a crash during save_catalog (after the enqueue). Because the run
    # was enqueued first, it survives in the durable on-disk queue: a fresh queue
    # over the same spool dir still sees it, so a restart re-ships it (no loss).
    scanner = FakeScanner("wandb", _runs("r1"))
    runner = _runner(tmp_path, [scanner], RecordingShipper())

    def boom(path, catalog):
        raise RuntimeError("crash mid-save")

    monkeypatch.setattr("skyportalai.agent.runner.save_catalog", boom)

    with pytest.raises(RuntimeError, match="crash mid-save"):
        runner.run_once()

    assert SpoolQueue(tmp_path / "spool").total_runs() == 1


def test_run_once_skips_unavailable_scanner(tmp_path: Path):
    scanner = FakeScanner("wandb", _runs("r1"), available=False)
    runner = _runner(tmp_path, [scanner], RecordingShipper())

    result = runner.run_once()

    assert scanner.discover_calls == 0
    assert result.new_runs == 0
    assert runner.queue.is_empty()


def test_run_once_continues_when_one_scanner_fails(tmp_path: Path):
    failing = FakeScanner("wandb", [], raises=True)
    healthy = FakeScanner("mlflow", _runs("r2"))
    runner = _runner(tmp_path, [failing, healthy], RecordingShipper())

    result = runner.run_once()

    # A blown scanner must not abort the cycle: the healthy one still ships.
    assert result.new_runs == 1
    assert runner.queue.total_runs() == 1


def test_run_forever_runs_one_cycle_then_flushes_on_stop(tmp_path: Path):
    stop = threading.Event()
    scanner = FakeScanner("wandb", _runs("r1"))
    # SIGTERM modelled as the stop event tripping during the cycle's ship.
    shipper = RecordingShipper(on_ship=stop.set)
    runner = _runner(tmp_path, [scanner], shipper, stop_event=stop)

    runner.run_forever()

    assert scanner.discover_calls == 1  # exactly one cycle ran
    assert shipper.calls == 2  # per-cycle ship + one final flush on shutdown


def test_run_forever_survives_a_failing_cycle(tmp_path: Path):
    stop = threading.Event()
    # A cycle-level failure (the ship raising) must not kill the daemon; the
    # scanner trips shutdown so the loop is bounded, and the final flush — a
    # second ship that succeeds — still runs.
    scanner = FakeScanner("wandb", _runs("r1"), on_discover=stop.set)
    shipper = RecordingShipper(raise_first=1)
    runner = _runner(tmp_path, [scanner], shipper, stop_event=stop)

    runner.run_forever()  # must not propagate the cycle exception

    assert scanner.discover_calls == 1
    assert shipper.calls == 2  # cycle ship raised, shutdown flush succeeded


def test_stop_sets_the_event(tmp_path: Path):
    stop = threading.Event()
    runner = _runner(tmp_path, [], RecordingShipper(), stop_event=stop)

    runner.stop()

    assert stop.is_set()
