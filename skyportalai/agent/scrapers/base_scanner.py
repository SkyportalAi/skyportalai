"""
Abstract base class for experiment scanners.

Each ML platform (W&B, MLflow, etc.) implements a scanner that knows
how to discover runs on the local filesystem and extract structured
data from them. The scanner interface normalises the output so the
transport layer (the shipper) doesn't care about the source.
"""

from __future__ import annotations

import abc
import datetime as dt
import re
from pathlib import Path
from typing import Any

# ── Canonical run data shape ──────────────────────────────────────────
#
# Every scanner returns a list of dicts matching this shape.  Keys are
# intentionally kept flat to avoid nested‑dict gymnastics on the
# receiving end.  Optional keys may be absent or None.
#
# {
#     "source":         "wandb" | "mlflow",
#     "run_id":         str,
#     "run_name":       str,
#     "experiment_id":  str,          # path-safe, slash-free (interpolated into
#                                     # the R2 object key); entity is a separate field
#     "status":         "running" | "finished" | "failed" | "killed" | "unknown",
#     "start_time":     float | str | None, # epoch seconds; scanners may emit "None"
#     "end_time":       float | str | None, # after value sanitisation
#     "config":         dict,         # hyperparameters
#     "summary":        dict,         # final metric values
#     "history":        list[dict],   # per‑step metrics (tier 2 — may be empty)
#     "system_metrics": list[dict],   # GPU/CPU during training (tier 2)
#     "tags":           list[str] | dict,
#     "script_path":    str | None,
#     "script_content": str | None,
#     "mode":           str | None,   # e.g. "offline" / "online" for wandb
#     "path":           str,          # local path to run directory
#     "run_index":      int | None,   # assigned by the catalog, not the scanner
# }

RunData = dict[str, Any]
Catalog = dict[str, Any]

REDACTED_VALUE = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:"
    r"api_?key|apikey|access_?token|refresh_?token|auth_?token|bearer_?token|"
    r"session_?token|client_?secret|private_?key|access_?key|password|passwd|"
    r"secret|credentials?|authorization|cookie"
    r")(?:$|_)",
    re.IGNORECASE,
)


def redact_sensitive_values(value: Any) -> Any:
    """Recursively replace values whose keys look like credentials.

    Experiment configs and tags are user-controlled and frequently contain
    service tokens. Redacting centrally before catalog/queue persistence keeps
    those values off disk and out of ingest payloads while preserving the rest
    of the run metadata.
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
            normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_")
            redacted[key] = (
                REDACTED_VALUE
                if _SENSITIVE_KEY.search(normalized)
                else redact_sensitive_values(item)
            )
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_values(item) for item in value)
    return value


def iso_now() -> str:
    """UTC ISO-8601 timestamp."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _default_search_roots() -> list[Path]:
    """Common directories to scan for ML experiment data.

    Avoids scanning '/' which is expensive on large filesystems.
    Override the search location via the ``SKYPORTAL_AGENT_WANDB_DIR`` /
    ``SKYPORTAL_AGENT_MLFLOW_DIR`` env vars (see :class:`AgentConfig`).
    """
    roots = [
        Path("/home"),
        Path("/data"),
        Path("/workspace"),
        Path("/opt"),
    ]
    try:
        roots.insert(0, Path.home())
    except Exception:
        pass
    return roots


DEFAULT_SEARCH_ROOTS: list[Path] = _default_search_roots()


class BaseScanner(abc.ABC):
    """Interface that every experiment scanner must implement."""

    # ── Identity ──────────────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def source_name(self) -> str:
        """Short identifier for this source, e.g. ``"wandb"`` or ``"mlflow"``."""

    @abc.abstractmethod
    def get_dependencies(self) -> list[str]:
        """Pip packages required on the host, e.g. ``["wandb"]``."""

    # ── Detection ─────────────────────────────────────────────────────

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return *True* if the required library can be imported."""

    @abc.abstractmethod
    def find_root_dirs(self, search_root: Path | None = None) -> list[Path]:
        """Locate root directories containing experiment data.

        When *search_root* is given, only that directory is scanned. When
        it is ``None`` the scanner falls back to a bounded set of common
        locations (:data:`DEFAULT_SEARCH_ROOTS`) rather than walking the
        whole filesystem, which would be prohibitively expensive.
        """

    # ── Extraction ────────────────────────────────────────────────────

    @abc.abstractmethod
    def discover_runs(
        self,
        root_dirs: list[Path],
        catalog: Catalog,
        log_path: Path | None = None,
    ) -> list[RunData]:
        """Discover new or updated runs under *root_dirs*.

        *catalog* is the previously‑persisted ``existing_experiments.json``
        so the scanner can skip finished runs that have already been sent.

        Returns a list of :pydata:`RunData` dicts in the canonical shape.
        """

    # ── Helpers available to subclasses ───────────────────────────────

    @staticmethod
    def build_existing_runs_map(catalog: Catalog) -> dict[str, RunData]:
        """Index catalogued runs by ``run_id`` for fast lookups."""
        runs_map: dict[str, RunData] = {}
        for exp in catalog.get("experiments", []):
            for run in exp.get("runs", []):
                run_id = run.get("run_id")
                if run_id:
                    runs_map[run_id] = run
        return runs_map

    @staticmethod
    def is_finished(run: RunData) -> bool:
        """Check whether a catalogued run is considered finished."""
        end = run.get("end_time")
        return end is not None and end != "None"
