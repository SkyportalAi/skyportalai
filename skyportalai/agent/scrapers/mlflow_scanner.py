"""
MLflow scanner adapter.

Discovers and extracts experiment data from local MLflow ``mlruns/``
directories.  Parses ``meta.yaml``, ``metrics/``, ``params/``, and
``tags/`` using only the filesystem — **no mlflow pip package required**.

Directory layout expected::

    mlruns/
        {experiment_id}/
            meta.yaml                   # experiment metadata
            {run_id}/
                meta.yaml               # run metadata (status, times)
                metrics/{metric_name}   # one line per step: ts value step
                params/{param_name}     # single-line value
                tags/{tag_key}          # single-line value
                artifacts/              # user artifacts (not ingested)

No Django imports -- this runs on remote hosts.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .base_scanner import DEFAULT_SEARCH_ROOTS, BaseScanner, Catalog, RunData, iso_now

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


# ── Status enum (mirrors mlflow.entities.RunStatus) ───────────────────

_STATUS_MAP: dict[int, str] = {
    1: "running",
    2: "running",   # SCHEDULED treated as running
    3: "finished",
    4: "failed",
    5: "killed",
}


# ── Logging helper ────────────────────────────────────────────────────

def _log(msg: str, log_path: Path | None) -> None:
    line = f"[{iso_now()}] {msg}"
    print(line)
    if log_path:
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


# ── File helpers ──────────────────────────────────────────────────────

def _read_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists() or yaml is None:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception:
        return None


def _read_text(path: Path) -> str | None:
    """Read a single-line text file (params/tags format)."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return None


def _read_dir_values(directory: Path) -> dict[str, str]:
    """Read all single-value files from a directory (params/ or tags/)."""
    result: dict[str, str] = {}
    if not directory.exists():
        return result
    try:
        for entry in directory.iterdir():
            if entry.is_file():
                val = _read_text(entry)
                if val is not None:
                    result[entry.name] = val
            elif entry.is_dir():
                # Handle nested keys like optimizer/lr
                for sub in entry.rglob("*"):
                    if sub.is_file():
                        key = str(sub.relative_to(directory))
                        val = _read_text(sub)
                        if val is not None:
                            result[key] = val
    except Exception:
        pass
    return result


def _parse_metrics_file(path: Path) -> list[dict[str, Any]]:
    """Parse a metrics file: each line is ``timestamp value step``."""
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split()
                if len(parts) >= 2:
                    timestamp = int(parts[0])
                    try:
                        value: int | float = int(parts[1])
                    except ValueError:
                        value = float(parts[1])
                    step = int(parts[2]) if len(parts) >= 3 else 0
                    entries.append({
                        "timestamp": timestamp,
                        "value": value,
                        "step": step,
                    })
    except Exception:
        pass
    return entries


