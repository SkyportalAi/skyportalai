"""Incremental legacy-shell rendering, approval recovery, and remote status."""

from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

from skyportal.portal import ChatTurnResult, PortalError
from skyportal.shell import InteractiveShell


def _assistant(text, sequence):
    return {
        "role": "assistant",
        "sequence": sequence,
        "content": [{"type": "text", "text": text}],
        "metadata": {},
    }


def _tool(command, output, sequence):
    return {
        "role": "tool",
        "sequence": sequence,
        "content": [{"type": "text", "text": output}],
        "metadata": {
            "terminal_command": command,
            "terminal_output": output,
            "terminal_server_hostname": "web-1",
            "terminal_success": True,
        },
    }


class _Session:
    def __init__(self, answers=()):
        self.answers = iter(answers)
        self.calls = 0

    def prompt(self, _message):
        self.calls += 1
        return next(self.answers)


def _shell(client, tmp_path, monkeypatch, session=None):
    monkeypatch.setenv("SKYPORTAL_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    console = Console(file=StringIO(), force_terminal=False, width=200)
    shell = InteractiveShell(
        console=console,
        client_factory=lambda: client,
        session=session or _Session(),
        token_prompt=lambda _prompt: "",
    )
    return shell, console


class StreamingClient:
    base_url = "https://app.skyportal.ai"

    def __init__(self):
        self.wait_calls = []

    def is_authenticated(self):
        return True

    def begin_chat_turn(self, message, chat_id=None, server_id=None):
        return chat_id or 42

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=300, on_progress=None):
        self.wait_calls.append((chat_id, after_sequence, timeout, on_progress))
        first = _assistant("streamed first", 1)
        second = _tool("uptime", "up 10 days", 2)
        assert on_progress is not None
        on_progress([first])
        on_progress([first, second])  # replayed sequence 1 must not render twice
        return ChatTurnResult(
            chat_id=chat_id,
            status="idle",
            messages=[second, _assistant("streamed final", 3)],
            pending_approvals=[],
            latest_sequence=3,
        )

    def cancel_chat(self, chat_id, reason=None):
        return {"status": "cancelled"}


def test_shell_waits_indefinitely_and_renders_each_persisted_message_once(
    tmp_path, monkeypatch
):
    client = StreamingClient()
    shell, console = _shell(client, tmp_path, monkeypatch)

    shell._send_prompt("inspect the host")

    output = console.file.getvalue()
    assert output.count("streamed first") == 1
    assert output.count("up 10 days") == 1
    assert output.count("streamed final") == 1
    assert output.count("Skyportal agent") == 1
    assert client.wait_calls[0][2] is None
    assert shell.last_sequence == 3


def test_failed_progress_render_is_retried_from_final_result(tmp_path, monkeypatch):
    shell, _console = _shell(StreamingClient(), tmp_path, monkeypatch)
    state = shell._new_render_state()
    message = _assistant("retry me", 9)

    with patch.object(
        shell,
        "_render_assistant_messages",
        side_effect=[RuntimeError("console failed"), True],
    ) as render:
        with pytest.raises(RuntimeError, match="console failed"):
            shell._render_incremental_messages([message], state)
        assert state["seen"] == set()
        assert shell.last_sequence == 0

        shell._render_incremental_messages([message], state)

    assert render.call_count == 2
    assert shell.last_sequence == 9


class ApprovalClient:
    base_url = "https://app.skyportal.ai"

    def __init__(self, *, stale=False):
        self.stale = stale
        self.submit_calls = []
        self.status_calls = []
        self.wait_calls = []

    def is_authenticated(self):
        return True

    def submit_chat_approval(self, chat_id, approval, decision):
        self.submit_calls.append((chat_id, approval, decision))
        if not self.stale:
            raise PortalError("Could not connect to Skyportal: timed out")
        return {"success": True}

    def chat_status(self, chat_id):
        self.status_calls.append(chat_id)
        return {"status": "processing", "pending_approvals": []}

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=300, on_progress=None):
        self.wait_calls.append((chat_id, after_sequence, timeout, on_progress))
        if self.stale:
            approval = {"approval_id": "approval-1", "command": "uptime"}
            return ChatTurnResult(
                chat_id, "awaiting_approval", [], [approval], after_sequence
            )
        message = _assistant("continued exactly once", 5)
        assert on_progress is not None
        on_progress([message])
        return ChatTurnResult(chat_id, "idle", [message], [], 5)


