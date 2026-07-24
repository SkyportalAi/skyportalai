"""Interactive-shell coverage for multi-server chat scope."""

from io import StringIO

import pytest
from rich.console import Console

from skyportal.portal import ChatTurnResult, PortalError
from skyportal.shell import InteractiveShell


class FakeClient:
    def __init__(self):
        self.base_url = "https://app.skyportal.ai"
        self.scope_calls = []
        self.single_scope_calls = []
        self.begin_calls = []
        self.wait_calls = []
        self.submit_calls = []
        self.permission_mode = "ask"
        self.wait_results = []

    def is_authenticated(self):
        return True

    def servers(self):
        return [
            {"id": 7, "hostname": "gpu-7"},
            {"id": 9, "hostname": "gpu-9"},
        ]

    def select_chat_servers(
        self,
        chat_id,
        server_ids,
        *,
        active_server_id=None,
        active_host_id=None,
        selected_namespaces=None,
    ):
        self.scope_calls.append(
            (
                chat_id,
                server_ids,
                active_server_id,
                active_host_id,
                selected_namespaces,
            )
        )
        return {"success": True, "selected_server_ids": server_ids}

    def select_chat_server(self, chat_id, server_id):
        self.single_scope_calls.append((chat_id, server_id))
        return {"success": True, "server_id": server_id}

    def begin_chat_turn(
        self,
        message,
        chat_id=None,
        server_id=None,
        *,
        server_ids=None,
        active_server_id=None,
    ):
        self.begin_calls.append(
            (message, chat_id, server_id, server_ids, active_server_id)
        )
        return chat_id if chat_id is not None else 100

    def wait_for_chat(
        self, chat_id, after_sequence=0, timeout=300, on_progress=None, on_status=None
    ):
        self.wait_calls.append((chat_id, after_sequence, timeout, on_progress))
        if self.wait_results:
            return self.wait_results.pop(0)
        return ChatTurnResult(chat_id, "idle", [], [], after_sequence)

    def get_permission_mode(self):
        return self.permission_mode

    def submit_chat_approval(
        self, chat_id, approval, decision, *, autoapproved=False
    ):
        self.submit_calls.append(
            (chat_id, approval["approval_id"], decision, autoapproved)
        )
        return {"success": True}


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTAL_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    client = FakeClient()
    console = Console(file=StringIO(), force_terminal=False, width=100)
    instance = InteractiveShell(
        console=console,
        client_factory=lambda: client,
        session=object(),
        token_prompt=lambda _prompt: "",
    )
    return instance, client, console


def test_multi_server_scope_is_sent_atomically_with_first_turn(shell):
    instance, client, console = shell

    instance._cmd_server(["7", "9", "7"])
    instance._send_prompt("compare every selected host")

    assert instance.selected_server_ids == [7, 9]
    assert instance.selected_server_id == 7
    assert client.scope_calls == []
    assert client.begin_calls == [
        ("compare every selected host", None, None, [7, 9], 7),
    ]
    assert "Servers 7, 9 selected; 7 is the default" in console.file.getvalue()
    assert "servers#7,9" in "".join(part[1] for part in instance._prompt_fragments())


def test_multi_server_scope_updates_existing_chat(shell):
    instance, client, _console = shell
    instance.chat_id = 42

    instance._cmd_server(["7,9"])

    assert client.scope_calls == [(42, [7, 9], 7, None, None)]
    assert instance.selected_server_ids == [7, 9]


def test_single_server_first_turn_preserves_legacy_singular_request(shell):
    instance, client, _console = shell

    instance._cmd_server(["7"])
    instance._send_prompt("check this host")

    assert client.scope_calls == []
    assert client.begin_calls == [("check this host", None, 7, None, None)]


def test_single_server_existing_chat_uses_legacy_singular_endpoint(shell):
    instance, client, _console = shell
    instance.chat_id = 42

    instance._cmd_server(["7"])

    assert client.single_scope_calls == [(42, 7)]
    assert client.scope_calls == []
    assert instance.selected_server_ids == [7]


def test_multihost_scope_survives_autoapproval_resume_with_indefinite_wait(shell):
    instance, client, _console = shell
    instance.chat_id = 42
    instance.selected_server_ids = [7, 9]
    instance.selected_server_id = 7
    client.permission_mode = "autoapprove"
    client.wait_results = [ChatTurnResult(42, "idle", [], [], 0)]
    approval = {
        "approval_id": "multi-1",
        "type": "bash_command",
        "command": "hostname",
    }

    instance._process_turn(
        ChatTurnResult(42, "awaiting_approval", [], [approval], 0)
    )

    assert instance.selected_server_ids == [7, 9]
    assert instance.selected_server_id == 7
    assert client.scope_calls == []
    assert client.submit_calls == [(42, "multi-1", "approved", True)]
    assert client.wait_calls[0][0:3] == (42, 0, None)
    assert client.wait_calls[0][3] is not None


def test_auto_explicitly_clears_existing_chat_scope(shell):
    instance, client, console = shell
    instance.chat_id = 42
    instance.selected_server_ids = [7, 9]
    instance.selected_server_id = 7

    instance._cmd_server(["auto"])

    assert client.scope_calls == [(42, [], None, None, None)]
    assert instance.selected_server_ids == []
    assert instance.selected_server_id is None
    assert "automatic" in console.file.getvalue()


def test_new_chat_scope_rejects_unowned_server(shell):
    instance, _client, _console = shell

    with pytest.raises(PortalError, match="Server 11 was not found"):
        instance._cmd_server(["7", "11"])

    assert instance.selected_server_ids == []


@pytest.mark.parametrize("arguments", [[], ["auto", "7"], ["bad"], ["0"]])
def test_invalid_multi_server_syntax_does_not_change_scope(shell, arguments):
    instance, client, _console = shell

    instance._cmd_server(arguments)

    assert instance.selected_server_ids == []
    assert client.scope_calls == []
