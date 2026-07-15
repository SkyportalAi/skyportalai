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
    monkeypatch.setenv("SKYPORTAL_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTAL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    for name in ("SKYPORTAL_API_KEY", "SKYPORTAL_ACCESS_TOKEN", "SKYPORTAL_BASE_URL", "SKYPORTAL_URL"):
        monkeypatch.delenv(name, raising=False)


def test_environment_api_key_has_precedence(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"access_token": "sk-file"}))
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk-env")

    settings = resolve_settings()

    assert settings.api_key == "sk-env"
    assert settings.api_key_source == "SKYPORTAL_API_KEY"


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
    monkeypatch.setenv("SKYPORTAL_BASE_URL", "https://two.example")

    with pytest.raises(SkyportalError, match="another SkyPortal deployment"):
        resolve_settings()


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