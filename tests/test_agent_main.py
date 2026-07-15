"""Tests for the agent entrypoint wiring (build_scanners / build_runner).

main() itself (signal install + health thread + infinite loop) is exercised by a
manual smoke run, not here; the assembly logic it delegates to is unit-tested.
"""

from __future__ import annotations

from pathlib import Path

from skyportalai.agent.__main__ import build_runner, build_scanners
from skyportalai.agent.config import AgentConfig
from skyportalai.agent.scrapers import MlflowScanner, WandbScanner


def test_build_scanners_includes_both_when_enabled():
    scanners = build_scanners(AgentConfig(token="t", enable_wandb=True, enable_mlflow=True))
    assert [type(s) for s in scanners] == [WandbScanner, MlflowScanner]


def test_build_scanners_respects_toggles():
    wandb_only = build_scanners(AgentConfig(token="t", enable_wandb=True, enable_mlflow=False))
    assert [type(s) for s in wandb_only] == [WandbScanner]

    mlflow_only = build_scanners(AgentConfig(token="t", enable_wandb=False, enable_mlflow=True))
    assert [type(s) for s in mlflow_only] == [MlflowScanner]

    none = build_scanners(AgentConfig(token="t", enable_wandb=False, enable_mlflow=False))
    assert none == []


def test_build_runner_wires_config(tmp_path: Path):
    cfg = AgentConfig(
        token="spa-tok",
        state_dir=tmp_path,
        interval_seconds=42,
        queue_max_batches=7,
        wandb_dir=Path("/mnt/wandb"),
        mlflow_dir=Path("/mnt/mlruns"),
    )
    runner = build_runner(cfg)

    assert runner.interval_seconds == 42
    assert runner.queue.spool_dir == tmp_path / "spool"
    assert runner.queue.max_batches == 7
    assert runner.catalog_path == tmp_path / "existing_experiments.json"
    assert runner.roots == {"wandb": Path("/mnt/wandb"), "mlflow": Path("/mnt/mlruns")}


def test_build_runner_shipper_targets_ingest_url():
    cfg = AgentConfig(token="spa-tok", base_url="https://skyportal.example")
    runner = build_runner(cfg)
    assert runner.shipper.url == "https://skyportal.example/agent/api/observability/ingest/"
    assert runner.shipper.token == "spa-tok"
