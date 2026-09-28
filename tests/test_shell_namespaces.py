"""Interactive-shell coverage for Kubernetes namespace scope (/namespace)."""

from io import StringIO

import pytest
from rich.console import Console

from skyportalai.shell.interactive import InteractiveShell
from skyportalai.shell.portal import ChatTurnResult, PortalError


class FakeClient:
    def __init__(self):
        self.base_url = "https://app.skyportal.ai"
        self.scope_calls = []
        self.single_scope_calls = []
        self.begin_calls = []

    def is_authenticated(self):
        return True

    def servers(self):
        return [
            {
                "id": 5,
                "hostname": "backblaze-test-1",
                "target_kind": "kubernetes",
                "namespaces": ["kube-system", "storefront"],
            },
            {"id": 7, "hostname": "gpu-7", "target_kind": "ssh"},
        ]

    def select_chat_servers(
        self, chat_id, server_ids, *, active_server_id=None, active_host_id=None, selected_namespaces=None
    ):
        self.scope_calls.append((chat_id, server_ids, active_server_id, selected_namespaces))
        return {"success": True, "selected_server_ids": server_ids}

    def select_chat_server(self, chat_id, server_id):
        self.single_scope_calls.append((chat_id, server_id))
        return {"success": True, "server_id": server_id}

    def begin_chat_turn(
        self, message, chat_id=None, server_id=None, *, server_ids=None, active_server_id=None,
        selected_namespaces=None,
    ):
        self.begin_calls.append((message, chat_id, server_id, server_ids, active_server_id, selected_namespaces))
        return chat_id if chat_id is not None else 100

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=300, on_progress=None, on_status=None):
        return ChatTurnResult(chat_id, "idle", [], [], after_sequence)


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    client = FakeClient()
    console = Console(file=StringIO(), force_terminal=False, width=140)
    instance = InteractiveShell(
        console=console,
        client_factory=lambda: client,
        session=object(),
        token_prompt=lambda _prompt: "",
    )
    return instance, client, console


def test_selecting_a_cluster_explains_how_to_choose_namespaces(shell):
    instance, _client, console = shell

    instance._cmd_server(["backblaze-test-1"])

    output = console.file.getvalue()
    assert "backblaze-test-1 is a Kubernetes cluster" in output
    assert "/namespace all" in output
    assert "Namespaces: kube-system, storefront" in output


def test_all_namespaces_travels_with_the_first_message(shell):
    """The guide's path: /server <cluster>, then ask. The first turn must carry the scope."""
    instance, client, _console = shell

    instance._cmd_server(["backblaze-test-1"])
    instance._cmd_namespace(["all"])
    instance._send_prompt("which pods are not running, and why?")

    assert client.begin_calls == [
        ("which pods are not running, and why?", None, None, [5], 5, {"5": ["__all__"]}),
    ]
    assert "ns#all" in "".join(part[1] for part in instance._prompt_fragments())


def test_named_namespaces_are_sent_as_chosen(shell):
    instance, client, console = shell

    instance._cmd_server(["backblaze-test-1"])
    instance._cmd_namespace(["storefront,kube-system"])
    instance._send_prompt("what is failing?")

    assert client.begin_calls[0][5] == {"5": ["storefront", "kube-system"]}
    assert "backblaze-test-1: storefront, kube-system" in console.file.getvalue()


def test_namespace_change_updates_an_existing_chat(shell):
    instance, client, _console = shell
    instance.chat_id = 42

    instance._cmd_server(["backblaze-test-1"])
    instance._cmd_namespace(["all"])

    assert client.single_scope_calls == [(42, 5)]
    assert client.scope_calls == [(42, [5], 5, {"5": ["__all__"]})]


def test_clear_sends_an_empty_namespace_scope(shell):
    instance, client, _console = shell
    instance.chat_id = 42
    instance._cmd_server(["backblaze-test-1"])
    instance._cmd_namespace(["all"])

    instance._cmd_namespace(["clear"])

    assert client.scope_calls[-1] == (42, [5], 5, {})
    assert instance.selected_namespaces == {}


def test_unknown_namespace_is_refused_without_changing_scope(shell):
    instance, client, _console = shell
    instance._cmd_server(["backblaze-test-1"])

    with pytest.raises(PortalError, match="Namespace default not found on backblaze-test-1"):
        instance._cmd_namespace(["default"])

    assert instance.selected_namespaces == {}
    assert client.scope_calls == []


@pytest.mark.parametrize("arguments", [[], ["all", "storefront"]])
def test_invalid_namespace_syntax_changes_nothing(shell, arguments):
    instance, client, console = shell
    instance._cmd_server(["backblaze-test-1"])

    instance._cmd_namespace(arguments)

    assert instance.selected_namespaces == {}
    assert client.scope_calls == []
    assert "Usage:" in console.file.getvalue()


def test_namespace_needs_a_selected_cluster(shell):
    instance, client, console = shell
    instance._cmd_server(["gpu-7"])

    instance._cmd_namespace(["all"])

    assert "Select a Kubernetes cluster first" in console.file.getvalue()
    assert instance.selected_namespaces == {}
    assert client.scope_calls == []


def test_ssh_host_keeps_the_legacy_first_turn(shell):
    instance, client, console = shell

    instance._cmd_server(["gpu-7"])
    instance._send_prompt("check disk usage")

    assert client.begin_calls == [("check disk usage", None, 7, None, None, None)]
    assert "Kubernetes cluster" not in console.file.getvalue()


def test_switching_away_from_a_cluster_drops_its_namespaces(shell):
    instance, _client, _console = shell
    instance._cmd_server(["backblaze-test-1"])
    instance._cmd_namespace(["all"])

    instance._cmd_server(["gpu-7"])

    assert instance.selected_namespaces == {}
    assert "ns#" not in "".join(part[1] for part in instance._prompt_fragments())
