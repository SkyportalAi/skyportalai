"""Tests for AgentConfig — the daemon's typed, env-first configuration."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from skyportalai._client import DEFAULT_BASE_URL
from skyportalai._exceptions import SkyportalError
from skyportalai.agent.config import AgentConfig


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "spa-tok")
    cfg = AgentConfig.from_env()
    assert cfg.token == "spa-tok"


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("SKYPORTAL_AGENT_TOKEN", raising=False)
    with pytest.raises(SkyportalError):
        AgentConfig.from_env()


def test_whitespace_token_raises(monkeypatch):
    # A whitespace-only token is a misconfiguration; fail fast rather than
    # authenticating with a blank credential.
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "   ")
    with pytest.raises(SkyportalError):
        AgentConfig.from_env()


def test_token_is_stripped(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "  spa-tok  ")
    cfg = AgentConfig.from_env()
    assert cfg.token == "spa-tok"


def test_explicit_construction_defaults():
    cfg = AgentConfig(token="t")
    assert cfg.token == "t"
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.interval_seconds == 60
    assert cfg.enable_wandb is True
    assert cfg.enable_mlflow is True
    assert cfg.wandb_dir is None
    assert cfg.mlflow_dir is None
    assert cfg.cluster_name is None


def test_default_base_url(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.delenv("SKYPORTAL_BASE_URL", raising=False)
    cfg = AgentConfig.from_env()
    assert cfg.base_url == DEFAULT_BASE_URL


def test_base_url_from_env_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_BASE_URL", "http://localhost:8000/")
    cfg = AgentConfig.from_env()
    assert cfg.base_url == "http://localhost:8000"


def test_defaults_when_unset(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    for var in (
        "SKYPORTAL_AGENT_WANDB_DIR",
        "SKYPORTAL_AGENT_MLFLOW_DIR",
        "SKYPORTAL_AGENT_INTERVAL_SECONDS",
        "SKYPORTAL_AGENT_ENABLE_WANDB",
        "SKYPORTAL_AGENT_ENABLE_MLFLOW",
        "SKYPORTAL_AGENT_CLUSTER_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = AgentConfig.from_env()
    assert cfg.interval_seconds == 60
    assert cfg.enable_wandb is True
    assert cfg.enable_mlflow is True
    assert cfg.wandb_dir is None
    assert cfg.mlflow_dir is None
    assert cfg.cluster_name is None


def test_scan_dirs_interval_and_cluster_from_env(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_WANDB_DIR", "/data/wandb")
    monkeypatch.setenv("SKYPORTAL_AGENT_MLFLOW_DIR", "/data/mlruns")
    monkeypatch.setenv("SKYPORTAL_AGENT_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("SKYPORTAL_AGENT_CLUSTER_NAME", "gpu-cluster-1")
    cfg = AgentConfig.from_env()
    assert cfg.wandb_dir == Path("/data/wandb")
    assert cfg.mlflow_dir == Path("/data/mlruns")
    assert cfg.interval_seconds == 30
    assert cfg.cluster_name == "gpu-cluster-1"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
    ],
)
def test_enable_toggle_parses_bool(monkeypatch, value, expected):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_ENABLE_WANDB", value)
    cfg = AgentConfig.from_env()
    assert cfg.enable_wandb is expected


def test_invalid_interval_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_INTERVAL_SECONDS", "not-an-int")
    cfg = AgentConfig.from_env()
    assert cfg.interval_seconds == 60


@pytest.mark.parametrize("value", ["0", "-1", "-30"])
def test_non_positive_interval_falls_back_to_default(monkeypatch, value):
    # A zero/negative interval would busy-loop or crash time.sleep in the daemon.
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_INTERVAL_SECONDS", value)
    cfg = AgentConfig.from_env()
    assert cfg.interval_seconds == 60


def test_from_env_accepts_explicit_mapping():
    cfg = AgentConfig.from_env(
        {"SKYPORTAL_AGENT_TOKEN": "t", "SKYPORTAL_AGENT_INTERVAL_SECONDS": "15"}
    )
    assert cfg.token == "t"
    assert cfg.interval_seconds == 15


def test_state_dir_default_and_derived_paths(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.delenv("SKYPORTAL_AGENT_STATE_DIR", raising=False)
    cfg = AgentConfig.from_env()
    assert cfg.state_dir == Path("/var/lib/skyportal-agent")
    assert cfg.spool_dir == Path("/var/lib/skyportal-agent/spool")
    assert cfg.catalog_path == Path("/var/lib/skyportal-agent/existing_experiments.json")


def test_state_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_STATE_DIR", str(tmp_path))
    cfg = AgentConfig.from_env()
    assert cfg.state_dir == tmp_path
    assert cfg.spool_dir == tmp_path / "spool"
    assert cfg.catalog_path == tmp_path / "existing_experiments.json"


def test_healthz_port_default_and_override(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.delenv("SKYPORTAL_AGENT_HEALTHZ_PORT", raising=False)
    assert AgentConfig.from_env().healthz_port == 8080
    monkeypatch.setenv("SKYPORTAL_AGENT_HEALTHZ_PORT", "9000")
    assert AgentConfig.from_env().healthz_port == 9000


@pytest.mark.parametrize("value", ["0", "-1", "65536", "99999", "garbage"])
def test_out_of_range_healthz_port_falls_back_to_default(monkeypatch, value):
    # A port <1 or >65535 would crash ThreadingHTTPServer at bind time; fall back
    # to the default like every other field rather than failing at startup.
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_HEALTHZ_PORT", value)
    assert AgentConfig.from_env().healthz_port == 8080


def test_queue_max_batches_default_and_override(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.delenv("SKYPORTAL_AGENT_QUEUE_MAX_BATCHES", raising=False)
    assert AgentConfig.from_env().queue_max_batches == 1000
    monkeypatch.setenv("SKYPORTAL_AGENT_QUEUE_MAX_BATCHES", "50")
    assert AgentConfig.from_env().queue_max_batches == 50


@pytest.mark.parametrize("value", ["0", "-5", "garbage"])
def test_invalid_queue_max_batches_falls_back_to_default(monkeypatch, value):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_QUEUE_MAX_BATCHES", value)
    assert AgentConfig.from_env().queue_max_batches == 1000


def test_explicit_construction_has_p1_defaults():
    cfg = AgentConfig(token="t")
    assert cfg.state_dir == Path("/var/lib/skyportal-agent")
    assert cfg.healthz_port == 8080
    assert cfg.queue_max_batches == 1000


def test_invalid_int_logs_warning_naming_the_var(monkeypatch, caplog):
    # A typo'd value must not fail silently: the fallback names the env var and
    # the bad value so an operator can see why their override was ignored.
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_HEALTHZ_PORT", "8O80")  # letter O, not zero
    with caplog.at_level(logging.WARNING):
        cfg = AgentConfig.from_env()
    assert cfg.healthz_port == 8080
    assert "SKYPORTAL_AGENT_HEALTHZ_PORT" in caplog.text


def test_out_of_range_int_logs_warning(monkeypatch, caplog):
    monkeypatch.setenv("SKYPORTAL_AGENT_TOKEN", "t")
    monkeypatch.setenv("SKYPORTAL_AGENT_HEALTHZ_PORT", "70000")  # > 65535
    with caplog.at_level(logging.WARNING):
        AgentConfig.from_env()
    assert "SKYPORTAL_AGENT_HEALTHZ_PORT" in caplog.text
