"""Tests for the interactive shell's /resume command."""

from io import StringIO

import pytest
from rich.console import Console

from skyportal.portal import PortalError
from skyportal.shell import COMMANDS, InteractiveShell


@pytest.fixture(autouse=True)
def _isolate_last_chat(tmp_path, monkeypatch):
    """Keep the persisted 'previous chat' file out of the real home dir."""
    monkeypatch.setenv("SKYPORTAL_LAST_CHAT_PATH", str(tmp_path / "last_chat"))


class FakeClient:
    """Records calls and returns canned chat data."""

    def __init__(self, *, authenticated=True, status_error=False, messages=None,
                 has_more=False):
        self.base_url = "https://app.skyportal.ai"
        self._authenticated = authenticated
        self._status_error = status_error
        self._messages = messages if messages is not None else []
        self._has_more = has_more
        self.status_calls = []
        self.message_calls = []

    def is_authenticated(self):
        return self._authenticated

    def chat_status(self, chat_id):
        self.status_calls.append(chat_id)
        if self._status_error:
            raise PortalError("not found", status_code=404)
        return {"status": "idle"}

    def chat_messages(self, chat_id, after_sequence=0):
        self.message_calls.append((chat_id, after_sequence))
        return {"messages": self._messages, "has_more": self._has_more}


def _shell(client):
    console = Console(file=StringIO(), force_terminal=False, width=100)
    shell = InteractiveShell(
        console=console,
        client_factory=lambda: client,
        session=object(),  # non-None so no real PromptSession is built
        token_prompt=lambda _prompt: "",
    )
    return shell, console


def test_resume_by_id_sets_chat_cursor_without_history_by_default():
    """/resume reloads context (chat_id/last_sequence) so the next message
    continues the right flow — it doesn't need to replay the transcript to
    do that, so the default is quiet. --verbose opts back into the render."""
    msgs = [
        {"role": "user", "sequence": 2, "content": [{"type": "text", "text": "list files"}]},
        {"role": "assistant", "sequence": 3,
         "content": [{"type": "text", "text": "here they are"}]},
    ]
    client = FakeClient(messages=msgs)
    shell, console = _shell(client)

    shell._cmd_resume(["42"])

    assert shell.chat_id == 42
    assert shell.last_sequence == 3
    assert client.status_calls == [42]
    assert client.message_calls == [(42, 0)]
    out = console.file.getvalue()
    assert "Resumed chat #42" in out
    assert "list files" not in out and "here they are" not in out


def test_resume_verbose_renders_history():
    msgs = [
        {"role": "user", "sequence": 2, "content": [{"type": "text", "text": "list files"}]},
        {"role": "assistant", "sequence": 3,
         "content": [{"type": "text", "text": "here they are"}]},
    ]
    client = FakeClient(messages=msgs)
    shell, console = _shell(client)

    shell._cmd_resume(["42", "--verbose"])

    assert shell.chat_id == 42
    assert shell.last_sequence == 3
    out = console.file.getvalue()
    assert "Resumed chat #42" in out
    assert "list files" in out and "here they are" in out


def test_resume_verbose_before_chat_id_also_works():
    msgs = [{"role": "assistant", "sequence": 1, "content": [{"type": "text", "text": "hi"}]}]
    client = FakeClient(messages=msgs)
    shell, console = _shell(client)

    shell._cmd_resume(["--verbose", "42"])

    assert shell.chat_id == 42
    assert "hi" in console.file.getvalue()


def test_bare_resume_uses_previous_chat():
    client = FakeClient(messages=[])
    shell, console = _shell(client)
    shell.previous_chat_id = 17  # as if a prior turn had set it

    shell._cmd_resume([])

    assert shell.chat_id == 17
    assert client.status_calls == [17]
    assert "Resumed chat #17" in console.file.getvalue()


def test_bare_resume_without_previous_chat_reports():
    client = FakeClient()
    shell, console = _shell(client)
    assert shell.previous_chat_id is None

    shell._cmd_resume([])

    assert shell.chat_id is None
    assert client.status_calls == []
    assert "No previous chat" in console.file.getvalue()


