"""Backwards compatibility for the pre-0.2.0 SKYPORTAL_* names and config dir."""

from __future__ import annotations

import os
import subprocess
import sys
import warnings

import pytest

from skyportalai import _env


def test_canonical_name_is_used_when_set(monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "new")
    monkeypatch.delenv("SKYPORTAL_API_KEY", raising=False)
    assert _env.get("SKYPORTALAI_API_KEY") == "new"


def test_canonical_name_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "new")
    monkeypatch.setenv("SKYPORTAL_API_KEY", "old")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # the legacy path must not even be consulted
        assert _env.get("SKYPORTALAI_API_KEY") == "new"


def test_legacy_name_still_works_and_warns(monkeypatch):
    monkeypatch.delenv("SKYPORTALAI_API_KEY", raising=False)
    monkeypatch.setenv("SKYPORTAL_API_KEY", "old")
    with pytest.warns(DeprecationWarning, match="SKYPORTAL_API_KEY"):
        assert _env.get("SKYPORTALAI_API_KEY") == "old"


def test_deprecation_message_names_the_replacement(monkeypatch):
    monkeypatch.delenv("SKYPORTALAI_BASE_URL", raising=False)
    monkeypatch.setenv("SKYPORTAL_BASE_URL", "https://example.invalid")
    with pytest.warns(DeprecationWarning, match="use SKYPORTALAI_BASE_URL instead"):
        _env.get("SKYPORTALAI_BASE_URL")


def test_default_is_returned_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("SKYPORTALAI_API_KEY", raising=False)
    monkeypatch.delenv("SKYPORTAL_API_KEY", raising=False)
    assert _env.get("SKYPORTALAI_API_KEY", "fallback") == "fallback"


def test_lookup_reports_which_variable_supplied_the_value(monkeypatch):
    monkeypatch.delenv("SKYPORTALAI_API_KEY", raising=False)
    monkeypatch.setenv("SKYPORTAL_API_KEY", "old")
    with pytest.warns(DeprecationWarning):
        value, source = _env.lookup("SKYPORTALAI_API_KEY")
    assert (value, source) == ("old", "SKYPORTAL_API_KEY")


def test_get_from_reads_an_injected_mapping():
    """The agent is configured from a mapping, not os.environ."""
    assert _env.get_from({"SKYPORTALAI_AGENT_TOKEN": "t"}, "SKYPORTALAI_AGENT_TOKEN") == "t"


def test_get_from_falls_back_and_warns():
    with pytest.warns(DeprecationWarning, match="SKYPORTAL_AGENT_TOKEN"):
        assert _env.get_from({"SKYPORTAL_AGENT_TOKEN": "t"}, "SKYPORTALAI_AGENT_TOKEN") == "t"


def test_legacy_name_derivation_rejects_foreign_names():
    with pytest.raises(ValueError):
        _env.legacy_name("PATH")


def test_config_dir_prefers_the_new_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".skyportalai").mkdir()
    assert _env.config_dir() == tmp_path / ".skyportalai"


def test_config_dir_migrates_the_legacy_directory(tmp_path, monkeypatch):
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


def test_legacy_config_path_override_still_honoured(tmp_path, monkeypatch):
    monkeypatch.delenv("SKYPORTALAI_CONFIG_PATH", raising=False)
    monkeypatch.setenv("SKYPORTAL_CONFIG_PATH", str(tmp_path / "custom.yaml"))
    with pytest.warns(DeprecationWarning):
        assert _env.config_path("config.yaml", "SKYPORTALAI_CONFIG_PATH") == tmp_path / "custom.yaml"


def test_legacy_package_import_warns():
    """`import skyportal` keeps working for one release."""
    import importlib
    import sys

    sys.modules.pop("skyportal", None)
    with pytest.warns(DeprecationWarning, match="skyportalai"):
        module = importlib.import_module("skyportal")
    assert module.__version__


def _run_probe(tmp_path, *, enable_filter: bool, trigger: str) -> str:
    """Trigger a warning from a real module, not from ``__main__``.

    Python's default filter shows DeprecationWarning attributed to
    ``__main__``, so a ``python -c`` probe would report warnings as visible
    even with no filter installed and prove nothing about library code.
    """
    (tmp_path / "probe_module.py").write_text(
        "import warnings\n"
        "from skyportalai import _env\n"
        "def go():\n"
        f"    {trigger}\n"
    )
    driver = (
        "import io, contextlib, sys;"
        f"sys.path.insert(0, {str(tmp_path)!r});"
        "from skyportalai import _env;"
        + ("_env.enable_deprecation_warnings();" if enable_filter else "")
        + "import probe_module;"
        "buf = io.StringIO();"
        "ctx = contextlib.redirect_stderr(buf);"
        "ctx.__enter__();"
        "probe_module.go();"
        "ctx.__exit__(None, None, None);"
        "print(buf.getvalue())"
    )
    environment = {**os.environ, "SKYPORTAL_API_KEY": "x"}
    environment.pop("SKYPORTALAI_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", driver], capture_output=True, text=True, env=environment
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_deprecation_warnings_are_hidden_by_default(tmp_path):
    """Baseline: without the filter the notice is emitted and swallowed."""
    output = _run_probe(tmp_path, enable_filter=False, trigger="_env.get('SKYPORTALAI_API_KEY')")
    assert "SKYPORTAL_API_KEY" not in output


def test_enable_deprecation_warnings_makes_them_visible(tmp_path):
    """The entry points call this so users actually see what to migrate."""
    output = _run_probe(tmp_path, enable_filter=True, trigger="_env.get('SKYPORTALAI_API_KEY')")
    assert "SKYPORTAL_API_KEY is deprecated" in output
    assert "use SKYPORTALAI_API_KEY instead" in output


def test_enable_deprecation_warnings_does_not_unsilence_unrelated_warnings(tmp_path):
    """The filter is scoped to this package's 0.3.0 removal notices."""
    output = _run_probe(
        tmp_path,
        enable_filter=True,
        trigger="warnings.warn('some unrelated library notice', DeprecationWarning)",
    )
    assert "unrelated" not in output


def test_console_script_surfaces_a_legacy_variable(tmp_path):
    """End to end through the real entry point, as a user would hit it."""
    environment = {
        **os.environ,
        "SKYPORTAL_CONFIG_PATH": str(tmp_path / "config.yaml"),
        "SKYPORTAL_CREDENTIALS_PATH": str(tmp_path / "credentials.json"),
    }
    environment.pop("SKYPORTALAI_CONFIG_PATH", None)
    environment.pop("SKYPORTALAI_CREDENTIALS_PATH", None)

    result = subprocess.run(
        [sys.executable, "-c", "from skyportalai.cli import main; main()", "config", "show"],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "SKYPORTAL_CONFIG_PATH is deprecated" in result.stderr
    assert "use SKYPORTALAI_CONFIG_PATH instead" in result.stderr
