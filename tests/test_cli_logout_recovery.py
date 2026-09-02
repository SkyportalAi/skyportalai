"""`logout` stays reachable when the stored credential cannot be used.

A credential saved against another deployment used to abort configuration
resolution, which runs above every command — including the one whose whole job
is to delete that credential. The CLI was wedged until the file was removed by
hand.
"""

from __future__ import annotations

import json

import yaml
from typer.testing import CliRunner

from skyportalai.cli.main import app

runner = CliRunner()


def _mismatched(monkeypatch, tmp_path, credential_body=None):
    config_path = tmp_path / "config.yaml"
    credentials_path = tmp_path / "credentials.json"
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(credentials_path))
    for name in ("SKYPORTALAI_API_KEY", "SKYPORTALAI_ACCESS_TOKEN", "SKYPORTALAI_BASE_URL", "SKYPORTALAI_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "300")
    config_path.write_text(yaml.safe_dump({"portal": {"base_url": "https://app.skyportal.ai"}}))
    credentials_path.write_text(
        credential_body
        if credential_body is not None
        else json.dumps({"access_token": "sk-local", "base_url": "http://localhost:8000"})
    )
    return credentials_path


def test_logout_clears_credentials_from_another_deployment(monkeypatch, tmp_path):
    credentials_path = _mismatched(monkeypatch, tmp_path)

    result = runner.invoke(app, ["logout"])

    assert result.exit_code == 0, result.output
    assert not credentials_path.exists()


def test_logout_clears_a_credential_file_that_cannot_be_parsed(monkeypatch, tmp_path):
    credentials_path = _mismatched(monkeypatch, tmp_path, credential_body="{not json")

    result = runner.invoke(app, ["logout"])

    assert result.exit_code == 0, result.output
    assert not credentials_path.exists()


def test_a_command_needing_the_credential_reports_the_conflict_without_a_traceback(monkeypatch, tmp_path):
    credentials_path = _mismatched(monkeypatch, tmp_path)

    result = runner.invoke(app, ["chat", "status", "1"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "another Skyportal deployment" in result.output
    assert "skyportalai logout" in result.output
    assert str(credentials_path) in result.output
    assert credentials_path.exists()


def test_the_advice_names_the_environment_variable_not_the_flag(monkeypatch, tmp_path):
    # Click fills --base-url from SKYPORTALAI_BASE_URL, so the CLI must not blame
    # a flag the user never typed; unsetting the variable is the only thing that
    # would actually change the selected URL here.
    _mismatched(monkeypatch, tmp_path)
    monkeypatch.setenv("SKYPORTALAI_BASE_URL", "https://app.skyportal.ai")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0, result.output
    assert "unsetting SKYPORTALAI_BASE_URL" in result.output


def test_the_advice_names_the_flag_when_the_flag_was_typed(monkeypatch, tmp_path):
    _mismatched(monkeypatch, tmp_path)

    result = runner.invoke(app, ["--base-url", "https://app.skyportal.ai", "config", "show"])

    assert result.exit_code == 0, result.output
    assert "dropping --base-url" in result.output


def test_config_show_reports_the_conflict_as_data(monkeypatch, tmp_path):
    credentials_path = _mismatched(monkeypatch, tmp_path)

    result = runner.invoke(app, ["--json", "config", "show"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["authenticated"] is False
    assert "skyportalai logout" in payload["data"]["credential_conflict"]
    assert "sk-local" not in result.stdout
    assert credentials_path.exists()


def test_config_show_stays_quiet_when_there_is_no_conflict(monkeypatch, tmp_path):
    _mismatched(monkeypatch, tmp_path, credential_body=json.dumps({"access_token": "sk-live", "base_url": "https://app.skyportal.ai"}))

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0, result.output
    assert "Credential problem" not in result.output
