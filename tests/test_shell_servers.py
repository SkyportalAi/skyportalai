"""/servers is name-first: no internal id or kind columns, and /server selects by hostname."""

from io import StringIO

import pytest
from rich.console import Console

from skyportal.portal import PortalError
from skyportal.shell import InteractiveShell

PAYLOAD = [
    {"id": "102", "name": "cluster-dev", "status": "connected", "host_type": "Staging",
     "target_kind": "kubernetes", "vcpu": 16, "ram": 31, "gpus": 0},
    {"id": "7", "name": "gpu-7", "status": "connected", "host_type": "Production",
     "target_kind": "ssh", "vcpu": 32, "ram": 128, "gpus": 2},
]


class FakeClient:
    base_url = "https://app.skyportal.ai"

    def __init__(self, payload):
        self._payload = payload
        self.single_scope_calls = []
        self.scope_calls = []

    def is_authenticated(self):
        return True

    def servers(self):
        return self._payload

    def select_chat_server(self, chat_id, server_id):
        self.single_scope_calls.append((chat_id, server_id))

    def select_chat_servers(self, chat_id, server_ids, active_server_id=None):
        self.scope_calls.append((chat_id, server_ids, active_server_id))


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTAL_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    out = StringIO()
    console = Console(file=out, width=160, force_terminal=False)
    client = FakeClient(PAYLOAD)
    instance = InteractiveShell(
        console=console,
        client_factory=lambda: client,
        session=object(),
        token_prompt=lambda _prompt: "",
    )
    return instance, client, out


def test_table_has_no_id_or_kind_columns(shell):
    instance, _, out = shell
    instance._cmd_servers([])
    text = out.getvalue()
    assert "Kind" not in text
    assert "102" not in text
    for expected in ("Name", "Status", "Environment", "Resources"):
        assert expected in text
    assert "cluster-dev" in text
    assert "16 vCPU / 31 GB RAM / 0 GPU" in text
    assert "32 vCPU / 128 GB RAM / 2 GPU" in text
    assert "/server <name>" in text


def test_select_by_name(shell):
    instance, _, out = shell
    instance._cmd_server(["cluster-dev"])
    assert instance.selected_server_ids == [102]
    assert instance.selected_server_names == ["cluster-dev"]
    assert "Server cluster-dev selected" in out.getvalue()


def test_select_multiple_names_dedupes_and_keeps_order(shell):
    instance, _, out = shell
    instance._cmd_server(["gpu-7", "cluster-dev", "gpu-7"])
    assert instance.selected_server_ids == [7, 102]
    assert instance.selected_server_names == ["gpu-7", "cluster-dev"]
    assert "gpu-7 is the default" in out.getvalue()


def test_numeric_id_still_accepted(shell):
    instance, _, _ = shell
    instance._cmd_server(["7"])
    assert instance.selected_server_ids == [7]
    assert instance.selected_server_names == ["gpu-7"]


def test_case_insensitive_unique_match(shell):
    instance, _, _ = shell
    instance._cmd_server(["Cluster-Dev"])
    assert instance.selected_server_ids == [102]


def test_unknown_name_errors_and_keeps_selection(shell):
    instance, _, _ = shell
    instance._cmd_server(["cluster-dev"])
    with pytest.raises(PortalError, match="nope"):
        instance._cmd_server(["nope"])
    assert instance.selected_server_ids == [102]


def test_chat_selection_sends_resolved_id(shell):
    instance, client, _ = shell
    instance.chat_id = 55
    instance._cmd_server(["cluster-dev"])
    assert client.single_scope_calls == [(55, 102)]


def test_prompt_and_status_show_names(shell):
    instance, _, out = shell
    instance._cmd_server(["cluster-dev", "gpu-7"])
    prompt_text = "".join(fragment for _, fragment in instance._prompt_fragments())
    assert "servers#cluster-dev,gpu-7" in prompt_text
    assert "102" not in prompt_text
    instance._cmd_status([])
    status_text = out.getvalue()
    assert "cluster-dev, gpu-7" in status_text


def test_auto_resets_names(shell):
    instance, _, _ = shell
    instance._cmd_server(["cluster-dev"])
    instance._cmd_server(["auto"])
    assert instance.selected_server_ids == []
    assert instance.selected_server_names == []
