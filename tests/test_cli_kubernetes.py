from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from skyportalai.cli.context import CLIContext
from skyportalai.cli.main import app
from skyportalai.types import KubernetesCluster

runner = CliRunner()


class FakeKubernetesResource:
    def __init__(self):
        self.calls = []

    def connect(self, name, kubeconfig, *, environment):
        self.calls.append(("connect", name, kubeconfig, environment))
        return KubernetesCluster(
            id=9,
            name=name,
            environment=environment,
            status="connected",
            connection_verified=True,
        )

    def list(self):
        self.calls.append(("list",))
        return [KubernetesCluster(id=9, name="prod", namespaces=["default"])]

    def disconnect(self, cluster_id):
        self.calls.append(("disconnect", cluster_id))
        return {"success": True, "cluster_id": cluster_id}


def _fake_client(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYPORTAL_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTAL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk-test")
    resource = FakeKubernetesResource()
    monkeypatch.setattr(
        CLIContext,
        "client",
        lambda self: SimpleNamespace(kubernetes=resource),
    )
    return resource


def test_connect_reads_file_and_never_prints_kubeconfig(monkeypatch, tmp_path):
    resource = _fake_client(monkeypatch, tmp_path)
    config = tmp_path / "config"
    config.write_text("apiVersion: v1\nkind: Config\nclusters: []\nsecret: do-not-print")

    result = runner.invoke(
        app,
        ["--json", "kubernetes", "connect", "prod", "-k", str(config), "-e", "Production"],
    )

    assert result.exit_code == 0
    assert resource.calls[0][0:2] == ("connect", "prod")
    assert "do-not-print" not in result.output
    assert json.loads(result.stdout)["data"]["id"] == 9


def test_list_and_disconnect(monkeypatch, tmp_path):
    resource = _fake_client(monkeypatch, tmp_path)
    listed = runner.invoke(app, ["--json", "kubernetes", "list"])
    removed = runner.invoke(app, ["--json", "kubernetes", "disconnect", "9", "--yes"])

    assert listed.exit_code == 0
    assert removed.exit_code == 0
    assert resource.calls == [("list",), ("disconnect", 9)]


def test_disconnect_json_mode_requires_yes(monkeypatch, tmp_path):
    _fake_client(monkeypatch, tmp_path)
    result = runner.invoke(app, ["--json", "kubernetes", "disconnect", "9"])

    assert result.exit_code != 0
    parsed = json.loads(result.output if result.output.strip() else "{}")
    assert parsed.get("ok") is False
