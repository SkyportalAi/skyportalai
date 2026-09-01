"""`login` and `github-token set` name the instance before prompting.

A persistent ``config.yaml`` outranks the shipped default forever, so without
these lines the instance that issues (and scopes) the credential is invisible
at the moment the prompt appears.
"""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from skyportalai.cli.main import app

runner = CliRunner()


class FakeClient:
    """Enough of ``SkyportalClient`` for the ``--token`` and PAT paths."""

    def __init__(self):
        self.saved: list[str] = []

    def set_access_token(self, token):
        self.saved.append(token)

    def save_github_token(self, pat, repo=None):
        self.saved.append(pat)
        return {"success": True, "login": "octocat", "masked_token": "ghp_****abc"}


def _isolated(monkeypatch, tmp_path, base_url=None):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    for name in ("SKYPORTALAI_API_KEY", "SKYPORTALAI_ACCESS_TOKEN", "SKYPORTALAI_BASE_URL", "SKYPORTALAI_URL"):
        monkeypatch.delenv(name, raising=False)
    # Keep rich from wrapping the file path mid-assertion.
    monkeypatch.setenv("COLUMNS", "300")
    if base_url is not None:
        config_path.write_text(yaml.safe_dump({"portal": {"base_url": base_url, "request_timeout": 30}}))
    client = FakeClient()
    monkeypatch.setattr("skyportalai.cli.shell_commands._portal_client", lambda: client)
    return config_path, client


def test_login_names_the_default_target_without_warning(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)

    result = runner.invoke(app, ["login", "--token"], input="sk-test\n")

    assert result.exit_code == 0, result.output
    assert "Connecting to https://app.skyportal.ai" in result.output
    assert "Warning" not in result.output


def test_login_warns_that_a_loopback_target_came_from_the_config_file(monkeypatch, tmp_path):
    config_path, _ = _isolated(monkeypatch, tmp_path, base_url="http://localhost:8000")

    result = runner.invoke(app, ["login", "--token"], input="sk-test\n")

    assert result.exit_code == 0, result.output
    assert "Connecting to http://localhost:8000" in result.output
    assert "base_url is a local address (http://localhost:8000)" in result.output
    assert str(config_path) in result.output
    assert "skyportalai configure" in result.output


def test_login_warns_for_any_loopback_spelling(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path, base_url="http://app.localhost:8000")

    result = runner.invoke(app, ["login", "--token"], input="sk-test\n")

    assert result.exit_code == 0, result.output
    assert "base_url is a local address" in result.output


def test_login_never_echoes_userinfo_from_the_configured_url(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path, base_url="https://user:secret@portal.example")

    result = runner.invoke(app, ["login", "--token"], input="sk-test\n")

    assert result.exit_code == 0, result.output
    assert "Connecting to https://portal.example" in result.output
    assert "secret" not in result.output


def test_github_token_set_names_the_target_before_prompting(monkeypatch, tmp_path):
    config_path, client = _isolated(monkeypatch, tmp_path, base_url="http://127.0.0.1:8000")

    result = runner.invoke(app, ["github-token", "set"], input="ghp_test\n")

    assert result.exit_code == 0, result.output
    assert "Connecting to http://127.0.0.1:8000" in result.output
    assert str(config_path) in result.output
    assert client.saved == ["ghp_test"]
