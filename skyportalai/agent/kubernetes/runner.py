"""Collect -> spool -> ship loop for the Kubernetes roles.

The same disk-backed SpoolQueue and retrying Shipper the experiment scanners use.
Each cycle's upload is one spooled item, so an outage is buffered on disk and
delivered in order when SkyPortal answers again. The server's reply sets the
interval and, for the cluster role, the next cycle's plan.
"""

from __future__ import annotations

import logging
import threading
import uuid

from ..queue import SpoolQueue
from ..shipper import Shipper

logger = logging.getLogger(__name__)

INGEST_PATH = "/agent/api/kubernetes/ingest/"


class KubernetesShipper(Shipper):
    """Ships one upload per request to the Kubernetes ingest endpoint."""

    ingest_path = INGEST_PATH

    def __init__(self, base_url: str, token: str, **kwargs):
        super().__init__(base_url, token, chunk_size=1, **kwargs)
        # The last 2xx reply: the server's interval and, for the cluster role, its plan.
        self.last_response: dict | None = None

    def _body(self, chunk: list[dict]) -> dict:
        return chunk[0]

    def _on_delivered(self, resp) -> None:
        try:
            reply = resp.json()
        except ValueError:
            return
        if isinstance(reply, dict):
            self.last_response = reply


class KubernetesRunner:
    """Runs one role (cluster or node) on the server-set interval until stopped."""

    def __init__(
        self,
        role,
        queue: SpoolQueue,
        shipper: KubernetesShipper,
        interval_seconds: float,
        stop_event: threading.Event | None = None,
    ):
        self.role = role
        self.queue = queue
        self.shipper = shipper
        self.interval_seconds = interval_seconds
        self._stop = stop_event or threading.Event()

    def run_once(self) -> None:
        payload = self.role.collect()
        payload["batch_id"] = uuid.uuid4().hex
        self.queue.enqueue([payload])
        self.shipper.last_response = None
        result = self.shipper.ship(self.queue)
        reply = self.shipper.last_response
        if reply:
            self.role.apply_reply(reply)
            interval = reply.get("interval_seconds")
            if isinstance(interval, int) and interval > 0:
                self.interval_seconds = interval
        if result.batches_remaining:
            logger.warning("%d upload(s) waiting to be delivered", result.batches_remaining)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — one bad cycle must not end the agent
                logger.exception("%s cycle failed", self.role.kind)
            self._stop.wait(self.role.next_delay(self.interval_seconds))

    def stop(self) -> None:
        self._stop.set()
