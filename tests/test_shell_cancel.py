"""Tests for cancelling an in-flight agent turn from the interactive shell."""

from io import StringIO

from prompt_toolkit.document import Document
from rich.console import Console

from skyportal.portal import ChatTurnResult, PortalError
from skyportal.shell import COMMANDS, InteractiveShell, SkyportalCompleter


class FakeClient:
    """Records begin/wait/cancel calls for the send flow."""

    def __init__(self, *, wait_result=None, wait_raises=None, cancel_raises=None):
        self.base_url = "https://app.skyportal.ai"
        self._wait_result = wait_result
        self._wait_raises = wait_raises
        self._cancel_raises = cancel_raises
        self.begin_calls = []
        self.wait_calls = []
        self.cancel_calls = []

    def is_authenticated(self):
        return True

    def begin_chat_turn(self, message, chat_id=None, server_id=None):
        self.begin_calls.append((message, chat_id, server_id))
        return chat_id if chat_id is not None else 100

    def wait_for_chat(self, chat_id, after_sequence=0):
        self.wait_calls.append((chat_id, after_sequence))
        if self._wait_raises is not None:
            raise self._wait_raises
        return self._wait_result

    def cancel_chat(self, chat_id, reason=None):
        self.cancel_calls.append((chat_id, reason))
        if self._cancel_raises is not None:
            raise self._cancel_raises
        return {"status": "cancelled"}


def _shell(client):
    console = Console(file=StringIO(), force_terminal=False, width=100)
    shell = InteractiveShell(
        console=console,
        client_factory=lambda: client,
        session=object(),
        token_prompt=lambda _prompt: "",
    )
    return shell, console


def test_ctrl_c_during_wait_cancels_turn_server_side():
    client = FakeClient(wait_raises=KeyboardInterrupt())
    shell, console = _shell(client)

    shell._send_prompt("do something slow")  # must not raise

    assert client.begin_calls == [("do something slow", None, None)]
    assert shell.chat_id == 100  # attached before waiting, so cancel has an id
    assert client.cancel_calls == [(100, "Cancelled from the CLI")]
    out = console.file.getvalue()
    assert "Stopped" in out


def test_cancel_targets_the_existing_chat_id():
    client = FakeClient(wait_raises=KeyboardInterrupt())
    shell, _ = _shell(client)
    shell.chat_id = 55  # already in a chat

    shell._send_prompt("follow up")

    assert client.begin_calls == [("follow up", 55, None)]
    assert client.cancel_calls == [(55, "Cancelled from the CLI")]


def test_cancel_when_turn_already_finished_is_benign():
    client = FakeClient(wait_raises=KeyboardInterrupt(), cancel_raises=PortalError("idle"))
    shell, console = _shell(client)

    shell._send_prompt("late cancel")  # must not raise

    assert client.cancel_calls == [(100, "Cancelled from the CLI")]
    assert "Nothing to stop" in console.file.getvalue()


def test_normal_completion_processes_the_turn():
    turn = ChatTurnResult(
        chat_id=100, status="idle",
        messages=[{"role": "assistant", "sequence": 1,
                   "content": [{"type": "text", "text": "all done"}]}],
        pending_approvals=[], latest_sequence=1,
    )
    client = FakeClient(wait_result=turn)
    shell, console = _shell(client)

    shell._send_prompt("hello")

    assert client.cancel_calls == []
    assert "all done" in console.file.getvalue()
    assert shell.chat_id == 100
    assert shell.last_sequence == 1


def test_prompt_uses_compact_reference_style():
    shell, _ = _shell(FakeClient())

    text = "".join(part[1] for part in shell._prompt_fragments())

    assert text == "skyportal [connected]  > "


def test_exit_still_completes_after_completion_message():
    client = FakeClient(wait_result=ChatTurnResult(1, "idle", [], [], 0))
    shell, _ = _shell(client)
    shell._cmd_exit([])
    assert shell.running is False


def test_completer_still_offers_exit():
    comps = [c.text for c in SkyportalCompleter().get_completions(Document("/ex"), None)]
    assert "/exit" in comps
    assert "/exit" in COMMANDS
