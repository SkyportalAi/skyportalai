"""Tests for public CLI configuration resolution."""

from __future__ import annotations

import json
import os

import pytest
import yaml

from skyportalai._exceptions import SkyportalError
from skyportalai.cli.config import resolve_settings, save_connection_config


@pytest.fixture(autouse=True)
def isolated_cli_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    for name in ("SKYPORTALAI_API_KEY", "SKYPORTALAI_ACCESS_TOKEN", "SKYPORTALAI_BASE_URL", "SKYPORTALAI_URL"):
        monkeypatch.delenv(name, raising=False)


def test_environment_api_key_has_precedence(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "sk-file"}))
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "sk-env")

    settings = resolve_settings()

    assert settings.api_key == "sk-env"
    assert settings.api_key_source == "SKYPORTALAI_API_KEY"


def test_existing_cli_files_are_supported(tmp_path):
    config = tmp_path / "config.yaml"
    credentials = tmp_path / "credentials.json"
    config.write_text(yaml.safe_dump({"portal": {"base_url": "https://portal.example/", "request_timeout": 12}}))
    credentials.write_text(json.dumps({"access_token": "sk-file", "base_url": "https://portal.example"}))

    settings = resolve_settings()

    assert settings.api_key == "sk-file"
    assert settings.base_url == "https://portal.example"
    assert settings.timeout == 12


def test_credentials_are_scoped_to_the_selected_deployment(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "sk-file", "base_url": "https://one.example"}))
    monkeypatch.setenv("SKYPORTALAI_BASE_URL", "https://two.example")

    settings = resolve_settings()

    assert settings.api_key is None
    # Compared whole rather than by substring: the message has to name both URLs,
    # the file and the recovery command, and a containment check for a URL is the
    # bug pattern CodeQL's py/incomplete-url-substring-sanitization looks for.
    assert settings.credential_conflict == (
        "Stored credentials belong to another Skyportal deployment (https://one.example), "
        "but the selected base URL is https://two.example. "
        f"Run 'skyportalai logout' to clear them ({credentials}), "
        "or keep them by unsetting SKYPORTALAI_BASE_URL."
    )


def test_the_conflict_names_the_setting_that_actually_selected_the_url(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "sk-file", "base_url": "https://one.example"}))
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"portal": {"base_url": "https://two.example"}}))

    settings = resolve_settings()

    assert settings.credential_conflict == (
        "Stored credentials belong to another Skyportal deployment (https://one.example), "
        "but the selected base URL is https://two.example. "
        f"Run 'skyportalai logout' to clear them ({credentials}), "
        "or keep them by running 'skyportalai config set --base-url https://one.example'."
    )


def test_a_base_url_flag_is_not_fixable_by_editing_the_config(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "sk-file", "base_url": "https://one.example"}))

    settings = resolve_settings(base_url="https://two.example")

    assert settings.credential_conflict == (
        "Stored credentials belong to another Skyportal deployment (https://one.example), "
        "but the selected base URL is https://two.example. "
        f"Run 'skyportalai logout' to clear them ({credentials}), "
        "or keep them by dropping --base-url."
    )


def test_the_marketing_host_is_not_a_different_deployment(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "sk-file", "base_url": "https://app.skyportal.ai"}))
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"portal": {"base_url": "https://skyportal.ai"}}))

    settings = resolve_settings()

    assert settings.credential_conflict is None
    assert settings.api_key == "sk-file"
    assert settings.base_url == "https://app.skyportal.ai"


def test_an_unparseable_credential_file_is_reported_not_raised(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{not json")

    settings = resolve_settings()

    assert settings.api_key is None
    assert settings.credential_conflict.startswith(f"Invalid Skyportal credentials in {credentials}:")
    assert settings.credential_conflict.endswith("Run 'skyportalai logout' to remove the file.")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs enforced file permissions")
def test_an_unreadable_credential_file_is_not_advised_away(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "sk-file"}))
    credentials.chmod(0o000)

    settings = resolve_settings()

    assert settings.api_key is None
    # Deleting a valid credential because the process could not open it would
    # turn a permissions problem into a lost key.
    assert "logout" not in settings.credential_conflict
    assert settings.credential_conflict.endswith("Check the file's permissions.")


def test_a_matching_stored_credential_still_resolves(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "sk-file", "base_url": "https://one.example"}))
    monkeypatch.setenv("SKYPORTALAI_BASE_URL", "https://one.example/")

    settings = resolve_settings()

    assert settings.api_key == "sk-file"
    assert settings.credential_conflict is None


def test_save_connection_config_is_private_and_legacy_compatible(tmp_path):
    path = save_connection_config(base_url="https://portal.example/", timeout=9)

    assert yaml.safe_load(path.read_text()) == {
        "portal": {"base_url": "https://portal.example", "request_timeout": 9},
    }
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_timeout_is_reported(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"portal": {"request_timeout": "never"}}))

    with pytest.raises(SkyportalError, match="Invalid request timeout"):
        resolve_settings()