""""Always allow" on bash and scope cards in the CLI and SDK (skyportal-website#3665, #3750).

Offered only where the server sent approval_context.always_allow, worded by the
server, and sent as ``"decision": "always_approved"`` to the approve endpoint.
Never automatic.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from rich.console import Console
from typer.testing import CliRunner

from skyportalai._client import Skyportal
from skyportalai._exceptions import APIError, APIStatusError
from skyportalai.chat import Chat
from skyportalai.cli.main import CLIContext, app
from skyportalai.shell.interactive import InteractiveShell
from skyportalai.shell.portal import ChatTurnResult, PortalError
from skyportalai.types import ApprovalResult, PendingApproval

BASE = "https://api.test"
OFFER = {"prefixes": ["kubectl logs"], "label": "Always allow `kubectl logs` on kind-smoke"}
SCOPE = {
    "kind": "namespace", "server_id": 12, "hostname": "kind-smoke", "namespace": "e2e", "namespace_choices": [],
    "combined": True, "label": "Add namespace e2e on kind-smoke to this chat and run `kubectl logs -f slow-logger -n e2e`",
}
# What skyportal-website answers a ✓✓ with; a bash card's answer also names the host.
ALWAYS_ANSWER = {"success": True, "decision": "always_approved", "added_prefixes": ["kubectl logs"]}
ALWAYS_PROMPT = "Approve this action? [y/N/a=always/r=reject with reason]: "


def _context(always_allow):
    return {"title": "Needs your approval", "why": ["Not on any allow list"], "host": "kind-smoke",
            "always_allow": always_allow}


def _bash(always_allow=OFFER):
    return {"approval_id": "b1", "type": "bash_command", "command": "kubectl logs -f slow-logger -n e2e",
            "approval_context": _context(always_allow)}


def _scope(always_allow=OFFER, **scope):
    return {"approval_id": "s1", "type": "scope", "command": "kubectl logs -f slow-logger -n e2e",
            "scope": {**SCOPE, **scope}, "approval_context": _context(always_allow)}


class _Session:
    def __init__(self, answers=()):
        self._answers = iter(answers)
        self.prompts = []

    def prompt(self, message):
        self.prompts.append(message)
        return next(self._answers)


class _Client:
    base_url = "https://app.skyportal.ai"

    def __init__(self, mode="ask", answer=None, times_out=False, refuses_always=False):
        self.mode = mode
        self.submit_calls = []
        self.answer = answer
        self.times_out = times_out
        self.refuses_always = refuses_always

    def is_authenticated(self):
        return True

    def get_permission_mode(self):
        return self.mode

    def servers(self):
        return [{"id": 12, "hostname": "kind-smoke", "target_kind": "kubernetes", "namespaces": ["e2e"]}]

    def submit_chat_approval(self, chat_id, approval, decision, *, autoapproved=False, rejection_reason=None,
                             namespace=None):
        self.submit_calls.append((approval["type"], decision, autoapproved, namespace))
        if decision == "always_approved" and self.refuses_always:
            # skyportal-website before #3665, or before #3750 for a scope card.
            raise PortalError('decision must be "approved" or "rejected"', status_code=400)
        if self.times_out:
            raise TimeoutError("the approve request timed out")
        return self.answer if self.answer is not None else {"success": True, "decision": decision}

    def chat_status(self, chat_id):
        # The approval went through: the chat moved on.
        return {"status": "processing"}

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=300, on_progress=None, on_status=None):
        return ChatTurnResult(chat_id=42, status="idle", messages=[], pending_approvals=[], latest_sequence=0)


def _shell(client, answers=(), picks=()):
    session = _Session(answers)
    shell = InteractiveShell(
        console=Console(file=StringIO(), force_terminal=False, width=200), client_factory=lambda: client,
        session=session, token_prompt=lambda _p: "", confirm_prompt=_Session(picks).prompt,
    )
    return shell, session


def _answer(shell, approval):
    shell._process_turn(ChatTurnResult(
        chat_id=42, status="awaiting_approval", messages=[], pending_approvals=[approval], latest_sequence=0,
    ))
    return shell.console.file.getvalue()


def _run(approval, answers=(), picks=(), **client_options):
    client = _Client(**client_options)
    shell, session = _shell(client, answers, picks)
    return client, _answer(shell, approval), session.prompts


@pytest.fixture(autouse=True)
def _last_chat_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))


def test_the_prompt_offers_always_allow_with_the_servers_label():
    for approval in (_bash(), _scope()):
        client, output, prompts = _run(approval, answers=["a"])
        assert "a = Always allow kubectl logs on kind-smoke" in output
        assert prompts == [ALWAYS_PROMPT]
        assert client.submit_calls == [(approval["type"], "always_approved", False, None)]


def test_y_approves_once():
    client, _, _ = _run(_scope(), answers=["y"])
    assert client.submit_calls == [("scope", "approved", False, None)]


def test_no_always_allow_where_the_card_does_not_offer_it():
    scope_only = {**_scope(), "scope": {**SCOPE, "combined": False}, "approval_context": None}
    plan = {"approval_id": "p1", "type": "plan", "approval_context": _context(OFFER)}
    for approval in (_bash(always_allow=None), _scope(always_allow=None), scope_only, plan):
        client, output, prompts = _run(approval, answers=["a"])
        assert "a=always" not in prompts[0]
        assert "Always allow" not in output
        # "a" is then just another answer that isn't yes.
        assert client.submit_calls == [(approval["type"], "rejected", False, None)]


def test_autoapprove_never_always_allows():
    client, _, prompts = _run(_bash(), mode="autoapprove")
    assert prompts == []
    assert client.submit_calls == [("bash_command", "approved", True, None)]


def test_the_shell_says_what_always_allow_added():
    # A bash answer names its host; a scope answer doesn't, and the card's does.
    for approval, answer in ((_bash(), {**ALWAYS_ANSWER, "host": "kind-smoke"}), (_scope(), ALWAYS_ANSWER)):
        _, output, _ = _run(approval, answers=["a"], answer=answer)
        assert "Now always allowed on kind-smoke: kubectl logs" in output
        assert "wasn't" not in output

    _, output, _ = _run(_bash(), answers=["a"], answer={**ALWAYS_ANSWER, "added_prefixes": []})
    assert "Nothing new was added to the allow list." in output


def test_always_allow_on_a_scope_card_adds_its_target_to_the_shell_scope():
    client = _Client(answer=ALWAYS_ANSWER)
    shell, _ = _shell(client, answers=["a"])
    _answer(shell, _scope())
    assert client.submit_calls == [("scope", "always_approved", False, None)]
    assert 12 in shell.selected_server_ids
    assert shell.selected_namespaces[12] == ["e2e"]


def test_an_always_allow_the_server_refuses_approves_once_and_says_so():
    for approval in (_bash(), _scope()):
        client, output, _ = _run(approval, answers=["a"], refuses_always=True)
        assert client.submit_calls == [
            (approval["type"], "always_approved", False, None), (approval["type"], "approved", False, None),
        ]
        assert "Always allow wasn't taken" in output
        assert "approving this once" in output
        assert "Now always allowed" not in output
        assert "wasn't applied" not in output


def test_an_answer_that_is_no_always_allow_is_reported():
    _, output, _ = _run(_bash(), answers=["a"], answer={"success": True, "decision": "approved"})
    assert "Always allow wasn't applied: the server approved the command once" in output
    assert "Now always allowed" not in output


def test_an_answer_that_timed_out_claims_nothing():
    _, output, _ = _run(_bash(), answers=["a"], times_out=True)
    assert "it isn't known whether Always allow was applied" in output
    assert "Now always allowed" not in output


def test_an_approve_once_says_nothing_about_always_allow():
    _, output, _ = _run(_bash(), answers=["y"], answer=ALWAYS_ANSWER)
    assert "Now always allowed" not in output
    assert "wasn't applied" not in output


def test_the_sdk_reads_the_offer_and_sends_always_approved(requests_mock):
    approval = PendingApproval.from_dict(_scope())
    assert approval.always_allow == OFFER
    assert PendingApproval.from_dict(_bash(always_allow=None)).always_allow == {}

    submit = requests_mock.post(f"{BASE}/api/v1/agent/chat/5/approve/s1/", json=ALWAYS_ANSWER)
    client = Skyportal(api_key="sk-test", base_url=BASE)
    result = Chat(client, 5).approve("s1", approval_type="scope", always=True)

    assert submit.last_request.json() == {"decision": "always_approved", "type": "scope"}
    assert result.decision == "always_approved"
    assert result.raw["added_prefixes"] == ["kubectl logs"]

    client.chat.approve(5, "s1", approval_type="scope")
    assert submit.last_request.json() == {"decision": "approved", "type": "scope"}


def test_the_sdk_raises_when_the_server_refuses_always_approved(requests_mock):
    requests_mock.post(
        f"{BASE}/api/v1/agent/chat/5/approve/b1/", status_code=400,
        json={"error": "Always allow is not offered for this command.", "code": "always_allow_not_offered"},
    )
    client = Skyportal(api_key="sk-test", base_url=BASE)
    with pytest.raises(APIStatusError) as raised:
        client.chat.approve(5, "b1", always=True)
    assert raised.value.status_code == 400


class _FakeChat:
    def __init__(self, answers_always=True, refuses_always=False):
        self.calls = []
        self.answers_always = answers_always
        self.refuses_always = refuses_always

    def approve(self, chat_id, approval_id, *, approval_type, command, namespace=None, always=False):
        self.calls.append((chat_id, approval_id, approval_type, always))
        if always and self.refuses_always:
            raise APIError("Always allow is not offered for this command.", status_code=400)
        if always and self.answers_always:
            return ApprovalResult(success=True, decision="always_approved",
                                  raw={"success": True, "decision": "always_approved", "added_prefixes": ["ls"]})
        return ApprovalResult(success=True, decision="approved", raw={"success": True, "decision": "approved"})


def _cli(monkeypatch, tmp_path, chat):
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "sk-test")
    monkeypatch.setattr(CLIContext, "client", lambda self: type("Fake", (), {"chat": chat})())
    return CliRunner()


def test_chat_approve_always(monkeypatch, tmp_path):
    chat = _FakeChat()
    runner = _cli(monkeypatch, tmp_path, chat)

    result = runner.invoke(app, ["chat", "approve", "42", "a1", "--always"])
    assert result.exit_code == 0, result.output
    assert chat.calls == [(42, "a1", "bash_command", True)]
    assert "Approval a1: always_approved; now always allowed: ls" in result.stdout

    result = runner.invoke(app, ["--json", "chat", "approve", "42", "a2", "--type", "scope"])
    assert chat.calls[-1] == (42, "a2", "scope", False)
    assert json.loads(result.stdout)["data"]["decision"] == "approved"


def test_chat_approve_always_says_when_the_answer_is_a_plain_approval(monkeypatch, tmp_path):
    runner = _cli(monkeypatch, tmp_path, _FakeChat(answers_always=False))
    result = runner.invoke(app, ["chat", "approve", "42", "a1", "--always"])
    assert result.exit_code == 0, result.output
    assert "Always allow wasn't applied, so this approved the command once" in result.stdout


def test_chat_approve_always_fails_when_the_server_refuses_it(monkeypatch, tmp_path):
    runner = _cli(monkeypatch, tmp_path, _FakeChat(refuses_always=True))
    result = runner.invoke(app, ["chat", "approve", "42", "a1", "--always"])
    assert result.exit_code == 1
    assert "Always allow is not offered for this command." in result.output
