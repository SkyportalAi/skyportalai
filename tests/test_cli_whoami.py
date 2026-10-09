"""``skyportalai whoami`` through the real Typer app and SDK client."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from skyportalai.cli.main import app

runner = CliRunner()

ENDPOINT = "https://api.test/api/v1/agent/permission/effective/"
PAYLOAD = {
    "account": {"username": "ada", "email": "ada@example.com"},
    "permission_mode": "ask",
    "read_only_mode": False,
    "own_environments": {"Dev": "unrestricted", "Production": "read_only"},
    "teams": [
        {
            "name": "Platform",
            "role": "maintainer",
            "environments": {"Production": "read_only", "Research": None, "Staging": "standard"},
        }
    ],
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.delenv("SKYPORTALAI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SKYPORTALAI_BASE_URL", raising=False)
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "sk-test")


def _invoke(*arguments: str):
    return runner.invoke(app, [*arguments, "--base-url", "https://api.test", "whoami"])


def test_whoami_shows_account_mode_and_each_environment(requests_mock):
    requests_mock.get(ENDPOINT, json=PAYLOAD)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert requests_mock.last_request.headers["Authorization"] == "Bearer sk-test"
    lines = result.stdout.splitlines()
    assert "Account: ada <ada@example.com>" in lines
    assert "Approval mode: ask" in lines
    assert "Read-only mode: off" in lines
    assert "  Production  read_only" in lines
    assert "Team Platform (role: maintainer), ceiling per environment:" in lines
    assert "  Research    denied" in lines


def test_whoami_json_prints_the_raw_payload(requests_mock):
    requests_mock.get(ENDPOINT, json=PAYLOAD)

    result = _invoke("--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == PAYLOAD


def test_whoami_against_a_deployment_without_the_endpoint(requests_mock):
    requests_mock.get(ENDPOINT, status_code=404, text="<h1>Not Found</h1>")

    result = _invoke()

    assert result.exit_code == 1
    assert "does not report effective permissions yet" in result.stderr
    assert "Traceback" not in result.output