def _read_all_metrics(metrics_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read all metrics, returning (summary, history).

    *summary* contains the last-logged value of each metric.
    *history* is a list of step dicts (one per unique step across all metrics).
    """
    summary: dict[str, Any] = {}
    steps_map: dict[int, dict[str, Any]] = {}

    if not metrics_dir.exists():
        return summary, []

    try:
        for entry in metrics_dir.iterdir():
            if not entry.is_file():
                continue
            metric_name = entry.name
            entries = _parse_metrics_file(entry)
            if entries:
                summary[metric_name] = entries[-1]["value"]
                for e in entries:
                    step = e["step"]
                    if step not in steps_map:
                        steps_map[step] = {"_step": step}
                    steps_map[step][metric_name] = e["value"]
    except Exception:
        pass

    history = [steps_map[s] for s in sorted(steps_map)]
    return summary, history


def _is_mlflow_run_dir(path: Path) -> bool:
    """Check if a directory looks like an MLflow run (has meta.yaml with run_id)."""
    meta = path / "meta.yaml"
    return meta.exists()


def _find_mlflow_runs(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Find all MLflow run directories under a mlruns root.

    Returns a list of ``(run_dir, experiment_meta)`` tuples.
    """
    if not root.exists():
        return []

    results: list[tuple[Path, dict[str, Any]]] = []

    try:
        for exp_dir in sorted(root.iterdir()):
            if not exp_dir.is_dir():
                continue
            # Skip .trash and models directories
            if exp_dir.name.startswith(".") or exp_dir.name == "models":
                continue

            exp_meta = _read_yaml(exp_dir / "meta.yaml") or {}

            for run_dir in sorted(exp_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                if _is_mlflow_run_dir(run_dir):
                    results.append((run_dir, exp_meta))
    except Exception:
        pass

    return results


def _scan_system_for_mlruns_dirs(log_path: Path | None) -> list[Path]:
    """Search common filesystem locations for ``mlruns/`` directories.

    Avoids a full-root ``find /`` scan (expensive on large filesystems) and
    instead searches:
    - Environment-hinted locations (``MLFLOW_TRACKING_URI``, etc.)
    - The current user's home directory and common project sub-directories
    - A small set of common ML / data mount points
    """
    _log("Scanning common locations for 'mlruns' directories...", log_path)

    candidate_roots: set[Path] = set()

    # 1. Environment hints. These may be plain absolute paths (``/data/mlruns``)
    #    or local file URIs (``file:///data/mlruns``); both point at the local
    #    filesystem and must be considered. Non-local schemes (http, databricks,
    #    s3, ...) have no local subtree to scan and are skipped.
    for var in ("MLFLOW_TRACKING_URI", "MLFLOW_ARTIFACT_ROOT"):
        value = os.environ.get(var)
        if not value:
            continue
        if value.startswith("file://"):
            value = unquote(urlparse(value).path)
        path = Path(value).expanduser()
        if path.is_absolute():
            candidate_roots.add(path)

    # 2. Current user's home directory and common project sub-directories
    try:
        home = Path.home()
        candidate_roots.update([home, home / "mlruns", home / "work", home / "projects"])
    except Exception:
        pass

    # 3. Start from DEFAULT_SEARCH_ROOTS (restricted set, not '/')
    for root in DEFAULT_SEARCH_ROOTS:
        candidate_roots.add(root)

    found_dirs: set[Path] = set()
    for root in candidate_roots:
        try:
            if not root.exists():
                continue
            # If the root itself is an "mlruns" directory, include it
            if root.name == "mlruns" and root.is_dir():
                found_dirs.add(root)
                continue
            # Otherwise, use find restricted to this subtree. Canonicalizing to
            # an absolute path prevents a dash-prefixed relative path from being
            # interpreted as a find option or expression.
            safe_root = root.resolve(strict=False)
            cmd = ["find", str(safe_root), "-maxdepth", "5", "-name", "mlruns", "-type", "d"]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
            for line in result.stdout.splitlines():
                if line.strip():
                    found_dirs.add(Path(line.strip()))
        except Exception:
            pass

    paths = sorted(found_dirs)
    _log(f"Found {len(paths)} 'mlruns' directories in common locations.", log_path)
    return paths


def _build_run_info(
    run_dir: Path,
    exp_meta: dict[str, Any],
    log_path: Path | None,
) -> RunData | None:
    """Extract structured data from a single MLflow run directory."""
    meta = _read_yaml(run_dir / "meta.yaml")
    if not meta:
        _log(f"No meta.yaml in {run_dir}, skipping.", log_path)
        return None

    run_id = meta.get("run_id") or meta.get("run_uuid") or run_dir.name
    experiment_id = meta.get("experiment_id") or exp_meta.get("experiment_id") or "0"
    experiment_name = exp_meta.get("name") or f"experiment-{experiment_id}"

    # Timestamps (MLflow stores milliseconds)
    start_time_ms = meta.get("start_time")
    end_time_ms = meta.get("end_time")
    start_time = start_time_ms / 1000.0 if start_time_ms else None
    end_time = end_time_ms / 1000.0 if end_time_ms and end_time_ms > 0 else None

    # Status from integer enum
    status_int = meta.get("status")
    status = _STATUS_MAP.get(status_int, "unknown") if isinstance(status_int, int) else "unknown"

    # Skip deleted runs
    lifecycle = meta.get("lifecycle_stage", "active")
    if lifecycle == "deleted":
        return None

    # Read params, tags, metrics
    params = _read_dir_values(run_dir / "params")
    tags = _read_dir_values(run_dir / "tags")
    summary, history = _read_all_metrics(run_dir / "metrics")

    # Extract useful info from tags
    run_name = (
        meta.get("run_name")
        or tags.get("mlflow.runName")
        or run_dir.name
    )
    script_path = tags.get("mlflow.source.name")
    user = tags.get("mlflow.user") or meta.get("user_id")
    parent_run_id = tags.get("mlflow.parentRunId")
    git_commit = tags.get("mlflow.source.git.commit")

    # Build config from params + selected tags
    config: dict[str, Any] = dict(params)
    if git_commit:
        config["git_commit"] = git_commit
    if parent_run_id:
        config["parent_run_id"] = parent_run_id

    # Build experiment_id for the canonical shape. It must be path-safe /
    # slash-free: the server interpolates it into the R2 object key
    # (`OBSERVABILITY/<user>/<server_ip>/<experiment_id>/...`), so a forward
    # slash would create a phantom intermediate "folder" in storage and the
    # UI's experiment listing (mirrors the W&B scanner's note). We keep the
    # human-readable name in `project` and use a sanitised id here.
    exp_id = experiment_name.replace("/", "_")

    _log(
        f"Parsed run {run_dir.name} experiment={experiment_name} "
        f"status={status} params={len(params)} metrics={len(summary)} "
        f"history_steps={len(history)}",
        log_path,
    )

    return {
        "source": "mlflow",
        "run_id": run_id,
        "run_name": run_name,
        "experiment_id": exp_id,
        "entity": user or "unknown",
        "project": experiment_name,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "config": config,
        "summary": summary,
        "history": history,
        "system_metrics": [],
        "tags": tags,
        "script_path": script_path,
        "script_content": None,
        "mode": "local",
        "path": str(run_dir.resolve()),
        "run_index": None,
    }


# ── Scanner class ─────────────────────────────────────────────────────


class MlflowScanner(BaseScanner):
    """Discovers and extracts MLflow experiment runs from the local filesystem.

    Parses the ``mlruns/`` directory structure directly — no ``mlflow``
    pip package required, only ``pyyaml`` for ``meta.yaml`` parsing.
    """

    @property
    def source_name(self) -> str:
        return "mlflow"

    def get_dependencies(self) -> list[str]:
        # Only need pyyaml for meta.yaml parsing; no mlflow package required
        return ["pyyaml"]

    def is_available(self) -> bool:
        # Always available — we parse the filesystem directly, no library needed.
        # We just need pyyaml, which is also required by the W&B scanner.
        return yaml is not None

    def find_root_dirs(self, search_root: Path | None = None) -> list[Path]:
        if search_root is not None:
            return [search_root] if search_root.exists() else []
        return _scan_system_for_mlruns_dirs(log_path=None)

    def discover_runs(
        self,
        root_dirs: list[Path],
        catalog: Catalog,
        log_path: Path | None = None,
    ) -> list[RunData]:
        existing_map = self.build_existing_runs_map(catalog)

        all_runs: list[tuple[Path, dict[str, Any]]] = []
        for root in root_dirs:
            all_runs.extend(_find_mlflow_runs(root))

        if not all_runs:
            _log("No MLflow runs found.", log_path)
            return []

        _log(f"Found {len(all_runs)} MLflow run directories.", log_path)

        new_runs: list[RunData] = []
        skipped = 0

        for run_dir, exp_meta in all_runs:
            run_id = run_dir.name
            if run_id in existing_map and self.is_finished(existing_map[run_id]):
                skipped += 1
                continue

            try:
                info = _build_run_info(run_dir, exp_meta, log_path)
                if not info:
                    continue

                actual_id = info["run_id"]
                if actual_id in existing_map and self.is_finished(existing_map[actual_id]):
                    skipped += 1
                    continue

                new_runs.append(info)
            except Exception as exc:
                _log(f"Failed to parse MLflow run {run_dir.name}: {exc}", log_path)

        _log(f"MLflow scanner: {len(new_runs)} new, {skipped} skipped.", log_path)
        return new_runs
