"""Backwards compatibility for the pre-0.2.0 SKYPORTAL_* names and config dir."""

from __future__ import annotations

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
