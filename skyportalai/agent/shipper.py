"""Shipper — gzip chunked POST delivery with retry/backoff.

Ports the proven semantics of the generated uploader (`run_agent.py`): the
pending queue is POSTed to the ingest endpoint in fixed-size chunks, each
gzip-compressed, with bounded in-cycle retry on *transient* failures (5xx, 408,
429, network errors) — permanent 4xx fail fast so a poison batch can't burn the
retry budget. A batch is cleared only on a full 2xx delivery; anything else
leaves it on disk for the next cycle.

POST retry lives here, not in the SDK transport, which retries idempotent GETs
only. Server-side ingest is idempotent (merges by ``run_id``), so re-sending a
chunk after a partial success cannot duplicate data.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

import requests

from .._version import __version__
from .queue import SpoolQueue

logger = logging.getLogger(__name__)

INGEST_PATH = "/agent/api/observability/ingest/"

# Mirror the generated uploader's caps: chunk so each request finishes well
# under the router/socket timeout even with large history arrays.
DEFAULT_CHUNK_SIZE = 25
DEFAULT_MAX_ATTEMPTS = 3  # first try + 2 retries
DEFAULT_BACKOFF = (1, 3)  # in-cycle backoff seconds, clamped to the last value
DEFAULT_TIMEOUT = 60

# Retry only transient failures. Any 5xx plus the two transient 4xx (408 Request
# Timeout, 429 Too Many Requests) are worth re-sending; every other 4xx is a
# permanent client error (malformed body, bad/expired token, wrong endpoint,
# unprocessable payload) that fails identically on every retry, so we surface it
# at once instead of spending the in-cycle retry budget and backoff sleeps on it.
RETRYABLE_STATUS_CODES = frozenset({408, 429})


class _PostOutcome(Enum):
    DELIVERED = "delivered"  # 2xx — chunk accepted, clear the batch
    RETRYABLE = "retryable"  # 5xx / 408 / 429 / network error — worth another try
    PERMANENT = "permanent"  # other 4xx — resending cannot help, stop now


@dataclass(frozen=True)
class ShipResult:
    batches_shipped: int
    runs_shipped: int
    batches_remaining: int


class Shipper:
    """Drains a :class:`SpoolQueue` to the ingest endpoint, owning POST retry."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: requests.Session | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self.url = base_url.rstrip("/") + INGEST_PATH
        self.token = token
        self.session = session or requests.Session()
        self.chunk_size = chunk_size
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.timeout = timeout
        self._sleep = sleep

    @classmethod
    def from_client(cls, client, **kwargs) -> "Shipper":
        """Build on the SDK transport: reuse its pooled session, base_url, token."""
        return cls(client.base_url, client.api_key, session=client._session, **kwargs)

    def ship(self, queue: SpoolQueue) -> ShipResult:
        """Deliver pending batches FIFO; clear each on full 2xx, stop on failure."""
        batches_shipped = 0
        runs_shipped = 0
        for batch in queue.batches():
            if not self._ship_batch(batch.runs):
                break  # leave this batch and everything after it for next cycle
            queue.remove(batch.batch_id)
            batches_shipped += 1
            runs_shipped += len(batch.runs)
        return ShipResult(batches_shipped, runs_shipped, len(queue))

    def _ship_batch(self, runs: list[dict]) -> bool:
        for start in range(0, len(runs), self.chunk_size):
            if not self._post_chunk_with_retry(runs[start : start + self.chunk_size]):
                return False
        return True

    def _post_chunk_with_retry(self, chunk: list[dict]) -> bool:
        for attempt in range(self.max_attempts):
            outcome = self._post_chunk(chunk)
            if outcome is _PostOutcome.DELIVERED:
                return True
            if outcome is _PostOutcome.PERMANENT:
                return False  # permanent client error: retrying cannot help
            if attempt < self.max_attempts - 1:
                delay = self.backoff[min(attempt, len(self.backoff) - 1)] if self.backoff else 0
                self._sleep(delay)
        logger.warning(
            "Chunk of %d run(s) undelivered after %d attempt(s)", len(chunk), self.max_attempts
        )
        return False

    def _post_chunk(self, chunk: list[dict]) -> _PostOutcome:
        body = gzip.compress(json.dumps({"new_runs": chunk}).encode("utf-8"))
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "User-Agent": f"skyportal-agent/{__version__}",
        }
        try:
            # allow_redirects=False: requests follows POST redirects by default, so a
            # misrouted request that 3xx'd to a 200 page would look delivered and drop
            # the batch. Keeping the raw 3xx routes it to the misconfiguration branch.
            resp = self.session.post(self.url, data=body, headers=headers, timeout=self.timeout, allow_redirects=False)
        except (requests.ConnectionError, requests.Timeout) as exc:
            logger.warning("Ingest POST failed: %s", type(exc).__name__)
            return _PostOutcome.RETRYABLE
        try:
            status = resp.status_code
            if 200 <= status < 300:
                return _PostOutcome.DELIVERED
            if status in RETRYABLE_STATUS_CODES or 500 <= status < 600:
                logger.warning("Ingest POST rejected (retryable): HTTP %d", status)
                return _PostOutcome.RETRYABLE
            if 400 <= status < 500:
                logger.warning(
                    "Ingest POST rejected (permanent client error): HTTP %d; not retrying", status
                )
                return _PostOutcome.PERMANENT
            # 1xx/3xx: not retryable and won't self-heal — surface as misconfiguration.
            logger.error(
                "Ingest POST returned unexpected HTTP %d (redirect/informational); "
                "check base_url/proxy",
                status,
            )
            return _PostOutcome.PERMANENT
        finally:
            resp.close()
