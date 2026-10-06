"""Why a command asked, ran, or was blocked, as the server words it (#33, skyportal-website#3632)."""

from io import StringIO

from rich.console import Console

from skyportalai.shell.interactive import InteractiveShell
from skyportalai.shell.portal import ChatTurnResult, SkyportalClient
from skyportalai.types import PendingApproval

CONTEXT = {
    "verdict": "ask",
    "rule": "destructive",
    "title": "Changes or deletes data",
    "why": ["Deletes or changes data (`rm`)", "Not on any allow list"],
    "can_change": "you",
    "settings_url": "/agent/settings/",
    "host": "prod-db-1",
    "always_allow": None,
}


class _Session:
    def __init__(self, answers=()):
        self._answers = iter(answers)
        self.prompts = []

    def prompt(self, message):
        self.prompts.append(message)
        return next(self._answers)


class _Client:
    base_url = "https://app.skyportal.ai"

    def __init__(self):
        self.submit_calls = []

    def is_authenticated(self):
        return True

    def get_permission_mode(self):
        return "ask"

    def submit_chat_approval(self, chat_id, approval, decision, *, autoapproved=False, rejection_reason=None):
        self.submit_calls.append((approval["approval_id"], decision, rejection_reason))
        return {"success": True}

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=300, on_progress=None, on_status=None):
        return _turn("idle")


def _turn(status, approvals=()):
    return ChatTurnResult(
        chat_id=42, status=status, messages=[], pending_approvals=list(approvals), latest_sequence=0,
    )


def _shell(client, tmp_path, monkeypatch, answers=()):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    console = Console(file=StringIO(), force_terminal=False, width=160)
    session = _Session(answers)
    shell = InteractiveShell(
        console=console, client_factory=lambda: client, session=session, token_prompt=lambda _p: "",
    )
    return shell, console, session


def _approval(**extra):
    return {"approval_id": "a1", "type": "bash_command", "command": "rm -rf /tmp/x", **extra}


def test_the_prompt_names_the_host_and_every_reason(tmp_path, monkeypatch):
    client = _Client()
    shell, console, _session = _shell(client, tmp_path, monkeypatch, answers=["y"])

    shell._process_turn(_turn("awaiting_approval", [_approval(approval_context=CONTEXT)]))

    output = console.file.getvalue()
    assert "on prod-db-1" in output
    assert "Changes or deletes data" in output
    assert "• Deletes or changes data (rm)" in output
    assert "• Not on any allow list" in output
    assert client.submit_calls == [("a1", "approved", None)]


def test_an_older_server_gets_todays_prompt(tmp_path, monkeypatch):
    client = _Client()
    shell, console, _session = _shell(client, tmp_path, monkeypatch, answers=["n"])

    shell._process_turn(_turn("awaiting_approval", [_approval()]))

    assert "•" not in console.file.getvalue()
    assert client.submit_calls == [("a1", "rejected", None)]


def test_r_rejects_with_the_reason(tmp_path, monkeypatch):
    client = _Client()
    shell, _console, session = _shell(client, tmp_path, monkeypatch, answers=["r", "use a dry run first"])

    shell._process_turn(_turn("awaiting_approval", [_approval(approval_context=CONTEXT)]))

    assert client.submit_calls == [("a1", "rejected", "use a dry run first")]
    assert session.prompts[1] == "Reason: "


def test_the_reason_reaches_the_approve_endpoint():
    client = SkyportalClient.__new__(SkyportalClient)
    sent = {}
    client._request = lambda method, path, json_body=None: sent.update(json_body) or {}

    client.submit_chat_approval(42, _approval(), "rejected", rejection_reason="too risky")
    assert sent["rejection_reason"] == "too risky"

    sent.clear()
    client.submit_chat_approval(42, _approval(), "approved", rejection_reason="ignored")
    assert "rejection_reason" not in sent


def _result(explanation, success=True):
    return {
        "role": "tool",
        "metadata": {
            "terminal_command": "kubectl get pods",
            "terminal_server_hostname": "prod-db-1",
            "terminal_success": success,
            "terminal_output": "NAME READY",
            "approval_explanation": explanation,
        },
    }


def test_a_command_that_ran_without_asking_says_why():
    line = InteractiveShell._tool_result_line(_result({
        "verdict": "ran", "rule": "permit_list",
        "title": "Auto-approved · host allow list (`kubectl get`)",
    }))
    assert line.plain.splitlines()[-1].strip() == "Auto-approved · host allow list (kubectl get)"


def test_a_blocked_command_says_blocked_and_who_can_change_it():
    line = InteractiveShell._tool_result_line(_result({
        "verdict": "blocked", "rule": "access", "title": "Blocked by your team role",
        "can_change": "team_admin",
    }, success=False))
    assert line.plain.splitlines()[-1].strip() == "✗ blocked: Blocked by your team role (ask a team admin)"


def test_a_result_without_an_explanation_is_unchanged():
    line = InteractiveShell._tool_result_line(_result(None))
    assert line.plain.splitlines()[-1].strip() == "NAME READY"


def test_the_sdk_exposes_the_reasons():
    approval = PendingApproval.from_dict(_approval(approval_context=CONTEXT))
    assert (approval.rule, approval.can_change, approval.host) == ("destructive", "you", "prod-db-1")
    assert approval.why == ("Deletes or changes data (`rm`)", "Not on any allow list")
    assert PendingApproval.from_dict(_approval()).why == ()
