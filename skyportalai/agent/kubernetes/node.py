"""Node role: read this node's CPU, memory, disk, load and GPUs, every cycle, on every node.

Runs as a DaemonSet pod. It reads the host, not the Kubernetes API: psutil
against the host's /proc (mounted read-only) and NVML for the GPUs. Cluster-wide
state (pods, events) comes from the cluster role, so this role needs no
Kubernetes permissions at all. Values are sent as read; the server stores them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from ..scrapers.base_scanner import iso_now

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ROLE = "node"
# psutil's cpu_percent over a short window; the first call without one returns 0.
_CPU_SAMPLE_SECONDS = 1.0


class NodeRole:
    """Samples one node's host metrics."""

    kind = ROLE

    def __init__(
        self,
        node_name: str,
        *,
        host_proc: Path | None = None,
        disk_path: Path | None = None,
        read_gpus: Callable[[], list[dict]] | None = None,
    ):
        if not node_name:
            raise ValueError("node_name is required for the node role (set from spec.nodeName)")
        self.node_name = node_name
        self.host_proc = host_proc
        # statvfs of a directory on the host filesystem reports that filesystem's
        # usage. The node spool is a hostPath under /var/lib, so pointing here at it
        # measures the node's disk without mounting the host root.
        self.disk_path = disk_path
        self._read_gpus = read_gpus or read_nvml_gpus

    def collect(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "role": ROLE,
            "collected_at": iso_now(),
            "node": {"name": self.node_name, **self._host_metrics(), "gpus": self._read_gpus()},
        }

    def apply_reply(self, reply: dict) -> None:
        """The node role takes no instructions beyond the interval."""

    def next_delay(self, interval_seconds: float) -> float:
        return interval_seconds

    def _host_metrics(self) -> dict:
        import psutil

        if self.host_proc is not None:
            psutil.PROCFS_PATH = str(self.host_proc)
        memory = psutil.virtual_memory()
        metrics = {
            "cpu_usage_percent": psutil.cpu_percent(interval=_CPU_SAMPLE_SECONDS),
            "cpu_cores": psutil.cpu_count(logical=True),
            # getloadavg reads the kernel's load, which is host-wide inside a container.
            "load_average_1m": os.getloadavg()[0],
            "memory_total_bytes": memory.total,
            "memory_used_bytes": memory.total - memory.available,
            "disk_total_bytes": None,
            "disk_used_bytes": None,
        }
        if self.disk_path is not None:
            try:
                disk = psutil.disk_usage(str(self.disk_path))
                metrics["disk_total_bytes"], metrics["disk_used_bytes"] = disk.total, disk.used
            except OSError as exc:
                logger.warning("Could not read disk usage at %s: %s", self.disk_path, exc)
        return metrics


def read_nvml_gpus() -> list[dict]:
    """Every GPU NVML can see, or [] on a node without NVIDIA GPUs or driver access."""
    try:
        import pynvml
    except ImportError:
        return []
    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError:
        return []  # no driver in reach: a CPU node, or the pod wasn't given GPU access
    try:
        gpus = []
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            gpus.append({
                "index": index,
                "name": name.decode() if isinstance(name, bytes) else name,
                "utilization_pct": pynvml.nvmlDeviceGetUtilizationRates(handle).gpu,
                "memory_used_bytes": memory.used,
                "memory_total_bytes": memory.total,
            })
        return gpus
    except pynvml.NVMLError as exc:
        logger.warning("NVML read failed: %s", exc)
        return []
    finally:
        pynvml.nvmlShutdown()
