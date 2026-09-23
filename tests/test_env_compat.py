"""Environment resolution and the config directory.

The pre-0.2.0 SKYPORTAL_* names were deprecated in 0.2.0 and removed in 0.3.0:
they are no longer read, and nothing warns about them. A leftover ~/.skyportal
directory is still moved into ~/.skyportalai.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings

import yaml

from skyportalai import _env


def test_canonical_name_is_used_when_set(monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "new")
    assert _env.get("SKYPORTALAI_API_KEY") == "new"


def test_removed_legacy_name_is_ignored_silently(monkeypatch):
    monkeypatch.delenv("SKYPORTALAI_API_KEY", raising=False)
    monkeypatch.setenv("SKYPORTAL_API_KEY", "old")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _env.get("SKYPORTALAI_API_KEY") is None


def test_default_is_returned_when_unset(monkeypatch):
    monkeypatch.delenv("SKYPORTALAI_API_KEY", raising=False)
    assert _env.get("SKYPORTALAI_API_KEY", "fallback") == "fallback"


def test_lookup_reports_which_variable_supplied_the_value(monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "new")
    assert _env.lookup("SKYPORTALAI_API_KEY") == ("new", "SKYPORTALAI_API_KEY")
    monkeypatch.delenv("SKYPORTALAI_API_KEY")
    assert _env.lookup("SKYPORTALAI_API_KEY", "d") == ("d", None)


def test_get_from_reads_an_injected_mapping():
    """The agent is configured from a mapping, not os.environ."""
    assert _env.get_from({"SKYPORTALAI_AGENT_TOKEN": "t"}, "SKYPORTALAI_AGENT_TOKEN") == "t"


def test_get_from_ignores_the_removed_legacy_name():
    assert _env.get_from({"SKYPORTAL_AGENT_TOKEN": "t"}, "SKYPORTALAI_AGENT_TOKEN") is None


def test_config_dir_prefers_the_new_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".skyportalai").mkdir()
    assert _env.config_dir() == tmp_path / ".skyportalai"


def test_config_dir_moves_a_leftover_legacy_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    legacy = tmp_path / ".skyportal"
    legacy.mkdir()
    (legacy / "credentials.json").write_text('{"access_token": "kept"}')

    resolved = _env.config_dir()

    assert resolved == tmp_path / ".skyportalai"
    assert (resolved / "credentials.json").read_text() == '{"access_token": "kept"}'
    assert not legacy.exists()


def test_config_dir_defaults_to_new_path_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert _env.config_dir() == tmp_path / ".skyportalai"


def test_config_path_override_wins_over_the_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "custom.yaml"))
    assert _env.config_path("config.yaml", "SKYPORTALAI_CONFIG_PATH") == tmp_path / "custom.yaml"


def test_removed_legacy_config_path_override_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("SKYPORTALAI_CONFIG_PATH", raising=False)
    monkeypatch.setenv("SKYPORTAL_CONFIG_PATH", str(tmp_path / "custom.yaml"))
    assert _env.config_path("config.yaml", "SKYPORTALAI_CONFIG_PATH") == tmp_path / ".skyportalai" / "config.yaml"


def test_console_script_ignores_removed_legacy_variables(tmp_path):
    """End to end through the real entry point: no value taken, no notice printed."""
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "SKYPORTAL_CONFIG_PATH": str(tmp_path / "legacy.yaml"),
    }
    environment.pop("SKYPORTALAI_CONFIG_PATH", None)

    result = subprocess.run(
        [sys.executable, "-c", "from skyportalai.cli import main; main()", "config", "show"],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "SKYPORTAL_CONFIG_PATH" not in result.stderr
    assert "legacy.yaml" not in result.stdout


def test_configure_ignores_the_removed_legacy_url_env(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from skyportalai._client import DEFAULT_BASE_URL
    from skyportalai.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SKYPORTALAI_URL", raising=False)
    monkeypatch.setenv("SKYPORTAL_URL", "https://legacy.example")

    assert CliRunner().invoke(app, ["configure"]).exit_code == 0
    saved = yaml.safe_load((tmp_path / ".skyportalai" / "config.yaml").read_text())
    assert saved["portal"]["base_url"] == DEFAULT_BASE_URL


def test_configure_uses_the_canonical_url_env(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from skyportalai.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKYPORTALAI_URL", "https://new.example")

    assert CliRunner().invoke(app, ["configure"]).exit_code == 0
    saved = yaml.safe_load((tmp_path / ".skyportalai" / "config.yaml").read_text())
    assert saved["portal"]["base_url"] == "https://new.example"


def test_configure_flag_beats_the_env(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from skyportalai.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKYPORTALAI_URL", "https://env.example")

    result = CliRunner().invoke(app, ["configure", "--portal-url", "https://flag.example"])
    assert result.exit_code == 0
    saved = yaml.safe_load((tmp_path / ".skyportalai" / "config.yaml").read_text())
    assert saved["portal"]["base_url"] == "https://flag.example"