def test_previous_chat_id_persists_across_restart():
    client = FakeClient(messages=[])
    shell, _ = _shell(client)
    shell._cmd_resume(["55"])  # attaching remembers it

    # A brand new shell (same env-isolated path) picks up the previous chat.
    fresh, console = _shell(FakeClient(messages=[]))
    assert fresh.previous_chat_id == 55
    fresh._cmd_resume([])
    assert fresh.chat_id == 55
    assert "Resumed chat #55" in console.file.getvalue()


def test_resume_unknown_chat_reports_and_leaves_state():
    client = FakeClient(status_error=True)
    shell, console = _shell(client)

    shell._cmd_resume(["999"])

    assert shell.chat_id is None
    assert "was not found" in console.file.getvalue()


def test_resume_requires_numeric_id():
    client = FakeClient()
    shell, console = _shell(client)

    shell._cmd_resume(["not-a-number"])

    assert shell.chat_id is None
    assert client.status_calls == []
    assert "must be a number" in console.file.getvalue()


def test_resume_rejects_extra_arguments():
    client = FakeClient()
    shell, console = _shell(client)

    shell._cmd_resume(["1", "2"])

    assert "Usage:" in console.file.getvalue()
    assert client.status_calls == []


def test_resume_empty_chat_still_attaches():
    client = FakeClient(messages=[])
    shell, console = _shell(client)

    shell._cmd_resume(["7", "--verbose"])

    assert shell.chat_id == 7
    assert shell.last_sequence == 0
    out = console.file.getvalue()
    assert "no earlier messages" in out
    assert "Resumed chat #7" in out


def test_resume_propagates_unexpected_portal_error():
    class BoomClient(FakeClient):
        def chat_status(self, chat_id):
            self.status_calls.append(chat_id)
            raise PortalError("server exploded", status_code=500)

    shell, _ = _shell(BoomClient())

    with pytest.raises(PortalError):
        shell._cmd_resume(["5"])

    # A 500 is not masked as "not found" and leaves state untouched.
    assert shell.chat_id is None


def test_resume_history_skips_tool_and_system_messages():
    msgs = [
        {"role": "user", "sequence": 1, "content": [{"type": "text", "text": "run it"}]},
        {"role": "assistant", "sequence": 2, "content": [{"type": "text", "text": "done"}]},
        {"role": "system", "sequence": 3, "content": [{"type": "text", "text": "system note"}]},
        {"role": "tool", "sequence": 4, "content": [{"type": "text", "text": "tool output"}]},
    ]
    client = FakeClient(messages=msgs)
    shell, console = _shell(client)

    shell._cmd_resume(["9", "--verbose"])

    out = console.file.getvalue()
    assert "run it" in out and "done" in out
    assert "system note" not in out and "tool output" not in out
    # The cursor still tracks every role even though history only shows the conversation.
    assert shell.last_sequence == 4


def test_resume_truncated_history_notes_hidden_messages():
    msgs = [{"role": "assistant", "sequence": 1,
             "content": [{"type": "text", "text": "hi"}]}]
    client = FakeClient(messages=msgs, has_more=True)
    shell, console = _shell(client)

    shell._cmd_resume(["3"])

    assert "Older messages were hidden" in console.file.getvalue()


def test_logout_forgets_previous_chat():
    client = FakeClient(messages=[])

    class LogoutClient(FakeClient):
        def logout(self):
            pass

    client = LogoutClient(messages=[])
    shell, _ = _shell(client)
    shell._cmd_resume(["8"])
    assert shell.previous_chat_id == 8

    shell._cmd_logout([])
    assert shell.previous_chat_id is None
    # a fresh shell no longer sees a previous chat
    fresh, _ = _shell(LogoutClient(messages=[]))
    assert fresh.previous_chat_id is None


def test_resume_registered_as_command_and_handler():
    assert "/resume" in COMMANDS
    assert "[chat_id]" in COMMANDS["/resume"].usage
    shell, _ = _shell(FakeClient())
    assert "/resume" in shell._handlers
