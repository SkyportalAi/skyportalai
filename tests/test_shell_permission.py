"""Shared approval-policy behavior in the interactive legacy CLI."""

from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.document import Document
from rich.console import Console

from skyportal.portal import ChatTurnResult, PortalError
from skyportal.shell import COMMANDS, InteractiveShell, SkyportalCompleter


class _Session:
    def __init__(self, answers=()):
        self._answers = iter(answers)
        self.calls = 0

    def prompt(self, _message):
        self.calls += 1
        return next(self._answers)


class PermissionClient:
    base_url = "https://app.skyportal.ai"

    def __init__(
        self,
        mode="ask",
        wait_results=(),
        permission_error=None,
        autoapproval_conflict=False,
    ):
        self.mode = mode
        self.wait_results = iter(wait_results)
        self.permission_error = permission_error
        self.autoapproval_conflict = autoapproval_conflict
        self.get_calls = 0
        self.set_calls = []
        self.submit_calls = []
        self.wait_calls = []

    def is_authenticated(self):
        return True

    def get_permission_mode(self):
        self.get_calls += 1
        if self.permission_error is not None:
            raise self.permission_error
        return self.mode

    def set_permission_mode(self, mode):
        self.set_calls.append(mode)
        self.mode = mode
        return mode

    def submit_chat_approval(
        self, chat_id, approval, decision, *, autoapproved=False
    ):
        self.submit_calls.append(
            (chat_id, approval["approval_id"], decision, autoapproved)
        )
        if autoapproved and self.autoapproval_conflict:
            raise PortalError(
                "Autoapproval is no longer enabled",
                status_code=409,
                code="autoapproval_policy_conflict",
            )
        return {"success": True}

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=300, on_progress=None):
        self.wait_calls.append((chat_id, after_sequence, timeout, on_progress))
        return next(self.wait_results)


def _shell(client, tmp_path, monkeypatch, session=None):
    monkeypatch.setenv("SKYPORTAL_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    console = Console(file=StringIO(), force_terminal=False, width=160)
    shell = InteractiveShell(
        console=console,
        client_factory=lambda: client,
        session=session or _Session(),
        token_prompt=lambda _prompt: "",
    )
    return shell, console


def _turn(status, approvals=()):
    return ChatTurnResult(
        chat_id=42,
        status=status,
        messages=[],
        pending_approvals=list(approvals),
        latest_sequence=0,
    )


def test_permission_command_gets_and_sets_shared_mode(tmp_path, monkeypatch):
    client = PermissionClient(mode="ask")
    shell, console = _shell(client, tmp_path, monkeypatch)

    shell._cmd_permission([])
    shell._cmd_permission(["autoapprove"])
    shell._cmd_status([])

    output = console.file.getvalue()
    assert "Permission mode: ask" in output
    assert "Permission mode: autoapprove" in output
    assert "autoapprove" in output
    assert client.set_calls == ["autoapprove"]
    assert client.get_calls == 2  # bare /permission and /status both refresh


def test_permission_help_handler_and_completions_are_registered(tmp_path, monkeypatch):
    shell, _console = _shell(PermissionClient(), tmp_path, monkeypatch)

    assert "/permission" in COMMANDS
    assert "/permission" in shell._handlers
    completions = [
        item.text
        for item in SkyportalCompleter().get_completions(Document("/permission a"), None)
    ]
    assert completions == ["autoapprove", "ask"]


def test_permission_rejects_unknown_mode_without_writing(tmp_path, monkeypatch):
    client = PermissionClient()
    shell, console = _shell(client, tmp_path, monkeypatch)

    shell._cmd_permission(["everything"])

    assert "Usage:" in console.file.getvalue()
    assert client.set_calls == []


def test_autoapprove_waits_out_stale_snapshot_then_approves_next_id_once(
    tmp_path, monkeypatch
):
    first = {"approval_id": "a1", "type": "bash_command", "command": "uptime"}
    second = {"approval_id": "a2", "type": "plan", "reason": "apply plan"}
    client = PermissionClient(
        mode="autoapprove",
        wait_results=(
            _turn("awaiting_approval", (first, second)),  # stale: a1 still present
            _turn("awaiting_approval", (second,)),
            _turn("idle"),
        ),
    )
    session = _Session()
    shell, console = _shell(client, tmp_path, monkeypatch, session=session)

    with patch("skyportal.shell.time.sleep") as sleep:
        shell._process_turn(_turn("awaiting_approval", (first, second)))

    assert session.calls == 0
    assert client.submit_calls == [
        (42, "a1", "approved", True),
        (42, "a2", "approved", True),
    ]
    assert len(client.wait_calls) == 3
    assert sleep.call_count == 1
    assert console.file.getvalue().count("Auto-approving") == 2


def test_autoapprove_mode_failure_falls_back_to_prompt(tmp_path, monkeypatch):
    approval = {"approval_id": "a1", "type": "bash_command", "command": "uptime"}
    client = PermissionClient(
        permission_error=PortalError("permission endpoint unavailable"),
        wait_results=(_turn("idle"),),
    )
    session = _Session(["y"])
    shell, console = _shell(client, tmp_path, monkeypatch, session=session)

    shell._process_turn(_turn("awaiting_approval", (approval,)))

    assert session.calls == 1
    assert client.submit_calls == [(42, "a1", "approved", False)]
    assert "asking for safety" in console.file.getvalue()


def test_autoapprove_policy_race_reprompts_and_submits_unmarked_manual_decision(
    tmp_path, monkeypatch
):
    approval = {"approval_id": "a1", "type": "bash_command", "command": "uptime"}
    client = PermissionClient(
        mode="autoapprove",
        autoapproval_conflict=True,
        wait_results=(_turn("idle"),),
    )
    session = _Session(["y"])
    shell, console = _shell(client, tmp_path, monkeypatch, session=session)

    shell._process_turn(_turn("awaiting_approval", (approval,)))

    assert session.calls == 1
    assert client.submit_calls == [
        (42, "a1", "approved", True),
        (42, "a1", "approved", False),
    ]
    assert "explicit decision is required" in console.file.getvalue()


def test_autoapprove_does_not_silently_coerce_unknown_approval_type(
    tmp_path, monkeypatch
):
    approval = {
        "approval_id": "future-1",
        "type": "future_privileged_action",
        "reason": "new action type",
    }
    client = PermissionClient(mode="autoapprove", wait_results=(_turn("idle"),))
    session = _Session(["n"])
    shell, console = _shell(client, tmp_path, monkeypatch, session=session)

    shell._process_turn(_turn("awaiting_approval", (approval,)))

    assert client.get_calls == 0
    assert session.calls == 1
    assert client.submit_calls == [(42, "future-1", "rejected", False)]
    assert "requires an explicit decision" in console.file.getvalue()


def test_malformed_first_approval_is_not_skipped_or_coerced(
    tmp_path, monkeypatch
):
    malformed = {"approval_id": None, "type": "bash_command"}
    later = {"approval_id": "a2", "type": "bash_command", "command": "uptime"}
    client = PermissionClient(mode="autoapprove")
    session = _Session(["y"])
    shell, _console = _shell(client, tmp_path, monkeypatch, session=session)

    with pytest.raises(PortalError, match="without an approval ID"):
        shell._process_turn(_turn("awaiting_approval", (malformed, later)))

    assert client.get_calls == 0
    assert client.submit_calls == []
    assert session.calls == 0
