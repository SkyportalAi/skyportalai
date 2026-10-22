"""Scope approvals in the SDK (skyportal-website#3724): never autoapproved, answered by a callback."""

from skyportalai._client import Skyportal
from skyportalai.types import PendingApproval

BASE = "https://api.test"
STATUS = f"{BASE}/api/v1/agent/chat/5/status/"
SCOPE = {
    "kind": "namespace", "server_id": 12, "hostname": "kind-smoke", "namespace": "e2e",
    "namespace_choices": [], "combined": True,
    "label": "Add namespace e2e on kind-smoke to this chat and run `kubectl logs -f slow-logger -n e2e`",
}


def _client():
    client = Skyportal(api_key="sk-test", base_url=BASE)
    client._backoff_base = 0.0
    return client


def _awaiting(scope=SCOPE):
    return {"json": {"status": "awaiting_approval", "pending_approvals": [{
        "approval_id": "s1", "type": "scope", "command": "kubectl logs -f slow-logger -n e2e", "scope": scope,
    }]}}


def test_pending_approval_carries_what_the_scope_card_adds():
    approval = PendingApproval.from_dict(_awaiting()["json"]["pending_approvals"][0])
    assert approval.type == "scope"
    assert approval.scope["hostname"] == "kind-smoke"
    assert approval.scope["namespace"] == "e2e"
    assert PendingApproval.from_dict({"approval_id": "a1", "type": "bash_command"}).scope == {}


def test_autoapprove_never_answers_a_scope_card(requests_mock):
    requests_mock.get(STATUS, **_awaiting())
    requests_mock.get(f"{BASE}/api/v1/agent/permission/", json={"permission_mode": "autoapprove"})

    result = _client().chat.wait(5, timeout=30.0, poll_interval=0.0)

    assert result.status == "awaiting_approval"
    assert not [r for r in requests_mock.request_history if r.method == "POST"]


def test_a_callback_answers_a_scope_card(requests_mock):
    requests_mock.get(STATUS, [_awaiting(), {"json": {"status": "idle"}}])
    submit = requests_mock.post(
        f"{BASE}/api/v1/agent/chat/5/approve/s1/", json={"success": True, "decision": "approved"},
    )
    seen = []

    result = _client().chat.wait(5, timeout=30.0, poll_interval=0.0, on_approval=lambda a: seen.append(a) or True)

    assert result.status == "idle"
    assert seen[0].scope["label"].startswith("Add namespace e2e")
    body = submit.last_request.json()
    assert body["type"] == "scope"
    assert body["decision"] == "approved"
    assert "autoapproved" not in body


def test_a_card_that_asks_for_a_namespace_goes_back_to_the_caller(requests_mock):
    requests_mock.get(STATUS, **_awaiting({**SCOPE, "namespace": None, "namespace_choices": ["e2e", "kube-system"]}))
    called = []

    result = _client().chat.wait(5, timeout=30.0, poll_interval=0.0, on_approval=lambda a: called.append(a) or True)

    assert result.status == "awaiting_approval"
    assert called == []
    assert not [r for r in requests_mock.request_history if r.method == "POST"]


def test_the_caller_answers_with_the_namespace(requests_mock):
    submit = requests_mock.post(
        f"{BASE}/api/v1/agent/chat/5/approve/s1/", json={"success": True, "decision": "approved"},
    )

    _client().chat.approve(5, "s1", approval_type="scope", namespace="e2e")

    assert submit.last_request.json() == {"decision": "approved", "type": "scope", "namespace": "e2e"}
