"""Tests for SpoolQueue — the disk-backed, bounded delivery buffer.

The queue is the durability layer: runs scanned in one cycle are written to
disk as a batch and survive process restarts / crashes until the shipper
confirms a 2xx, at which point the batch is removed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from skyportalai.agent.queue import SpoolQueue


def _runs(*ids: str) -> list[dict]:
    return [{"run_id": rid, "experiment_id": "exp", "source": "wandb"} for rid in ids]


def test_init_rejects_non_positive_max_batches(tmp_path: Path):
    # enqueue() writes the batch before enforcing the bound, so max_batches=0 would
    # evict the batch it just persisted — turning bad config into dropped data.
    # Fail fast in the constructor instead, mirroring Shipper's guards.
    with pytest.raises(ValueError, match="max_batches"):
        SpoolQueue(tmp_path, max_batches=0)
    with pytest.raises(ValueError, match="max_batches"):
        SpoolQueue(tmp_path, max_batches=-1)


def test_enqueue_returns_batch_id_and_writes_one_file(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    batch_id = q.enqueue(_runs("r1", "r2"))

    assert batch_id is not None
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert len(q) == 1
    assert q.total_runs() == 2


def test_enqueue_persists_across_restart(tmp_path: Path):
    # A fresh SpoolQueue over the same directory models a process restart:
    # the batch written before the "crash" must still be pending afterwards.
    q1 = SpoolQueue(tmp_path)
    batch_id = q1.enqueue(_runs("r1"))

    q2 = SpoolQueue(tmp_path)
    batches = q2.batches()

    assert len(batches) == 1
    assert batches[0].batch_id == batch_id
    assert batches[0].runs == _runs("r1")


def test_enqueue_empty_is_noop(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    assert q.enqueue([]) is None
    assert len(q) == 0
    assert q.batches() == []


def test_batches_returned_in_fifo_order(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("first"))
    q.enqueue(_runs("second"))
    q.enqueue(_runs("third"))

    order = [b.runs[0]["run_id"] for b in q.batches()]
    assert order == ["first", "second", "third"]


def test_remove_deletes_only_that_batch(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    first = q.enqueue(_runs("a"))
    q.enqueue(_runs("b"))

    q.remove(first)

    remaining = q.batches()
    assert len(remaining) == 1
    assert remaining[0].runs[0]["run_id"] == "b"


def test_remove_missing_batch_is_silent(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.remove("does-not-exist")  # must not raise


def test_bounded_eviction_drops_oldest_and_logs(tmp_path: Path, caplog):
    q = SpoolQueue(tmp_path, max_batches=2)
    with caplog.at_level(logging.WARNING):
        q.enqueue(_runs("oldest"))
        q.enqueue(_runs("middle"))
        q.enqueue(_runs("newest"))

    remaining = [b.runs[0]["run_id"] for b in q.batches()]
    assert remaining == ["middle", "newest"]
    assert len(q) == 2
    # The drop must be observable, never silent.
    assert any("evict" in r.message.lower() for r in caplog.records)


def test_corrupt_batch_file_is_skipped(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("valid"))
    (tmp_path / "0-garbage.json").write_text("{not json")

    batches = q.batches()

    assert len(batches) == 1
    assert batches[0].runs[0]["run_id"] == "valid"


def test_enqueue_leaves_no_partial_tmp_files(tmp_path: Path):
    # Writes are atomic (temp + os.replace); a completed enqueue must never
    # leave a half-written temp file behind for batches() to choke on.
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    assert list(tmp_path.glob("*.tmp")) == []


def test_batch_file_contains_batch_id_and_runs(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    batch_id = q.enqueue(_runs("r1"))

    payload = json.loads((tmp_path / f"{batch_id}.json").read_text())
    assert payload["batch_id"] == batch_id
    assert payload["runs"] == _runs("r1")


def test_batches_empty_when_dir_absent(tmp_path: Path):
    # Constructing the queue must be side-effect free; the directory is created
    # lazily on first enqueue.
    q = SpoolQueue(tmp_path / "not-created-yet")
    assert q.batches() == []
    assert len(q) == 0


def test_poison_batch_with_null_runs_is_quarantined_not_wedged(tmp_path: Path, caplog):
    # A batch whose "runs" is null (partial/legacy write) passes the key-presence
    # check but would crash _ship_batch forever. It must be quarantined to *.bad,
    # not left to wedge delivery, and the valid batch still ships.
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("valid"))
    (tmp_path / "0-poison.json").write_text(json.dumps({"batch_id": "0-poison", "runs": None}))

    with caplog.at_level(logging.ERROR):
        batches = q.batches()

    assert [b.runs[0]["run_id"] for b in batches] == ["valid"]
    assert (tmp_path / "0-poison.bad").exists()
    assert not (tmp_path / "0-poison.json").exists()
    assert "quarantine" in caplog.text.lower()


def test_non_list_runs_is_quarantined(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    (tmp_path / "0-dict.json").write_text(json.dumps({"batch_id": "0-dict", "runs": {"a": 1}}))
    assert q.batches() == []
    assert (tmp_path / "0-dict.bad").exists()


def test_corrupt_file_stops_counting_toward_len_after_read(tmp_path: Path):
    # __len__/is_empty count the *.json glob, so a corrupt file would otherwise
    # make the queue report non-empty forever. Reading it quarantines it to *.bad,
    # so the count self-heals.
    q = SpoolQueue(tmp_path)
    (tmp_path / "0-bad.json").write_text("{not json")
    assert len(q) == 1  # raw glob still sees it before the quarantining read

    q.batches()

    assert len(q) == 0
    assert q.is_empty()


def test_batch_id_not_matching_filename_is_quarantined(tmp_path: Path):
    # A content-supplied batch_id that doesn't match the filename stem is a
    # path-traversal payload (or corruption): quarantine it, never hand it to remove().
    q = SpoolQueue(tmp_path)
    (tmp_path / "0-evil.json").write_text(json.dumps({"batch_id": "../VICTIM", "runs": []}))

    assert q.batches() == []
    assert (tmp_path / "0-evil.bad").exists()
    assert not (tmp_path / "0-evil.json").exists()


def test_remove_refuses_path_traversal_id(tmp_path: Path):
    # remove() must not delete a .json file outside the spool dir even if handed a
    # crafted batch_id directly (PoC: spool/../VICTIM.json).
    victim = tmp_path / "VICTIM.json"
    victim.write_text("{}")
    spool = tmp_path / "spool"
    spool.mkdir()
    q = SpoolQueue(spool)

    q.remove("../VICTIM")

    assert victim.exists()  # the traversal target is untouched
