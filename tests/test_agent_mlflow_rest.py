"""Tests for the MLflow tracking-server (REST) scanner.

Mirrors ``TestMlflowScanner`` but drives the MLflow REST API via requests-mock
instead of the local filesystem. Pins the same canonical RunData shape.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests_mock

from skyportalai.agent.__main__ import build_scanners
from skyportalai.agent.config import AgentConfig
from skyportalai.agent.scrapers import MlflowRestScanner, MlflowScanner

URI = "http://mlflow.example"
EXP_URL = f"{URI}/api/2.0/mlflow/experiments/search"
RUNS_URL = f"{URI}/api/2.0/mlflow/runs/search"


def _experiment_pages() -> list[dict]:
    # Two experiments across two pages to exercise experiments pagination.
    return [
        {"json": {"experiments": [{"experiment_id": "0", "name": "Default"}], "next_page_token": "exp-2"}},
        {"json": {"experiments": [{"experiment_id": "1", "name": "team/project"}]}},
    ]


def _run_finished() -> dict:
    # int64 fields (start_time/end_time, metric step/timestamp) are wire-encoded
    # by MLflow's proto3-JSON serializer as STRINGS, not numbers; the metric
    # value is a proto double and stays numeric. Mirror that shape exactly so the
    # coercion in the scanner is actually exercised.
    return {
        "info": {
            "run_id": "run-aaa",
            "experiment_id": "0",
            "user_id": "alice",
            "status": "FINISHED",
            "start_time": "1704110400000",
            "end_time": "1704114000000",
            "run_name": "run-aaa-name",
            "lifecycle_stage": "active",
        },
        "data": {
            "params": [
                {"key": "learning_rate", "value": "0.001"},
                {"key": "epochs", "value": "10"},
            ],
            "metrics": [
                {"key": "loss", "value": 0.85, "timestamp": "1704110400000", "step": "0"},
                {"key": "loss", "value": 0.21, "timestamp": "1704110600000", "step": "2"},
                {"key": "accuracy", "value": 0.91, "timestamp": "1704110600000", "step": "2"},
            ],
            "tags": [
                {"key": "mlflow.runName", "value": "run-aaa-name"},
                {"key": "mlflow.source.name", "value": "train.py"},
                {"key": "mlflow.user", "value": "alice"},
                {"key": "mlflow.source.git.commit", "value": "abc123"},
            ],
        },
    }


def _run_running() -> dict:
    return {
        "info": {
            "run_id": "run-bbb",
            "experiment_id": "1",
            "user_id": "bob",
            "status": "RUNNING",
            "start_time": "1704200000000",
            "end_time": "0",
            "lifecycle_stage": "active",
        },
        "data": {
            "params": [{"key": "batch_size", "value": "32"}],
            "metrics": [{"key": "loss", "value": 0.5, "timestamp": "1704200000000", "step": "0"}],
            "tags": [{"key": "mlflow.parentRunId", "value": "parent-1"}],
        },
    }


def _run_pages() -> list[dict]:
    # One run per page to exercise runs pagination.
    return [
        {"json": {"runs": [_run_finished()], "next_page_token": "runs-2"}},
        {"json": {"runs": [_run_running()]}},
    ]


def _register(m: requests_mock.Mocker) -> None:
    m.get(EXP_URL, _experiment_pages())
    m.post(RUNS_URL, _run_pages())


# ── Identity / availability ───────────────────────────────────────────


def test_is_available_true() -> None:
    assert MlflowRestScanner(URI).is_available() is True


def test_is_available_false_when_no_uri() -> None:
    assert MlflowRestScanner("").is_available() is False
    assert MlflowRestScanner(None).is_available() is False


def test_source_name_matches_fs_scanner() -> None:
    assert MlflowRestScanner(URI).source_name == "mlflow"


def test_get_dependencies() -> None:
    assert MlflowRestScanner(URI).get_dependencies() == ["requests"]


def test_find_root_dirs_always_empty() -> None:
    scanner = MlflowRestScanner(URI)
    assert scanner.find_root_dirs() == []
    assert scanner.find_root_dirs(Path("/data/mlruns")) == []


# ── discover_runs mapping ─────────────────────────────────────────────


def test_discover_runs_maps_canonical_shape() -> None:
    scanner = MlflowRestScanner(URI)
    with requests_mock.Mocker() as m:
        _register(m)
        runs = scanner.discover_runs([], {"experiments": []})

    by_id = {r["run_id"]: r for r in runs}
    assert set(by_id) == {"run-aaa", "run-bbb"}

    a = by_id["run-aaa"]
    assert a["source"] == "mlflow"
    assert a["run_name"] == "run-aaa-name"
    assert a["status"] == "finished"
    assert a["experiment_id"] == "Default"
    assert a["project"] == "Default"
    assert a["entity"] == "alice"
    assert a["start_time"] == 1704110400.0
    assert a["end_time"] == 1704114000.0
    assert a["config"]["learning_rate"] == "0.001"
    assert a["config"]["epochs"] == "10"
    assert a["config"]["git_commit"] == "abc123"
    assert a["summary"]["loss"] == 0.21  # latest value, not 0.85
    assert a["summary"]["accuracy"] == 0.91
    assert a["history"] == []
    assert a["system_metrics"] == []
    assert a["tags"]["mlflow.runName"] == "run-aaa-name"
    assert a["script_path"] == "train.py"
    assert a["script_content"] is None
    assert a["mode"] == "rest"
    assert a["path"] == URI
    assert a["run_index"] is None

    b = by_id["run-bbb"]
    assert b["status"] == "running"
    assert "/" not in b["experiment_id"]
    assert b["experiment_id"] == "team_project"
    assert b["project"] == "team/project"
    assert b["end_time"] is None
    assert b["config"]["batch_size"] == "32"
    assert b["config"]["parent_run_id"] == "parent-1"
    assert b["entity"] == "bob"
    # No mlflow.runName tag and no info.run_name -> falls back to run_id.
    assert b["run_name"] == "run-bbb"


def test_run_name_prefers_run_info_over_tag() -> None:
    # RunInfo.run_name and the mlflow.runName tag can legitimately disagree (e.g.
    # the tag was set directly). REST must mirror the FS scanner: RunInfo wins.
    scanner = MlflowRestScanner(URI)
    run = {
        "info": {
            "run_id": "run-ccc",
            "experiment_id": "0",
            "status": "FINISHED",
            "start_time": "1704110400000",
            "end_time": "1704114000000",
            "run_name": "info-name",
            "lifecycle_stage": "active",
        },
        "data": {"tags": [{"key": "mlflow.runName", "value": "tag-name"}]},
    }
    with requests_mock.Mocker() as m:
        m.get(EXP_URL, json={"experiments": [{"experiment_id": "0", "name": "Default"}]})
        m.post(RUNS_URL, json={"runs": [run]})
        runs = scanner.discover_runs([], {"experiments": []})
    assert runs[0]["run_name"] == "info-name"


def test_discover_runs_paginates_experiments_and_runs() -> None:
    scanner = MlflowRestScanner(URI)
    with requests_mock.Mocker() as m:
        _register(m)
        scanner.discover_runs([], {"experiments": []})
        history = m.request_history

    exp_calls = [h for h in history if h.path == "/api/2.0/mlflow/experiments/search"]
    run_calls = [h for h in history if h.path == "/api/2.0/mlflow/runs/search"]
    assert len(exp_calls) == 2
    assert len(run_calls) == 2
    # Page 2 of experiments carried the page_token from page 1.
    assert exp_calls[1].qs.get("page_token") == ["exp-2"]
    # runs/search searches all experiment_ids at once, ACTIVE_ONLY only.
    body0 = run_calls[0].json()
    assert body0["experiment_ids"] == ["0", "1"]
    assert body0["run_view_type"] == "ACTIVE_ONLY"
    assert "page_token" not in body0
    # Page 2 of runs carried the page_token from page 1.
    assert run_calls[1].json()["page_token"] == "runs-2"


def test_discover_runs_skips_known_finished_run() -> None:
    scanner = MlflowRestScanner(URI)
    catalog = {
        "experiments": [
            {"id": "Default", "runs": [{"run_id": "run-aaa", "end_time": 1704114000.0}]}
        ]
    }
    with requests_mock.Mocker() as m:
        _register(m)
        runs = scanner.discover_runs([], catalog)
    assert [r["run_id"] for r in runs] == ["run-bbb"]


def test_discover_runs_handles_http_error() -> None:
    scanner = MlflowRestScanner(URI)
    with requests_mock.Mocker() as m:
        m.get(EXP_URL, status_code=500)
        runs = scanner.discover_runs([], {"experiments": []})
    assert runs == []


def test_discover_runs_no_experiments_returns_empty() -> None:
    scanner = MlflowRestScanner(URI)
    with requests_mock.Mocker() as m:
        m.get(EXP_URL, json={"experiments": []})
        runs = scanner.discover_runs([], {"experiments": []})
    assert runs == []


# ── Config parsing ────────────────────────────────────────────────────


def test_config_defaults_to_filesystem_mode() -> None:
    cfg = AgentConfig(token="t")
    assert cfg.mlflow_mode == "filesystem"
    assert cfg.mlflow_tracking_uri is None


def test_config_default_mode_from_env_unset() -> None:
    cfg = AgentConfig.from_env({"SKYPORTAL_AGENT_TOKEN": "t"})
    assert cfg.mlflow_mode == "filesystem"
    assert cfg.mlflow_tracking_uri is None


def test_config_parses_rest_mode_and_uri() -> None:
    cfg = AgentConfig.from_env(
        {
            "SKYPORTAL_AGENT_TOKEN": "t",
            "SKYPORTAL_AGENT_MLFLOW_MODE": "rest",
            "SKYPORTAL_AGENT_MLFLOW_TRACKING_URI": URI,
        }
    )
    assert cfg.mlflow_mode == "rest"
    assert cfg.mlflow_tracking_uri == URI


def test_config_warns_when_rest_mode_without_tracking_uri(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="skyportalai.agent.config"):
        cfg = AgentConfig.from_env(
            {"SKYPORTAL_AGENT_TOKEN": "t", "SKYPORTAL_AGENT_MLFLOW_MODE": "rest"}
        )
    assert cfg.mlflow_mode == "rest"
    assert cfg.mlflow_tracking_uri is None
    assert any("TRACKING_URI" in r.getMessage() for r in caplog.records)


def test_config_no_warning_when_rest_mode_with_uri(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="skyportalai.agent.config"):
        AgentConfig.from_env(
            {
                "SKYPORTAL_AGENT_TOKEN": "t",
                "SKYPORTAL_AGENT_MLFLOW_MODE": "rest",
                "SKYPORTAL_AGENT_MLFLOW_TRACKING_URI": URI,
            }
        )
    assert not any("TRACKING_URI" in r.getMessage() for r in caplog.records)


def test_config_unknown_mode_coerced_to_filesystem() -> None:
    cfg = AgentConfig.from_env(
        {"SKYPORTAL_AGENT_TOKEN": "t", "SKYPORTAL_AGENT_MLFLOW_MODE": "databricks"}
    )
    assert cfg.mlflow_mode == "filesystem"


# ── build_scanners selection ──────────────────────────────────────────


def test_build_scanners_uses_rest_scanner_when_mode_rest() -> None:
    scanners = build_scanners(
        AgentConfig(token="t", mlflow_mode="rest", mlflow_tracking_uri=URI)
    )
    types = [type(s) for s in scanners]
    assert MlflowRestScanner in types
    assert MlflowScanner not in types
    # Exactly one mlflow scanner — never both (avoids source='mlflow' collision).
    assert sum(1 for s in scanners if s.source_name == "mlflow") == 1


def test_build_scanners_uses_fs_scanner_when_mode_filesystem() -> None:
    scanners = build_scanners(AgentConfig(token="t", mlflow_mode="filesystem"))
    types = [type(s) for s in scanners]
    assert MlflowScanner in types
    assert MlflowRestScanner not in types
    assert sum(1 for s in scanners if s.source_name == "mlflow") == 1
