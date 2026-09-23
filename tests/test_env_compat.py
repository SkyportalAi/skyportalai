"""Environment resolution and the config directory.

The pre-0.2.0 SKYPORTAL_* names were deprecated in 0.2.0 and removed in 0.3.0:
they are no longer read. One still set draws a UserWarning naming its replacement,
except the settings that choose the host credentials go to: there it is an error,
since the default host would receive them. A leftover ~/.skyportal directory is
still moved into ~/.skyportalai.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import yaml

from skyportalai import SkyportalError, _env


def test_canonical_name_is_used_when_set(monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "new")
    assert _env.get("SKYPORTALAI_API_KEY") == "new"


def test_removed_legacy_name_is_ignored_with_a_warning(monkeypatch):
    monkeypatch.delenv("SKYPORTALAI_API_KEY", raising=False)
    monkeypatch.setenv("SKYPORTAL_API_KEY", "old")
    with pytest.warns(UserWarning, match="SKYPORTAL_API_KEY was removed in 0.3.0 and is ignored; set SKYPORTALAI_API_KEY"):
        assert _env.get("SKYPORTALAI_API_KEY") is None


def test_a_leftover_base_url_is_refused_rather_than_sending_credentials_to_the_default_host(monkeypatch):
    """A self-hosted user with the new key but the old URL would otherwise ship it to SaaS."""
    from skyportalai import Skyportal

    monkeypatch.delenv("SKYPORTALAI_BASE_URL", raising=False)
    monkeypatch.setenv("SKYPORTAL_BASE_URL", "https://skyportal.internal.example")
    with pytest.raises(SkyportalError, match="set SKYPORTALAI_BASE_URL instead. Refusing"):
        Skyportal(api_key="sk_x")


def test_a_leftover_base_url_is_fine_once_the_new_name_is_set(monkeypatch):
    from skyportalai import Skyportal

    monkeypatch.setenv("SKYPORTALAI_BASE_URL", "https://skyportal.internal.example")
    monkeypatch.setenv("SKYPORTAL_BASE_URL", "https://skyportal.internal.example")
    assert Skyportal(api_key="sk_x").base_url == "https://skyportal.internal.example"


def test_the_cli_reports_a_leftover_base_url_without_a_traceback(tmp_path):
    environment = {**os.environ, "HOME": str(tmp_path), "SKYPORTAL_BASE_URL": "https://skyportal.internal.example"}
    environment.pop("SKYPORTALAI_BASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", "from skyportalai.cli import main; main()", "config", "show"],
        capture_output=True, text=True, env=environment,
    )
    assert result.returncode == 1
    assert "Error: SKYPORTAL_BASE_URL was removed in 0.3.0" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_agent_refuses_a_leftover_base_url():
    from skyportalai.agent.config import AgentConfig

    with pytest.raises(SkyportalError, match="SKYPORTALAI_BASE_URL"):
        AgentConfig.from_env({"SKYPORTALAI_AGENT_TOKEN": "agt_x", "SKYPORTAL_BASE_URL": "https://internal"})


def test_no_warning_when_the_canonical_name_is_set(monkeypatch, recwarn):
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "new")
    monkeypatch.setenv("SKYPORTAL_API_KEY", "old")
    assert _env.get("SKYPORTALAI_API_KEY") == "new"
    assert not [w for w in recwarn if "SKYPORTAL_API_KEY" in str(w.message)]


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


def test_get_from_ignores_the_removed_legacy_name_with_a_warning():
    with pytest.warns(UserWarning, match="set SKYPORTALAI_AGENT_TOKEN"):
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
    with pytest.warns(UserWarning, match="SKYPORTAL_CONFIG_PATH"):
        assert _env.config_path("config.yaml", "SKYPORTALAI_CONFIG_PATH") == tmp_path / ".skyportalai" / "config.yaml"


def test_console_script_ignores_and_names_removed_legacy_variables(tmp_path):
    """End to end through the real entry point: the value is not used, and the user is told."""
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
    assert "SKYPORTAL_CONFIG_PATH was removed in 0.3.0" in result.stderr
    assert "legacy.yaml" not in result.stdout


def test_configure_refuses_a_leftover_url_env(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from skyportalai.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SKYPORTALAI_URL", raising=False)
    monkeypatch.setenv("SKYPORTAL_URL", "https://legacy.example")

    result = CliRunner().invoke(app, ["configure"])
    assert result.exit_code == 1
    assert "SKYPORTAL_URL was removed in 0.3.0" in result.output
    assert not (tmp_path / ".skyportalai" / "config.yaml").exists()


def test_a_leftover_url_env_is_refused_before_any_command_runs(tmp_path, monkeypatch):
    """The root callback resolves the target first, so even an explicit configure flag
    stops here; every other command would be refused too until the env is cleaned up."""
    from typer.testing import CliRunner

    from skyportalai.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SKYPORTALAI_URL", raising=False)
    monkeypatch.setenv("SKYPORTAL_URL", "https://legacy.example")

    result = CliRunner().invoke(app, ["configure", "--portal-url", "https://flag.example"])
    assert result.exit_code == 1
    assert "set SKYPORTALAI_URL instead" in result.output


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
