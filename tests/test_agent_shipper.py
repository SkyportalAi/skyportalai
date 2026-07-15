"""Tests for Shipper — gzip chunked POST with retry/backoff over the queue.

The shipper owns POST retry because the SDK transport retries GETs only. It
clears a batch only on a full 2xx delivery; partial or failed deliveries leave
the batch on disk for the next cycle (safe — the server merges by run_id).
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from skyportalai.agent.queue import SpoolQueue
from skyportalai.agent.shipper import Shipper

TOKEN = "spa-secret"
BASE_URL = "https://skyportal.test"
INGEST_PATH = "/agent/api/observability/ingest/"


@dataclass
class FakeResponse:
    status_code: int

    def close(self) -> None:  # mirrors requests.Response.close()
        pass


class FakeSession:
    """Injected transport: serves scripted responses and records each POST."""

    def __init__(self, responses=None, default=None):
        self._responses = list(responses or [])
        self._default = default if default is not None else FakeResponse(200)
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):
        # allow_redirects defaults to True to mirror requests, so a shipper that
        # forgot to disable it would be caught by the recorded value.
        self.calls.append(
            {
                "url": url,
                "data": data,
                "headers": headers,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        item = self._responses.pop(0) if self._responses else self._default
        if isinstance(item, BaseException):
            raise item
        return item


def _runs(*ids: str) -> list[dict]:
    return [{"run_id": rid, "experiment_id": "exp", "source": "wandb"} for rid in ids]


def _decode_new_runs(call: dict) -> list[dict]:
    return json.loads(gzip.decompress(call["data"]).decode("utf-8"))["new_runs"]


def _shipper(session, **kw) -> Shipper:
    kw.setdefault("sleep", lambda _s: None)
    return Shipper(BASE_URL, TOKEN, session=session, **kw)


def test_ship_clears_batch_on_2xx(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1", "r2"))
    session = FakeSession(default=FakeResponse(200))

    result = _shipper(session).ship(q)

    assert q.is_empty()
    assert result.batches_shipped == 1
    assert result.runs_shipped == 2
    assert result.batches_remaining == 0
    assert len(session.calls) == 1


def test_ship_posts_gzipped_new_runs_with_bearer_auth(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    session = FakeSession()

    _shipper(session).ship(q)

    call = session.calls[0]
    assert call["url"] == BASE_URL + INGEST_PATH
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["Content-Encoding"] == "gzip"
    assert call["headers"]["Content-Type"] == "application/json"
    assert _decode_new_runs(call) == _runs("r1")


def test_ship_chunks_batches_larger_than_chunk_size(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs(*[f"r{i}" for i in range(30)]))
    session = FakeSession(default=FakeResponse(200))

    result = _shipper(session, chunk_size=25).ship(q)

    assert len(session.calls) == 2
    assert [len(_decode_new_runs(c)) for c in session.calls] == [25, 5]
    assert q.is_empty()
    assert result.runs_shipped == 30


def test_ship_retries_with_backoff_then_succeeds(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    session = FakeSession(responses=[
        requests.ConnectionError("boom"),
        FakeResponse(500),
        FakeResponse(200),
    ])
    sleeps: list[float] = []

    result = _shipper(session, max_attempts=3, backoff=(1, 3), sleep=sleeps.append).ship(q)

    assert q.is_empty()
    assert result.batches_shipped == 1
    assert len(session.calls) == 3
    assert sleeps == [1, 3]  # backoff[0] after attempt 0, backoff[1] after attempt 1


def test_ship_leaves_batch_after_exhausting_retries(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    session = FakeSession(default=FakeResponse(503))
    sleeps: list[float] = []

    result = _shipper(session, max_attempts=2, backoff=(1,), sleep=sleeps.append).ship(q)

    assert not q.is_empty()
    assert result.batches_shipped == 0
    assert result.batches_remaining == 1
    assert len(session.calls) == 2  # max_attempts, no more
    assert sleeps == [1]  # one backoff between the two attempts


def test_ship_does_not_retry_permanent_4xx(tmp_path: Path):
    # A permanent client error (e.g. 400) is terminal: one POST, no retries and
    # no backoff sleeps. The batch stays on disk (never silently dropped); the
    # server merges by run_id so a later re-send can't duplicate.
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    session = FakeSession(default=FakeResponse(400))
    sleeps: list[float] = []

    result = _shipper(session, max_attempts=3, backoff=(1, 3), sleep=sleeps.append).ship(q)

    assert len(session.calls) == 1  # no retries burned on a permanent error
    assert sleeps == []  # and no backoff slept
    assert result.batches_shipped == 0
    assert not q.is_empty()


def test_ship_retries_transient_429_and_503_then_succeeds(tmp_path: Path):
    # 429 (rate limit) and 503 (transient server error) are retryable — keep
    # trying within the attempt budget until a 2xx clears the batch.
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    session = FakeSession(responses=[
        FakeResponse(429),
        FakeResponse(503),
        FakeResponse(200),
    ])
    sleeps: list[float] = []

    result = _shipper(session, max_attempts=3, backoff=(1, 3), sleep=sleeps.append).ship(q)

    assert q.is_empty()
    assert result.batches_shipped == 1
    assert len(session.calls) == 3  # 429, 503, then 200
    assert sleeps == [1, 3]  # both transient failures backed off


def test_ship_stops_at_first_unshippable_batch(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("first"))
    q.enqueue(_runs("second"))
    # First chunk 2xx, then 500 forever -> first batch clears, second is stuck.
    session = FakeSession(responses=[FakeResponse(200)], default=FakeResponse(500))

    result = _shipper(session, max_attempts=1).ship(q)

    assert result.batches_shipped == 1
    assert result.batches_remaining == 1
    remaining = q.batches()
    assert remaining[0].runs[0]["run_id"] == "second"


def test_ship_retries_with_empty_backoff_sleeps_zero(tmp_path: Path):
    # An empty backoff means "retry immediately": no IndexError from backoff[-1],
    # just sleep(0) between attempts. The default (1, 3) hides this off-by-one.
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    session = FakeSession(responses=[FakeResponse(503), FakeResponse(200)])
    sleeps: list[float] = []

    result = _shipper(session, max_attempts=3, backoff=(), sleep=sleeps.append).ship(q)

    assert q.is_empty()
    assert result.batches_shipped == 1
    assert len(session.calls) == 2  # 503 then 200, no crash
    assert sleeps == [0]  # retried immediately


def test_init_rejects_non_positive_chunk_size():
    # chunk_size=0 would later blow up in range(0, n, 0); fail fast and clearly.
    with pytest.raises(ValueError, match="chunk_size"):
        Shipper(BASE_URL, TOKEN, chunk_size=0)


def test_init_rejects_non_positive_max_attempts():
    # max_attempts=0 would silently make zero POSTs; fail fast and clearly.
    with pytest.raises(ValueError, match="max_attempts"):
        Shipper(BASE_URL, TOKEN, max_attempts=0)


def test_ship_empty_queue_makes_no_requests(tmp_path: Path):
    q = SpoolQueue(tmp_path)
    session = FakeSession()

    result = _shipper(session).ship(q)

    assert session.calls == []
    assert result.batches_shipped == 0
    assert result.runs_shipped == 0


def test_partial_batch_is_left_and_fully_reshipped_next_cycle(tmp_path: Path):
    # A batch is atomic: if chunk 2 fails, the whole batch stays and is
    # re-sent in full next cycle. The server's merge-by-run_id makes the
    # repeated chunk 1 harmless.
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs(*[f"r{i}" for i in range(30)]))  # 2 chunks at size 25

    failing = FakeSession(responses=[FakeResponse(200), FakeResponse(500)])
    result = _shipper(failing, chunk_size=25, max_attempts=1).ship(q)
    assert result.batches_shipped == 0
    assert not q.is_empty()

    ok = FakeSession(default=FakeResponse(200))
    result2 = _shipper(ok, chunk_size=25).ship(q)
    assert result2.batches_shipped == 1
    assert q.is_empty()
    assert len(ok.calls) == 2  # both chunks re-sent, including the previously-OK one


def test_ship_treats_3xx_as_permanent_without_retry(tmp_path: Path):
    # A 3xx (e.g. a 302 from a proxy enforcing https) won't self-heal on retry;
    # treat it as permanent — one POST, no backoff — and leave the batch on disk.
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    session = FakeSession(default=FakeResponse(302))
    sleeps: list[float] = []

    result = _shipper(session, max_attempts=3, backoff=(1, 3), sleep=sleeps.append).ship(q)

    assert len(session.calls) == 1  # no retries burned on a redirect
    assert sleeps == []
    assert result.batches_shipped == 0
    assert not q.is_empty()


def test_ship_disables_redirects_on_the_ingest_post(tmp_path: Path):
    # requests follows POST redirects by default, so a misrouted request that 302s
    # to a 200 page would look delivered and silently drop the batch. The POST must
    # set allow_redirects=False so the raw 302 reaches the non-delivered branch and
    # the batch stays on disk for the next cycle.
    q = SpoolQueue(tmp_path)
    q.enqueue(_runs("r1"))
    session = FakeSession(default=FakeResponse(302))

    result = _shipper(session, max_attempts=1).ship(q)

    assert session.calls[0]["allow_redirects"] is False
    assert result.batches_shipped == 0
    assert not q.is_empty()  # the redirect was not counted as a delivery
