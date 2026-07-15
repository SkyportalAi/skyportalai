"""MLflow tracking-server (REST) scanner adapter.

Discovers experiment runs from a remote MLflow tracking server over its REST
API instead of the local filesystem. Emits the SAME canonical RunData shape as
the filesystem :class:`MlflowScanner` (``source="mlflow"``) so the catalog and
ingest treat both identically.

Endpoints used (MLflow REST API 2.0):
    GET  {uri}/api/2.0/mlflow/experiments/search   -> experiments (paginated)
    POST {uri}/api/2.0/mlflow/runs/search          -> runs (paginated)

Only ``requests`` is required — no ``mlflow`` pip package.

Follow-ups deferred for later tiers:
    - per-step history via ``GET metrics/get-history`` (history stays ``[]``).
    - auth variety (basic / bearer / databricks token); ``auth_header`` today is
      a plain header mapping merged verbatim into each request.
    - Helm env keys (SKYPORTAL_AGENT_MLFLOW_MODE / _TRACKING_URI) and a
      NetworkPolicy egress rule to the tracking server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from .base_scanner import BaseScanner, Catalog, RunData, iso_now

# MLflow REST status is a STRING enum (unlike the FS meta.yaml integer enum).
_STATUS_MAP: dict[str, str] = {
    "RUNNING": "running",
    "SCHEDULED": "running",
    "FINISHED": "finished",
    "FAILED": "failed",
    "KILLED": "killed",
}

_PAGE_SIZE = 1000
_TIMEOUT = 30


def _as_int(value: Any) -> int | None:
    """Coerce a proto3-JSON int64 field to ``int``.

    MLflow serializes REST responses with protobuf's ``MessageToJson``, and the
    proto3 JSON mapping renders every 64-bit integer field (RunInfo.start_time /
    end_time, Metric.step / timestamp) as a quoted STRING, not a number. Coerce
    tolerantly so arithmetic/comparisons don't blow up; ``None`` on absent/bad.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _log(msg: str, log_path: Path | None) -> None:
    line = f"[{iso_now()}] {msg}"
    print(line)
    if log_path:
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def _latest_metrics(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a metrics list to the latest (highest-step) value per key."""
    summary: dict[str, Any] = {}
    steps: dict[str, int] = {}
    for entry in metrics:
        key = entry.get("key")
        if key is None:
            continue
        step = _as_int(entry.get("step")) or 0
        if key not in summary or step >= steps.get(key, -1):
            summary[key] = entry.get("value")
            steps[key] = step
    return summary


class MlflowRestScanner(BaseScanner):
    """Discovers MLflow runs from a remote tracking server over the REST API."""

    def __init__(
        self,
        tracking_uri: str | None,
        auth_header: dict[str, str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.tracking_uri = tracking_uri
        self._base = (tracking_uri or "").rstrip("/")
        self._headers: dict[str, str] = dict(auth_header) if auth_header else {}
        self._session = session or requests.Session()

    @property
    def source_name(self) -> str:
        # Same as the FS scanner so the catalog / ingest treat it identically.
        return "mlflow"

    def get_dependencies(self) -> list[str]:
        return ["requests"]

    def is_available(self) -> bool:
        return bool(self.tracking_uri)

    def find_root_dirs(self, search_root: Path | None = None) -> list[Path]:
        # REST mode ignores the local filesystem entirely.
        return []

    def discover_runs(
        self,
        root_dirs: list[Path],
        catalog: Catalog,
        log_path: Path | None = None,
    ) -> list[RunData]:
        existing_map = self.build_existing_runs_map(catalog)

        exp_names = self._fetch_experiments(log_path)
        if not exp_names:
            _log("No MLflow experiments found via REST.", log_path)
            return []

        experiment_ids = sorted(exp_names)
        raw_runs = self._fetch_runs(experiment_ids, log_path)
        if not raw_runs:
            _log("No MLflow runs found via REST.", log_path)
            return []

        _log(f"Fetched {len(raw_runs)} MLflow runs via REST.", log_path)

        new_runs: list[RunData] = []
        skipped = 0

        for raw in raw_runs:
            run_id = (raw.get("info") or {}).get("run_id")
            if run_id and run_id in existing_map and self.is_finished(existing_map[run_id]):
                skipped += 1
                continue

            try:
                info = self._build_run_info(raw, exp_names, log_path)
                if not info:
                    continue

                actual_id = info["run_id"]
                if actual_id in existing_map and self.is_finished(existing_map[actual_id]):
                    skipped += 1
                    continue

                new_runs.append(info)
            except Exception as exc:
                _log(f"Failed to map MLflow REST run: {exc}", log_path)

        _log(f"MLflow REST scanner: {len(new_runs)} new, {skipped} skipped.", log_path)
        return new_runs

    # ── HTTP paging ───────────────────────────────────────────────────

    def _fetch_experiments(self, log_path: Path | None) -> dict[str, str]:
        """Page experiments/search, returning ``{experiment_id: name}``."""
        names: dict[str, str] = {}
        url = f"{self._base}/api/2.0/mlflow/experiments/search"
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"max_results": _PAGE_SIZE}
            if page_token:
                params["page_token"] = page_token
            payload = self._request("get", url, log_path, params=params)
            if payload is None:
                break
            for exp in payload.get("experiments") or []:
                exp_id = exp.get("experiment_id")
                if exp_id is None:
                    continue
                names[str(exp_id)] = exp.get("name") or f"experiment-{exp_id}"
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return names

    def _fetch_runs(self, experiment_ids: list[str], log_path: Path | None) -> list[dict[str, Any]]:
        """Page runs/search across all experiments, returning raw run dicts."""
        runs: list[dict[str, Any]] = []
        url = f"{self._base}/api/2.0/mlflow/runs/search"
        page_token: str | None = None
        while True:
            body: dict[str, Any] = {
                "experiment_ids": experiment_ids,
                "max_results": _PAGE_SIZE,
                "run_view_type": "ACTIVE_ONLY",
            }
            if page_token:
                body["page_token"] = page_token
            payload = self._request("post", url, log_path, json=body)
            if payload is None:
                break
            runs.extend(payload.get("runs") or [])
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return runs

    def _request(
        self,
        method: str,
        url: str,
        log_path: Path | None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Issue one HTTP call, returning parsed JSON or None on any error."""
        try:
            resp = self._session.request(
                method, url, headers=self._headers, timeout=_TIMEOUT, **kwargs
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            _log(f"MLflow REST request to {url} failed: {exc}", log_path)
            return None

    # ── Mapping ───────────────────────────────────────────────────────

    def _build_run_info(
        self,
        raw: dict[str, Any],
        exp_names: dict[str, str],
        log_path: Path | None,
    ) -> RunData | None:
        """Map one REST run (info + data) to the canonical RunData shape."""
        info = raw.get("info") or {}
        data = raw.get("data") or {}

        run_id = info.get("run_id") or info.get("run_uuid")
        if not run_id:
            _log("MLflow REST run without a run_id; skipping.", log_path)
            return None

        experiment_id = info.get("experiment_id")
        experiment_name = exp_names.get(str(experiment_id)) or f"experiment-{experiment_id}"

        # Timestamps (MLflow stores milliseconds, wire-encoded as int64 strings);
        # end 0/absent means still open.
        start_time_ms = _as_int(info.get("start_time"))
        end_time_ms = _as_int(info.get("end_time"))
        start_time = start_time_ms / 1000.0 if start_time_ms else None
        end_time = end_time_ms / 1000.0 if end_time_ms and end_time_ms > 0 else None

        status = _STATUS_MAP.get(info.get("status"), "unknown")

        params = {p["key"]: p.get("value") for p in data.get("params") or [] if "key" in p}
        tags = {t["key"]: t.get("value") for t in data.get("tags") or [] if "key" in t}
        summary = _latest_metrics(data.get("metrics") or [])

        # Mirror the FS scanner's precedence: RunInfo.run_name wins over the tag
        # (they can legitimately disagree if the tag was set directly).
        run_name = info.get("run_name") or tags.get("mlflow.runName") or run_id
        script_path = tags.get("mlflow.source.name")
        user = tags.get("mlflow.user") or info.get("user_id")
        parent_run_id = tags.get("mlflow.parentRunId")
        git_commit = tags.get("mlflow.source.git.commit")

        config: dict[str, Any] = dict(params)
        if git_commit:
            config["git_commit"] = git_commit
        if parent_run_id:
            config["parent_run_id"] = parent_run_id

        # experiment_id is interpolated into the R2 object key, so it must stay
        # slash-free; the human-readable name lives in `project`.
        exp_id = experiment_name.replace("/", "_")

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
            "history": [],
            "system_metrics": [],
            "tags": tags,
            "script_path": script_path,
            "script_content": None,
            "mode": "rest",
            "path": self.tracking_uri,
            "run_index": None,
        }
