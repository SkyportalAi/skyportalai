"""Node role: upload this node's raw host readings, every cycle, on every node.

Runs as a DaemonSet pod. It reads the host, not the Kubernetes API, so it needs no
Kubernetes permissions at all. Everything is sent as read and nothing is derived
here: the kernel's /proc files as text, the whole statvfs of the node's disk, and
nvidia-smi's full XML report. The server parses them and computes every metric
(CPU from consecutive /proc/stat, used memory, GPU utilization), so what a metric
means is decided in one place, and the raw readings are kept in R2 as uploaded.
"""

from __future__ import annotations

import errno
import logging
import os
from collections.abc import Callable
from pathlib import Path

from ..scrapers.base_scanner import iso_now
from .kubectl import run_process
from .kubelet import STATS_SUMMARY_PATH, KubeletClient

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ROLE = "node"

# Fixed here, never taken from the server: the host's /proc also holds every
# process's environ and cmdline, so a server-chosen path could read other workloads'
# secrets. The system-wide files below hold no per-process data.
PROC_FILES = (
    "stat", "meminfo", "loadavg", "uptime", "vmstat", "diskstats", "cpuinfo",
    "pressure/cpu", "pressure/memory", "pressure/io",
    # /proc/net resolves through /proc/self, i.e. this pod's network namespace; PID 1's
    # is the node's.
    "1/net/dev",
)
# Everything the driver reports. Present only when the pod runs with the NVIDIA
# runtime (kubernetes.node.gpu.runtimeClassName); elsewhere it exits 127, which is
# itself the "no GPUs visible" reading.
NVIDIA_SMI = ["nvidia-smi", "-q", "-x"]
# /proc/cpuinfo is ~1 KB per core, so even a 512-core node stays far below this.
MAX_FILE_CHARS = 4 * 1024 * 1024


class NodeRole:
    """Reads one node's raw host readings."""

    kind = ROLE

    def __init__(
        self,
        node_name: str,
        *,
        host_proc: Path | None = None,
        disk_path: Path | None = None,
        run: Callable[[list[str]], dict] = run_process,
        kubelet: KubeletClient | None = None,
    ):
        if not node_name:
            raise ValueError("node_name is required for the node role (set from spec.nodeName)")
        self.node_name = node_name
        self.host_proc = Path(host_proc) if host_proc is not None else Path("/proc")
        # statvfs of a directory on the host filesystem reports that filesystem. The
        # node spool is a hostPath under /var/lib, so pointing here at it reads the
        # node's disk without mounting the host root.
        self.disk_path = disk_path
        self._run = run
        self._kubelet = kubelet

    def collect(self) -> dict:
        files, unreadable = self._read_proc_files()
        statvfs = self._read_statvfs(unreadable)
        return {
            "schema_version": SCHEMA_VERSION,
            "role": ROLE,
            "collected_at": iso_now(),
            "node": {
                "name": self.node_name,
                "files": files,
                "statvfs": statvfs,
                "commands": [self._run(list(NVIDIA_SMI))],
                # Keyed by the kubelet path, raw body, like files. An additive key: a server
                # that predates it ignores it, and the /proc readings above still land.
                "kubelet": self._read_kubelet(unreadable),
                # What was asked for and could not be read, so a gap is never silent.
                "unreadable": unreadable,
            },
        }

    def apply_reply(self, reply: dict) -> None:
        """The node role takes no instructions beyond the interval."""

    def next_delay(self, interval_seconds: float) -> float:
        return interval_seconds

    def _read_proc_files(self) -> tuple[dict[str, str], dict[str, str]]:
        files: dict[str, str] = {}
        unreadable: dict[str, str] = {}
        for rel in PROC_FILES:
            # Keyed by the node's own path, not this pod's mount point.
            name = f"/proc/{rel}"
            try:
                text = (self.host_proc / rel).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                unreadable[name] = _errno_name(exc)
                continue
            if len(text) > MAX_FILE_CHARS:
                unreadable[name] = "EFBIG"
                continue
            files[name] = text
        return files, unreadable

    def _read_kubelet(self, unreadable: dict[str, str]) -> dict[str, str]:
        if self._kubelet is None:
            return {}
        body, reason = self._kubelet.stats_summary(MAX_FILE_CHARS)
        if body is None:
            unreadable[f"kubelet:{STATS_SUMMARY_PATH}"] = reason or "unknown"
            return {}
        return {STATS_SUMMARY_PATH: body}

    def _read_statvfs(self, unreadable: dict[str, str]) -> dict[str, dict[str, int]]:
        if self.disk_path is None:
            return {}
        try:
            fs = os.statvfs(self.disk_path)
        except OSError as exc:
            unreadable[str(self.disk_path)] = _errno_name(exc)
            return {}
        # The whole struct: which of free/available counts as "used" is the server's call.
        return {str(self.disk_path): {field: getattr(fs, field) for field in dir(fs) if field.startswith("f_")}}


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno, type(exc).__name__) if exc.errno else type(exc).__name__