def _approval_turn():
    approval = {"approval_id": "approval-1", "command": "uptime"}
    return ChatTurnResult(42, "awaiting_approval", [], [approval], 4)


def test_approval_timeout_reconciles_without_resubmitting(tmp_path, monkeypatch):
    client = ApprovalClient()
    session = _Session(["y"])
    shell, console = _shell(client, tmp_path, monkeypatch, session=session)

    shell._process_turn(_approval_turn())

    assert len(client.submit_calls) == 1
    assert client.status_calls == [42]
    assert client.wait_calls[0][2] is None
    assert console.file.getvalue().count("continued exactly once") == 1
    assert "reattached" in console.file.getvalue()


def test_stale_approval_snapshot_is_not_prompted_or_submitted_twice(
    tmp_path, monkeypatch
):
    client = ApprovalClient(stale=True)
    session = _Session(["y", "y"])
    shell, _console = _shell(client, tmp_path, monkeypatch, session=session)

    with patch("skyportal.shell.time.monotonic", side_effect=[0.0, 301.0]):
        with pytest.raises(PortalError, match="will not be submitted twice"):
            shell._process_turn(_approval_turn())

    assert session.calls == 1
    assert len(client.submit_calls) == 1


class StatusClient:
    base_url = "https://app.skyportal.ai"

    def __init__(self, payload=None, error=None, authenticated=True):
        self.payload = payload or {}
        self.error = error
        self.authenticated = authenticated
        self.execution_calls = []
        self.status_calls = []

    def is_authenticated(self):
        return self.authenticated

    def get_execution_status(self, chat_id):
        self.execution_calls.append(chat_id)
        if self.error is not None:
            raise self.error
        return self.payload

    def chat_status(self, chat_id):
        self.status_calls.append(chat_id)
        return {"status": "awaiting_approval", "pending_approvals": []}


def test_status_shows_remote_workflow_command_and_approval_without_raw_output(
    tmp_path, monkeypatch
):
    client = StatusClient(
        {
            "status": "processing",
            "pending_approvals": [
                {"approval_id": "a1", "command": "[red]restart web[/red]"}
            ],
            "live_command_output": {
                "command": "printf '[bold]hello[/bold]'",
                "output": "RAW-SECRET-MUST-NOT-BE-DISPLAYED",
            },
            "live_plan": {"current_step_index": 1, "total_steps": 4},
        }
    )
    shell, console = _shell(client, tmp_path, monkeypatch)
    shell.chat_id = 42

    shell._cmd_status([])

    output = console.file.getvalue()
    assert "processing" in output
    assert "restart web" in output
    assert "printf '[bold]hello[/bold]'" in output
    assert "step 2/4" in output
    assert "RAW-SECRET-MUST-NOT-BE-DISPLAYED" not in output
    assert client.execution_calls == [42]


def test_status_falls_back_to_lightweight_endpoint_on_older_server(
    tmp_path, monkeypatch
):
    client = StatusClient(error=PortalError("missing", status_code=404))
    shell, console = _shell(client, tmp_path, monkeypatch)
    shell.chat_id = 17

    shell._cmd_status([])

    assert "awaiting_approval" in console.file.getvalue()
    assert client.execution_calls == [17]
    assert client.status_calls == [17]


def test_status_without_authenticated_chat_does_not_make_remote_request(
    tmp_path, monkeypatch
):
    client = StatusClient(authenticated=False)
    shell, _console = _shell(client, tmp_path, monkeypatch)

    shell._cmd_status([])

    assert client.execution_calls == []
    assert client.status_calls == []
