"""A turn that paused for scope offers a numbered picker, not just prose.

The server-side message names both the web Scope pill and `/server <name>`
because nothing at that layer can tell the clients apart. The terminal spots
the pause from the metadata type and then builds the picker from its OWN live
/servers call — the same list /server resolves against — so the options are
never a stale snapshot of the chat's cached metadata.
"""

from io import StringIO

import pytest
from rich.console import Console

from skyportal.portal import PortalError
from skyportal.shell import InteractiveShell

SERVERS = [
    {"id": "66", "name": "build-box-01", "status": "connected", "host_type": "Dev",
     "target_kind": "ssh", "vcpu": 8, "ram": 16, "gpus": 0},
    {"id": "67", "name": "k8s-cp-01", "status": "connected", "host_type": "Dev",
     "target_kind": "kubernetes", "vcpu": 8, "ram": 16, "gpus": 0},
]


def _pause_message(available=None, sequence=3):
    metadata = {"type": "react_no_server", "failed_tool": "run_command", "iteration": 1}
    if available is not None:
        metadata["available_servers"] = available
    return {
        "role": "assistant",
        "sequence": sequence,
        "content": "Which server would you like to use for this chat?",
        "metadata": metadata,
    }


AVAILABLE = [
    {"id": 66, "hostname": "build-box-01", "target_kind": "ssh"},
    {"id": 67, "hostname": "k8s-cp-01", "target_kind": "kubernetes"},
]


class FakeClient:
    base_url = "https://app.skyportal.ai"

    def __init__(self):
        self.single_scope_calls = []
        self.scope_calls = []
        self.fail_with = None

    def is_authenticated(self):
        return True

    def servers(self):
        if self.fail_with is not None:
            raise self.fail_with
        return SERVERS

    def select_chat_server(self, chat_id, server_id):
        self.single_scope_calls.append((chat_id, server_id))

    def select_chat_servers(self, chat_id, server_ids, active_server_id=None):
        self.scope_calls.append((chat_id, server_ids, active_server_id))


class FakeSession:
    """Answers the picker prompt; records what it was asked."""

    def __init__(self, answer):
        self._answer = answer
        self.prompts = []

    def prompt(self, text, **_kwargs):
        self.prompts.append(text)
        return self._answer


@pytest.fixture
def make_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTAL_LAST_CHAT_PATH", str(tmp_path / "last_chat"))

    def _build(answer):
        out = StringIO()
        console = Console(file=out, width=160, force_terminal=False)
        client = FakeClient()
        session = FakeSession(answer)
        instance = InteractiveShell(
            console=console,
            client_factory=lambda: client,
            session=session,
            token_prompt=lambda _prompt: "",
        )
        instance.chat_id = 1251
        return instance, client, session, out

    return _build


def test_lists_the_offered_servers_and_selects_by_number(make_shell):
    instance, client, session, out = make_shell("2")
    instance._offer_server_choice([_pause_message(AVAILABLE)])
    text = out.getvalue()

    assert "1." in text and "build-box-01" in text
    assert "2." in text and "k8s-cp-01" in text
    assert "kubernetes" in text
    assert session.prompts and "Which server?" in session.prompts[0]
    # Picking 2 scopes the chat to k8s-cp-01 through the same path as /server.
    assert client.single_scope_calls == [(1251, 67)]


def test_a_typed_name_works_as_well_as_a_number(make_shell):
    instance, client, _, _ = make_shell("build-box-01")
    instance._offer_server_choice([_pause_message(AVAILABLE)])

    assert client.single_scope_calls == [(1251, 66)]


def test_enter_skips_without_selecting(make_shell):
    instance, client, _, out = make_shell("")
    instance._offer_server_choice([_pause_message(AVAILABLE)])

    assert client.single_scope_calls == []
    assert "/server <name>" in out.getvalue()


def test_silent_when_the_turn_did_not_pause_for_scope(make_shell):
    instance, client, session, out = make_shell("1")
    instance._offer_server_choice([
        {"role": "assistant", "sequence": 2, "content": "done", "metadata": {"type": "react_thought"}},
    ])

    assert session.prompts == []
    assert client.single_scope_calls == []
    assert out.getvalue() == ""


def test_offers_hosts_the_pause_message_never_listed(make_shell):
    # The whole reason the list is fetched rather than read off the message:
    # the message's copy comes from the chat's cached metadata, so a host added
    # after the chat started is missing from it. /servers has it, so we do too.
    instance, client, _, out = make_shell("2")
    instance._offer_server_choice([
        _pause_message([{"id": 66, "hostname": "build-box-01", "target_kind": "ssh"}]),
    ])

    assert "k8s-cp-01" in out.getvalue()
    assert client.single_scope_calls == [(1251, 67)]


def test_works_against_a_website_that_sends_no_server_list(make_shell):
    # available_servers is not required — the metadata type alone marks the
    # pause, so the picker works against a backend that never ships the field.
    instance, client, _, out = make_shell("1")
    instance._offer_server_choice([_pause_message(available=None)])

    assert "build-box-01" in out.getvalue()
    assert client.single_scope_calls == [(1251, 66)]


def test_a_failed_server_lookup_does_not_bury_the_message(make_shell):
    instance, client, session, out = make_shell("1")
    client.fail_with = PortalError("portal unreachable")
    instance._offer_server_choice([_pause_message(AVAILABLE)])

    assert session.prompts == []
    assert client.single_scope_calls == []
    assert "Couldn't load your servers" in out.getvalue()
