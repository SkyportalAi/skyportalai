"""Tests for the public Typer CLI scaffold."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from typer.testing import CliRunner

from skyportalai.cli.main import app

runner = CliRunner()


def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYPORTAL_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTAL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.delenv("SKYPORTAL_API_KEY", raising=False)
    monkeypatch.delenv("SKYPORTAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SKYPORTAL_BASE_URL", raising=False)
    monkeypatch.delenv("SKYPORTAL_URL", raising=False)


def test_help_and_version(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)

    help_result = runner.invoke(app, ["--help"])
    version_result = runner.invoke(app, ["--version"])

    assert help_result.exit_code == 0
    assert "config" in help_result.stdout
    assert version_result.exit_code == 0
    assert version_result.stdout.startswith("skyportalai ")


def test_module_entry_point_does_not_load_main_twice(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "skyportalai.cli.main", "--version"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "SKYPORTAL_CONFIG_PATH": str(tmp_path / "config.yaml"),
            "SKYPORTAL_CREDENTIALS_PATH": str(tmp_path / "credentials.json"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("skyportalai ")
    assert "RuntimeWarning" not in result.stderr


def test_config_show_json_never_prints_the_key(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk-super-secret")

    result = runner.invoke(app, ["--json", "config", "show"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["authenticated"] is True
    assert payload["data"]["api_key_source"] == "SKYPORTAL_API_KEY"
    assert "sk-super-secret" not in result.stdout


def test_config_set_and_human_target_output(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["config", "set", "--base-url", "https://portal.example/", "--timeout", "15"],
    )

    assert result.exit_code == 0
    assert "Saved SkyPortal CLI configuration" in result.stdout
    assert "API target: https://portal.example" in result.output
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert config["portal"] == {"base_url": "https://portal.example", "request_timeout": 15.0}


def test_config_set_requires_a_value(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)

    result = runner.invoke(app, ["--json", "config", "set"])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert "Provide --base-url" in payload["error"]