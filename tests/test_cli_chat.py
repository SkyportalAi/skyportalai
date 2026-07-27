"""Tests for public chat subcommands."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from skyportalai import APIError
from skyportalai.cli.main import CLIContext, app
from skyportalai.types import ApprovalResult, ChatStatus, Message, MessagesPage, PendingApproval

runner = CliRunner()


class FakeChatResource:
    def __init__(self):
        self.calls = []
        self.wait_status = ChatStatus(status="idle")

    def create_chat(
        self,
        message,
        *,
        server_id=None,
        server_ids=None,
        active_server_id=None,
        active_host_id=None,
        selected_namespaces=None,
    ):
        self.calls.append(
            (
                "create_chat",
                message,
                server_id,
                server_ids,
                active_server_id,
                active_host_id,
                selected_namespaces,
            )
        )
        return SimpleNamespace(chat_id=42, raw={"status": "processing"})

    def send_message(self, chat_id, message):
        self.calls.append(("send_message", chat_id, message))
        return {"status": "processing"}

    def wait(self, chat_id, *, timeout, poll_interval):
        self.calls.append(("wait", chat_id, timeout, poll_interval))
        return self.wait_status

    def get_status(self, chat_id):
        self.calls.append(("get_status", chat_id))
        return self.wait_status

    def get_messages(self, chat_id, *, after_sequence, limit):
        self.calls.append(("get_messages", chat_id, after_sequence, limit))
        return MessagesPage(messages=[Message(role="assistant", content="done", sequence=3)])

    def approve(self, chat_id, approval_id, *, approval_type, command):
        self.calls.append(("approve", chat_id, approval_id, approval_type, command))
        return ApprovalResult(success=True, decision="approved")

    def reject(self, chat_id, approval_id, *, approval_type, reason):
        self.calls.append(("reject", chat_id, approval_id, approval_type, reason))
        return ApprovalResult(success=True, decision="rejected")

    def cancel(self, chat_id, *, reason):
        self.calls.append(("cancel", chat_id, reason))
        return {"success": True, "status": "cancelled"}

    def select_servers(
        self,
        chat_id,
        server_ids,
        *,
        active_server_id=None,
        active_host_id=None,
        selected_namespaces=None,
    ):
        self.calls.append(
            (
                "select_servers",
                chat_id,
                server_ids,
                active_server_id,
                active_host_id,
                selected_namespaces,
            )
        )
        return {
            "success": True,
            "selected_server_ids": server_ids,
            "active_server_id": active_server_id,
            "active_host_id": active_host_id,
            "selected_namespaces": selected_namespaces or {},
        }


@pytest.fixture
def fake_client(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYPORTAL_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTAL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk-test")
    chat = FakeChatResource()
    client = SimpleNamespace(chat=chat)
    monkeypatch.setattr(CLIContext, "client", lambda self: client)
    return client


def test_send_wait_targets_server_and_returns_json(fake_client):
    result = runner.invoke(
        app,
        [
            "--json",
            "chat",
            "send",
            "edit the file",
            "--server",
            "7",
            "--wait",
            "--timeout",
            "12",
            "--poll-interval",
            "0",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls == [
        ("create_chat", "edit the file", 7, None, None, None, None),
        ("wait", 42, 12.0, 0.0),
        ("get_messages", 42, 0, 100),
    ]
    payload = json.loads(result.stdout)
    assert payload["data"]["chat_id"] == 42
    assert payload["data"]["status"] == "idle"
    assert payload["data"]["messages"][0]["content"] == "done"


def test_send_wait_defaults_to_indefinite_for_long_running_turns(fake_client):
    result = runner.invoke(
        app,
        ["--json", "chat", "send", "inspect every host", "--server", "7", "--wait"],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls[0:2] == [
        ("create_chat", "inspect every host", 7, None, None, None, None),
        ("wait", 42, None, 1.0),
    ]


def test_chat_wait_defaults_indefinite_and_keeps_finite_timeout_opt_in(fake_client):
    indefinite = runner.invoke(app, ["--json", "chat", "wait", "42"])
    finite = runner.invoke(
        app,
        ["--json", "chat", "wait", "42", "--timeout", "12", "--poll-interval", "0"],
    )

    assert indefinite.exit_code == 0
    assert finite.exit_code == 0
    assert fake_client.chat.calls == [
        ("wait", 42, None, 1.0),
        ("wait", 42, 12.0, 0.0),
    ]


def test_send_without_wait_does_not_fetch_messages(fake_client):
    result = runner.invoke(app, ["--json", "chat", "send", "inspect", "--server", "7"])

    assert result.exit_code == 0
    assert fake_client.chat.calls == [
        ("create_chat", "inspect", 7, None, None, None, None)
    ]
    assert json.loads(result.stdout)["data"]["messages"] == []


def test_send_creates_first_turn_with_atomic_multi_host_scope(fake_client):
    result = runner.invoke(
        app,
        [
            "--json",
            "chat",
            "send",
            "compare the clusters",
            "--server",
            "7",
            "--server",
            "9",
            "--server",
            "7",
            "--namespace",
            "9=default",
            "--namespace",
            "9=vllm",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls == [
        (
            "create_chat",
            "compare the clusters",
            None,
            [7, 9],
            7,
            None,
            {"9": ["default", "vllm"]},
        )
    ]


def test_send_uses_explicit_active_host_as_first_turn_default(fake_client):
    result = runner.invoke(
        app,
        [
            "--json",
            "chat",
            "send",
            "inspect",
            "--server",
            "7",
            "--server",
            "9",
            "--active-host",
            "9",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls == [
        ("create_chat", "inspect", None, [7, 9], 9, 9, None)
    ]


def test_send_follow_up_rejects_server_option(fake_client):
    result = runner.invoke(
        app,
        ["--json", "chat", "send", "continue", "--chat-id", "42", "--server", "7"],
    )

    assert result.exit_code == 2
    assert fake_client.chat.calls == []
    assert "--server can only" in json.loads(result.stderr)["error"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--active-server", "7"], "require at least one --server"),
        (["--server", "7", "--active-server", "9"], "--active-server"),
        (
            [
                "--server",
                "7",
                "--server",
                "9",
                "--active-server",
                "7",
                "--active-host",
                "9",
            ],
            "must match",
        ),
        (["--server", "7", "--namespace", "9=default"], "included with --server"),
        (["--server", "7", "--namespace", "bad"], "SERVER_ID=NAMESPACE"),
    ],
)
def test_send_rejects_invalid_first_turn_scope(fake_client, arguments, message):
    result = runner.invoke(
        app,
        ["--json", "chat", "send", "inspect", *arguments],
    )

    assert result.exit_code == 2
    assert fake_client.chat.calls == []
    assert message in json.loads(result.stderr)["error"]


def test_status_awaiting_approval_uses_actionable_exit_code(fake_client):
    fake_client.chat.wait_status = ChatStatus(
        status="awaiting_approval",
        pending_approvals=[PendingApproval(approval_id="a1")],
    )

    result = runner.invoke(app, ["--json", "chat", "status", "42"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["data"]["pending_approvals"][0]["approval_id"] == "a1"


def test_messages_forwards_cursor_and_limit(fake_client):
    result = runner.invoke(
        app,
        ["chat", "messages", "42", "--after-sequence", "2", "--limit", "10"],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls == [("get_messages", 42, 2, 10)]
    assert "[3] assistant: done" in result.stdout


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["approve", "42", "a1", "--type", "plan", "--command", "deploy"],
         ("approve", 42, "a1", "plan", "deploy")),
        (["reject", "42", "a1", "--reason", "unsafe"],
         ("reject", 42, "a1", "bash_command", "unsafe")),
        (["cancel", "42", "--reason", "done"], ("cancel", 42, "done")),
    ],
)
def test_mutating_commands_are_thin_sdk_wrappers(fake_client, arguments, expected):
    result = runner.invoke(app, ["--json", "chat", *arguments])

    assert result.exit_code == 0
    assert fake_client.chat.calls == [expected]


def test_select_servers_sets_multi_host_and_namespace_scope(fake_client):
    result = runner.invoke(
        app,
        [
            "--json",
            "chat",
            "select-servers",
            "42",
            "--server",
            "9",
            "--server",
            "12",
            "--active-server",
            "9",
            "--namespace",
            "12=default",
            "--namespace",
            "12=vllm",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls == [
        ("get_status", 42),
        ("select_servers", 42, [9, 12], 9, None, {"12": ["default", "vllm"]}),
    ]
    assert json.loads(result.stdout)["data"]["selected_server_ids"] == [9, 12]


def test_select_servers_can_clear_scope_and_namespaces(fake_client):
    result = runner.invoke(
        app,
        [
            "--json",
            "chat",
            "select-servers",
            "42",
            "--clear-scope",
            "--clear-namespaces",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls == [
        ("get_status", 42),
        ("select_servers", 42, [], None, None, {}),
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ([], "at least one --server"),
        (["--server", "9", "--clear-scope"], "cannot be combined"),
        (["--server", "9", "--active-server", "12"], "--active-server"),
        (["--server", "9", "--namespace", "12=default"], "included with --server"),
        (["--server", "9", "--namespace", "bad"], "SERVER_ID=NAMESPACE"),
        (
            ["--server", "9", "--namespace", "9=default", "--clear-namespaces"],
            "cannot be combined",
        ),
    ],
)
def test_select_servers_rejects_invalid_scope_options(fake_client, arguments, message):
    result = runner.invoke(
        app,
        ["--json", "chat", "select-servers", "42", *arguments],
    )

    assert result.exit_code == 2
    assert fake_client.chat.calls == []
    assert message in json.loads(result.stderr)["error"]


def test_select_servers_refuses_to_rescope_a_processing_chat(fake_client):
    fake_client.chat.wait_status = ChatStatus(status="processing")

    result = runner.invoke(
        app,
        ["--json", "chat", "select-servers", "42", "--server", "9"],
    )

    assert result.exit_code == 2
    assert fake_client.chat.calls == [("get_status", 42)]
    assert "finish the current turn" in json.loads(result.stderr)["error"]


def test_select_servers_defaults_active_server_to_first_deduplicated_id(fake_client):
    result = runner.invoke(
        app,
        [
            "--json",
            "chat",
            "select-servers",
            "42",
            "--server",
            "9",
            "--server",
            "12",
            "--server",
            "9",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls == [
        ("get_status", 42),
        ("select_servers", 42, [9, 12], 9, None, None),
    ]


def test_select_servers_refuses_to_rescope_while_approval_is_pending(fake_client):
    fake_client.chat.wait_status = ChatStatus(status="awaiting_approval")

    result = runner.invoke(
        app,
        ["--json", "chat", "select-servers", "42", "--server", "9"],
    )

    assert result.exit_code == 2
    assert fake_client.chat.calls == [("get_status", 42)]
    assert "approval" in json.loads(result.stderr)["error"]


def test_select_servers_allows_rescope_while_chat_awaits_input(fake_client):
    fake_client.chat.wait_status = ChatStatus(status="awaiting_input")

    result = runner.invoke(
        app,
        ["--json", "chat", "select-servers", "42", "--server", "9"],
    )

    assert result.exit_code == 0
    assert fake_client.chat.calls == [
        ("get_status", 42),
        ("select_servers", 42, [9], 9, None, None),
    ]


def test_sdk_errors_are_clean_json_without_traceback(fake_client, monkeypatch):
    def fail(chat_id):
        raise APIError("chat not found", status_code=404)

    monkeypatch.setattr(fake_client.chat, "get_status", fail)

    result = runner.invoke(app, ["--json", "chat", "status", "404"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"] == "chat not found"
    assert "Traceback" not in result.output
