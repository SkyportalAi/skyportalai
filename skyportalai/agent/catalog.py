"""
Catalog persistence and run-diffing.

The catalog (``existing_experiments.json``) records which runs have already
been seen so scanners can skip finished runs that were already shipped.
Ported from skyportal-website ``observability_agent/main.py``; the run loop /
CLI that also lived there is deferred to P1 (Reliability).

No Django imports -- this runs on remote hosts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .scrapers.base_scanner import Catalog, RunData, iso_now


def load_catalog(path: Path) -> Catalog:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"last_updated": None, "experiments": []}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Catalog is not valid JSON: {path}") from exc


def save_catalog(path: Path, catalog: Catalog) -> None:
    """Persist the catalog atomically (temp file + ``replace``).

    A direct write can leave a truncated/corrupt file if the process is
    interrupted mid-write, which would then trip ``load_catalog``'s
    ``JSONDecodeError`` path on the next run. Writing to a sibling temp file
    and atomically renaming it gives all-or-nothing commit semantics.
    """
    # A cold start whose first cycle finds no runs never calls queue.enqueue()
    # (which is what would create the state dir), so ensure the parent exists here
    # too — otherwise the temp write below raises FileNotFoundError every interval.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2)
        fh.flush()
    tmp.replace(path)


def _fallback_exp_id(exp: dict[str, Any]) -> str:
    """Derive a slash-free experiment id for legacy entries lacking ``id``.

    ``experiment_id`` is interpolated into the R2 object key, so it must stay
    slash-free (a ``/`` creates a phantom intermediate folder in storage / the
    UI listing). Join entity and project with ``_`` rather than ``/``.
    """
    return f"{exp.get('entity', 'unknown')}_{exp.get('project', 'unknown')}"


def upsert_runs(catalog: Catalog, runs: list[RunData]) -> Catalog:
    """Merge new runs into the catalog, keyed by experiment_id + run_id."""
    experiments: dict[str, dict[str, Any]] = {}
    for exp in catalog.get("experiments", []):
        exp_id = exp.get("id") or _fallback_exp_id(exp)
        experiments[exp_id] = exp

    for run in runs:
        exp_id = run.get("experiment_id", "unknown")
        if exp_id not in experiments:
            experiments[exp_id] = {
                "id": exp_id,
                "entity": run.get("entity", "unknown"),
                "project": run.get("project", "unknown"),
                "source": run.get("source", "unknown"),
                "runs": [],
            }

        run_id = run.get("run_id")
        existing_runs = experiments[exp_id]["runs"]
        for idx, existing in enumerate(existing_runs):
            if existing.get("run_id") == run_id:
                existing_runs[idx] = run
                break
        else:
            existing_runs.append(run)

    catalog["experiments"] = list(experiments.values())
    catalog["last_updated"] = iso_now()
    return catalog


def assign_run_indices(catalog: Catalog, new_runs: list[RunData]) -> None:
    """Assign sequential ``run_index`` to new runs within each experiment."""
    exp_counts: dict[str, int] = {}
    for exp in catalog.get("experiments", []):
        exp_id = exp.get("id") or _fallback_exp_id(exp)
        exp_counts[exp_id] = len(exp.get("runs", []))

    for run in new_runs:
        exp_id = run.get("experiment_id", "unknown")
        current = exp_counts.get(exp_id, 0)
        run["run_index"] = current
        exp_counts[exp_id] = current + 1
