""""Always allow" on bash and scope cards in the CLI and SDK (skyportal-website#3750).

Offered only where the server sent approval_context.always_allow, worded by the
server, and sent as ``"always": true`` to the approve endpoint. Never automatic.
"""

from __future__ import annotations

import json
from io import StringIO

from rich.console import Console
from typer.testing import CliRunner

from skyportalai._client import Skyportal
from skyportalai.chat import Chat
from skyportalai.cli.main import CLIContext, app
from skyportalai.shell.interactive import InteractiveShell
from skyportalai.shell.portal import ChatTurnResult, SkyportalClient
from skyportalai.types import ApprovalResult, PendingApproval

BASE = "https://api.test"
OFFER = {"prefixes": ["kubectl logs"], "label": "Always allow `kubectl logs` on kind-smoke"}
SCOPE = {
    "kind": "namespace", "server_id": 12, "hostname": "kind-smoke", "namespace": "e2e", "namespace_choices": [],
    "combined": True, "label": "Add namespace e2e on kind-smoke to this chat and run `kubectl logs -f slow-logger -n e2e`",
}


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

    def __init__(self, mode="ask"):
        self.mode = mode
        self.submit_calls = []

    def is_authenticated(self):
        return True

    def get_permission_mode(self):
        return self.mode

    def servers(self):
        return [{"id": 12, "hostname": "kind-smoke", "target_kind": "kubernetes", "namespaces": ["e2e"]}]

    def submit_chat_approval(self, chat_id, approval, decision, *, autoapproved=False, rejection_reason=None,
                             namespace=None, always=False):
        self.submit_calls.append((approval["type"], decision, autoapproved, namespace, always))
        return {"success": True}

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=300, on_progress=None, on_status=None):
        return ChatTurnResult(chat_id=42, status="idle", messages=[], pending_approvals=[], latest_sequence=0)


def _run(approval, answers=(), picks=(), mode="ask"):
    client = _Client(mode)
    console = Console(file=StringIO(), force_terminal=False, width=200)
    session = _Session(answers)
    shell = InteractiveShell(
        console=console, client_factory=lambda: client, session=session, token_prompt=lambda _p: "",
        confirm_prompt=_Session(picks).prompt,
    )
    shell._process_turn(ChatTurnResult(
        chat_id=42, status="awaiting_approval", messages=[], pending_approvals=[approval], latest_sequence=0,
    ))
    return client, console.file.getvalue(), session.prompts


def test_the_prompt_offers_always_allow_with_the_servers_label(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    for approval in (_bash(), _scope()):
        client, output, prompts = _run(approval, answers=["a"])
        assert "a = Always allow kubectl logs on kind-smoke" in output
        assert prompts == ["Approve this action? [y/N/a=always allow/r=reject with reason]: "]
        assert client.submit_calls == [(approval["type"], "approved", False, None, True)]


def test_y_approves_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    client, _, _ = _run(_scope(), answers=["y"])
    assert client.submit_calls == [("scope", "approved", False, None, False)]


def test_no_always_allow_where_the_card_does_not_offer_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    scope_only = {**_scope(), "scope": {**SCOPE, "combined": False}, "approval_context": None}
    plan = {"approval_id": "p1", "type": "plan", "approval_context": _context(OFFER)}
    for approval in (_bash(always_allow=None), _scope(always_allow=None), scope_only, plan):
        client, output, prompts = _run(approval, answers=["a"])
        assert "always allow" not in prompts[0].lower()
        assert "Always allow" not in output
        # "a" is then just another answer that isn't yes.
        assert client.submit_calls == [(approval["type"], "rejected", False, None, False)]


def test_always_allow_rides_along_with_the_namespace_pick(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    client, _, _ = _run(_scope(namespace=None, namespace_choices=["e2e"]), answers=["a"], picks=["e2e"])
    assert client.submit_calls == [("scope", "approved", False, "e2e", True)]

    client, _, _ = _run(_scope(namespace=None, namespace_choices=["e2e"]), answers=["a"], picks=[""])
    assert client.submit_calls == [("scope", "rejected", False, None, False)]


def test_autoapprove_never_always_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    client, _, prompts = _run(_bash(), mode="autoapprove")
    assert prompts == []
    assert client.submit_calls == [("bash_command", "approved", True, None, False)]


def test_the_shell_client_sends_always_only_with_an_approval():
    client = SkyportalClient.__new__(SkyportalClient)
    sent = {}
    client._request = lambda method, path, json_body=None: sent.update(json_body) or {}

    client.submit_chat_approval(42, _scope(), "approved", always=True)
    assert sent["always"] is True

    sent.clear()
    client.submit_chat_approval(42, _scope(), "rejected", always=True)
    assert "always" not in sent


def test_the_sdk_reads_the_offer_and_sends_always(requests_mock):
    approval = PendingApproval.from_dict(_scope())
    assert approval.always_allow == OFFER
    assert PendingApproval.from_dict(_bash(always_allow=None)).always_allow == {}

    submit = requests_mock.post(
        f"{BASE}/api/v1/agent/chat/5/approve/s1/",
        json={"success": True, "decision": "approved", "always": True, "added_prefixes": ["kubectl logs"]},
    )
    client = Skyportal(api_key="sk-test", base_url=BASE)
    result = Chat(client, 5).approve("s1", approval_type="scope", always=True)

    assert submit.last_request.json() == {"decision": "approved", "type": "scope", "always": True}
    assert result.raw["added_prefixes"] == ["kubectl logs"]

    client.chat.approve(5, "s1", approval_type="scope")
    assert "always" not in submit.last_request.json()


class _FakeChat:
    def __init__(self):
        self.calls = []

    def approve(self, chat_id, approval_id, *, approval_type, command, namespace=None, always=False):
        self.calls.append((chat_id, approval_id, approval_type, always))
        raw = {"success": True, "decision": "approved", "always": always, "added_prefixes": ["ls"] if always else []}
        return ApprovalResult(success=True, decision="approved", raw=raw)


def test_chat_approve_always(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("SKYPORTALAI_API_KEY", "sk-test")
    chat = _FakeChat()
    fake = type("Fake", (), {"chat": chat})()
    monkeypatch.setattr(CLIContext, "client", lambda self: fake)
    runner = CliRunner()

    result = runner.invoke(app, ["chat", "approve", "42", "a1", "--always"])
    assert result.exit_code == 0, result.output
    assert chat.calls == [(42, "a1", "bash_command", True)]
    assert "now always allowed: ls" in result.stdout

    result = runner.invoke(app, ["--json", "chat", "approve", "42", "a2", "--type", "scope"])
    assert chat.calls[-1] == (42, "a2", "scope", False)
    assert json.loads(result.stdout)["data"]["decision"] == "approved"
