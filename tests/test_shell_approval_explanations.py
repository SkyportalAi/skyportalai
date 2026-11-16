"""Why a command asked, ran, or was blocked, as the server words it (#33, skyportal-website#3632)."""

from io import StringIO

from rich.console import Console

from skyportalai.shell.interactive import InteractiveShell
from skyportalai.shell.portal import ChatTurnResult, PortalError, SkyportalClient
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


def _shell(client, tmp_path, monkeypatch, answers=(), confirm_answers=()):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    console = Console(file=StringIO(), force_terminal=False, width=160)
    session = _Session(answers)
    confirm = _Session(confirm_answers)
    shell = InteractiveShell(
        console=console, client_factory=lambda: client, session=session, token_prompt=lambda _p: "",
        confirm_prompt=confirm.prompt,
    )
    shell.confirm = confirm
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
    shell, _console, session = _shell(
        client, tmp_path, monkeypatch, answers=["r"], confirm_answers=["use a dry run first"],
    )

    shell._process_turn(_turn("awaiting_approval", [_approval(approval_context=CONTEXT)]))

    assert client.submit_calls == [("a1", "rejected", "use a dry run first")]
    # Asked outside the saved-history session, so the reason never lands in ~/.skyportalai/history.
    assert shell.confirm.prompts == ["Reason: "]
    assert len(session.prompts) == 1


def test_the_host_shows_even_without_reasons(tmp_path, monkeypatch):
    client = _Client()
    shell, console, _session = _shell(client, tmp_path, monkeypatch, answers=["y"])

    shell._process_turn(_turn("awaiting_approval", [_approval(approval_context={"host": "prod-db-1", "why": []})]))

    output = console.file.getvalue()
    assert "on prod-db-1" in output
    assert "•" not in output


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


def test_a_blocked_command_without_an_owner_does_not_point_to_settings():
    line = InteractiveShell._tool_result_line(_result({
        "verdict": "blocked", "rule": "access", "title": "Blocked by policy",
    }, success=False))
    assert line.plain.splitlines()[-1].strip() == "✗ blocked: Blocked by policy"


def test_a_blocked_command_points_to_settings_only_with_a_settings_url():
    line = InteractiveShell._tool_result_line(_result({
        "verdict": "blocked", "rule": "destructive", "title": "Blocked: changes data",
        "can_change": "you", "settings_url": "/agent/settings/",
    }, success=False))
    assert line.plain.splitlines()[-1].strip() == (
        "✗ blocked: Blocked: changes data (you can change this in Settings)"
    )


def test_a_read_only_host_block_does_not_point_to_settings():
    # The read-only policy lives in the chat scope menu, so the server sends no settings_url.
    line = InteractiveShell._tool_result_line(_result({
        "verdict": "blocked", "rule": "read_only_environment", "title": "Blocked: read-only host",
        "can_change": "you", "settings_url": "",
    }, success=False))
    assert line.plain.splitlines()[-1].strip() == "✗ blocked: Blocked: read-only host (you can change this)"


def test_a_positional_raw_stays_raw():
    approval = PendingApproval("a1", "bash_command", "ls", "", "", {"k": "v"})
    assert (approval.raw, approval.rule) == ({"k": "v"}, "")


def test_a_result_without_an_explanation_is_unchanged():
    line = InteractiveShell._tool_result_line(_result(None))
    assert line.plain.splitlines()[-1].strip() == "NAME READY"


def test_the_sdk_exposes_the_reasons():
    approval = PendingApproval.from_dict(_approval(approval_context=CONTEXT))
    assert (approval.rule, approval.can_change, approval.host) == ("destructive", "you", "prod-db-1")
    assert approval.why == ("Deletes or changes data (`rm`)", "Not on any allow list")
    assert PendingApproval.from_dict(_approval()).why == ()


OFFERS_ALWAYS = {
    **CONTEXT, "rule": "permit_list", "title": "Needs your approval", "why": ["Not on any allow list"],
    "always_allow": {"prefixes": ["npm test"], "label": "Always allow `npm test` on altaf-laptop"},
}


def test_a_answers_always_where_the_card_offers_it(tmp_path, monkeypatch):
    client = _Client()
    shell, console, session = _shell(client, tmp_path, monkeypatch, answers=["a"])

    shell._process_turn(_turn("awaiting_approval", [_approval(command="npm test", approval_context=OFFERS_ALWAYS)]))

    assert "a=always" in session.prompts[0]
    assert "Always allow npm test on altaf-laptop" in console.file.getvalue()
    assert client.submit_calls == [("a1", "always_approved", None)]


def test_a_is_no_answer_where_the_card_offers_no_always(tmp_path, monkeypatch):
    # Sensitive or destructive commands, and fan-outs, get no ✓✓ on the web card either.
    client = _Client()
    shell, _console, session = _shell(client, tmp_path, monkeypatch, answers=["a"])

    shell._process_turn(_turn("awaiting_approval", [_approval(approval_context=CONTEXT)]))

    assert "a=always" not in session.prompts[0]
    assert client.submit_calls == [("a1", "rejected", None)]


def test_an_always_the_server_refuses_still_approves_once(tmp_path, monkeypatch):
    class _OlderServer(_Client):
        def submit_chat_approval(self, chat_id, approval, decision, *, autoapproved=False, rejection_reason=None):
            if decision == "always_approved":
                self.submit_calls.append((approval["approval_id"], decision, None))
                raise PortalError('decision must be "approved" or "rejected"', status_code=400)
            return super().submit_chat_approval(chat_id, approval, decision)

    client = _OlderServer()
    shell, console, _session = _shell(client, tmp_path, monkeypatch, answers=["a"])

    shell._process_turn(_turn("awaiting_approval", [_approval(command="npm test", approval_context=OFFERS_ALWAYS)]))

    assert client.submit_calls == [("a1", "always_approved", None), ("a1", "approved", None)]
    assert "approving this once" in console.file.getvalue()
