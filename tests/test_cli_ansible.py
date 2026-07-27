from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from skyportalai.cli.context import CLIContext
from skyportalai.cli.main import app
from skyportalai.types import AnsibleDeployment, AnsiblePlaybook

runner = CliRunner()


class FakeAnsibleResource:
    def __init__(self):
        self.calls = []

    def create(self, name, content, *, description):
        self.calls.append(("create", name, content, description))
        return AnsiblePlaybook(id=5, name=name, content=content, description=description)

    def list(self):
        self.calls.append(("list",))
        return [AnsiblePlaybook(id=5, name="bootstrap", updated_at="today")]

    def get(self, playbook_id):
        self.calls.append(("get", playbook_id))
        return AnsiblePlaybook(id=playbook_id, name="bootstrap", content="- hosts: all\n")

    def update(self, playbook_id, **changes):
        self.calls.append(("update", playbook_id, changes))
        return AnsiblePlaybook(id=playbook_id, name=changes.get("name") or "bootstrap")

    def delete(self, playbook_id):
        self.calls.append(("delete", playbook_id))
        return {"success": True, "playbook_id": playbook_id}

    def deploy(self, playbook_id, *, server_id):
        self.calls.append(("deploy", playbook_id, server_id))
        return AnsibleDeployment(chat_id=31, playbook_id=playbook_id, server_id=server_id)


def _fake_client(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYPORTAL_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTAL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk-test")
    resource = FakeAnsibleResource()
    monkeypatch.setattr(
        CLIContext,
        "client",
        lambda self: SimpleNamespace(ansible=resource),
    )
    return resource


def test_create_reads_file_and_lifecycle_commands(monkeypatch, tmp_path):
    resource = _fake_client(monkeypatch, tmp_path)
    playbook = tmp_path / "playbook.yml"
    playbook.write_text("- hosts: all\n  tasks: []\n")

    created = runner.invoke(
        app,
        ["--json", "ansible", "create", "bootstrap", "-f", str(playbook)],
    )
    listed = runner.invoke(app, ["--json", "ansible", "list"])
    shown = runner.invoke(app, ["--json", "ansible", "show", "5"])
    updated = runner.invoke(
        app,
        ["--json", "ansible", "update", "5", "--name", "base"],
    )
    deleted = runner.invoke(app, ["--json", "ansible", "delete", "5", "--yes"])
    deployed = runner.invoke(
        app,
        ["--json", "ansible", "deploy", "5", "--server", "12"],
    )

    for result in (created, listed, shown, updated, deleted, deployed):
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ok"] is True
    assert [call[0] for call in resource.calls] == [
        "create", "list", "get", "update", "delete", "deploy",
    ]


def test_update_requires_a_change(monkeypatch, tmp_path):
    _fake_client(monkeypatch, tmp_path)
    result = runner.invoke(app, ["--json", "ansible", "update", "5"])
    assert result.exit_code == 2
    assert json.loads(result.output)["ok"] is False
