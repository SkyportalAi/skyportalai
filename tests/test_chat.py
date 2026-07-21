import pytest

from skyportalai._client import Skyportal
from skyportalai._exceptions import (
    APIError,
    AuthenticationError,
    WaitTimeoutError,
)
from skyportalai.chat import Chat
from skyportalai.types import (
    ApprovalResult,
    ChatStatus,
    Message,
    MessagesPage,
    PendingApproval,
)

BASE = "https://api.test"


def _client():
    c = Skyportal(api_key="sk-test", base_url=BASE)
    c._backoff_base = 0.0
    return c


def test_create_chat_posts_message_and_returns_bound_handle(requests_mock):
    requests_mock.post(
        f"{BASE}/api/v1/agent/chat/",
        status_code=202,
        json={"chat_id": 123, "status": "processing",
              "poll_url": "/api/v1/agent/chat/123/status/"},
    )
    client = _client()
    chat = client.chat.create_chat("list files in /tmp", server_id=7)

    assert isinstance(chat, Chat)
    assert chat.chat_id == 123
    assert chat.raw["status"] == "processing"
    assert chat.raw["poll_url"] == "/api/v1/agent/chat/123/status/"
    assert requests_mock.last_request.json() == {"message": "list files in /tmp",
                                                 "server_id": 7}
    assert requests_mock.last_request.headers["Authorization"] == "Bearer sk-test"


def test_create_chat_omits_server_id_when_not_given(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/", status_code=202,
                       json={"chat_id": 1, "status": "processing"})
    _client().chat.create_chat("hello")
    assert requests_mock.last_request.json() == {"message": "hello"}


def test_create_chat_posts_atomic_multi_server_scope(requests_mock):
    requests_mock.post(
        f"{BASE}/api/v1/agent/chat/",
        status_code=202,
        json={"chat_id": 1, "status": "processing"},
    )

    _client().chat.create_chat(
        "compare the clusters",
        server_ids=[9, 12],
        active_server_id=9,
        active_host_id=9,
        selected_namespaces={12: ["default", "vllm"]},
    )

    assert requests_mock.last_request.json() == {
        "message": "compare the clusters",
        "selected_server_ids": [9, 12],
        "active_server_id": 9,
        "active_host_id": 9,
        "selected_namespaces": {"12": ["default", "vllm"]},
    }


def test_create_chat_preserves_explicit_empty_multi_server_scope(requests_mock):
    requests_mock.post(
        f"{BASE}/api/v1/agent/chat/",
        status_code=202,
        json={"chat_id": 1, "status": "processing"},
    )

    _client().chat.create_chat("work without a host", server_ids=[])

    assert requests_mock.last_request.json() == {
        "message": "work without a host",
        "selected_server_ids": [],
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"server_id": 9, "server_ids": [9]},
        {"active_server_id": 9},
        {"active_host_id": 9},
        {"selected_namespaces": {}},
    ],
)
def test_create_chat_rejects_ambiguous_scope_fields_before_request(requests_mock, kwargs):
    with pytest.raises(ValueError):
        _client().chat.create_chat("hello", **kwargs)

    assert not requests_mock.called


def test_send_message_posts_follow_up(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/message/", status_code=202,
                       json={"status": "processing"})
    result = _client().chat.send_message(5, "now show disk usage")
    assert result == {"status": "processing"}
    assert requests_mock.last_request.json() == {"message": "now show disk usage"}


def test_get_status_parses_pending_approvals(requests_mock):
    requests_mock.get(
        f"{BASE}/api/v1/agent/chat/5/status/",
        json={"status": "awaiting_approval", "workflow_type": "react",
              "pending_approvals": [{"approval_id": "abc-123",
                                     "type": "bash_command",
                                     "command": "rm -rf /tmp/old",
                                     "plan_id": "p1",
                                     "reason": "cleanup"}]},
    )
    status = _client().chat.get_status(5)
    assert isinstance(status, ChatStatus)
    assert status.status == "awaiting_approval"
    assert status.workflow_type == "react"
    assert len(status.pending_approvals) == 1
    approval = status.pending_approvals[0]
    assert isinstance(approval, PendingApproval)
    assert approval.approval_id == "abc-123"
    assert approval.command == "rm -rf /tmp/old"
    assert approval.plan_id == "p1"
    assert approval.reason == "cleanup"


def test_get_messages_sends_pagination_and_parses_page(requests_mock):
    requests_mock.get(
        f"{BASE}/api/v1/agent/chat/5/messages/",
        json={"messages": [{"role": "assistant", "content": "done", "sequence": 4}],
              "has_more": True},
    )
    page = _client().chat.get_messages(5, after_sequence=3, limit=50)
    assert isinstance(page, MessagesPage)
    assert page.has_more is True
    assert page.messages == [Message(role="assistant", content="done", sequence=4,
                                     raw={"role": "assistant", "content": "done",
                                          "sequence": 4})]
    assert requests_mock.last_request.qs == {"after_sequence": ["3"], "limit": ["50"]}


def test_approve_posts_decision_with_command(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/approve/abc-123/",
                       json={"success": True, "decision": "approved"})
    result = _client().chat.approve(5, "abc-123", command="ls -la")
    assert isinstance(result, ApprovalResult)
    assert result.success is True
    assert result.decision == "approved"
    assert requests_mock.last_request.json() == {"decision": "approved",
                                                 "type": "bash_command",
                                                 "command": "ls -la"}


def test_reject_posts_decision_with_reason(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/approve/abc-123/",
                       json={"success": True, "decision": "rejected"})
    result = _client().chat.reject(5, "abc-123", approval_type="plan",
                                   reason="too risky")
    assert result.decision == "rejected"
    assert requests_mock.last_request.json() == {"decision": "rejected",
                                                 "type": "plan",
                                                 "rejection_reason": "too risky"}


def test_approval_id_is_url_quoted(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/approve/a%2Fb/", json={})
    _client().chat.approve(5, "a/b")
    assert requests_mock.called


def test_select_server_posts_id(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/select-server/",
                       json={"success": True, "server_id": 9})
    result = _client().chat.select_server(5, 9)
    assert result == {"success": True, "server_id": 9}
    assert requests_mock.last_request.json() == {"server_id": 9}


def test_select_servers_posts_server_ids_only_when_optionals_omitted(requests_mock):
<<<<<<< HEAD
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/select-servers/",
                       json={"success": True, "selected_server_ids": [9, 12]})
    result = _client().chat.select_servers(5, [9, 12])
=======
    requests_mock.post(
        f"{BASE}/api/v1/agent/chat/5/select-servers/",
        json={"success": True, "selected_server_ids": [9, 12]},
    )

    result = _client().chat.select_servers(5, [9, 12])

>>>>>>> origin/main
    assert result == {"success": True, "selected_server_ids": [9, 12]}
    assert requests_mock.last_request.json() == {"selected_server_ids": [9, 12]}


def test_select_servers_posts_all_optional_fields(requests_mock):
    requests_mock.post(
        f"{BASE}/api/v1/agent/chat/5/select-servers/",
        json={
            "success": True,
            "selected_server_ids": [9, 12],
            "active_server_id": 9,
<<<<<<< HEAD
            "active_host_id": 3,
            "selected_namespaces": {"9": ["kube-test"]},
        },
    )
    result = _client().chat.select_servers(
        5, [9, 12],
        active_server_id=9, active_host_id=3,
        selected_namespaces={"9": ["kube-test"]},
    )
    assert result["selected_namespaces"] == {"9": ["kube-test"]}
    assert requests_mock.last_request.json() == {
        "selected_server_ids": [9, 12],
        "active_server_id": 9,
        "active_host_id": 3,
        "selected_namespaces": {"9": ["kube-test"]},
=======
            "active_host_id": 9,
            "selected_namespaces": {"12": ["default"]},
        },
    )

    result = _client().chat.select_servers(
        5,
        [9, 12],
        active_server_id=9,
        active_host_id=9,
        selected_namespaces={"12": ["default"]},
    )

    assert result["selected_namespaces"] == {"12": ["default"]}
    assert requests_mock.last_request.json() == {
        "selected_server_ids": [9, 12],
        "active_server_id": 9,
        "active_host_id": 9,
        "selected_namespaces": {"12": ["default"]},
>>>>>>> origin/main
    }


def test_chat_select_servers_delegates_with_bound_chat_id(requests_mock):
<<<<<<< HEAD
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/select-servers/",
                       json={"success": True, "selected_server_ids": [9]})
    chat = Chat(_client(), 5)
    chat.select_servers([9], selected_namespaces={"9": ["kube-test"]})
    assert requests_mock.last_request.json() == {
        "selected_server_ids": [9],
        "selected_namespaces": {"9": ["kube-test"]},
=======
    requests_mock.post(
        f"{BASE}/api/v1/agent/chat/5/select-servers/",
        json={"success": True, "selected_server_ids": [9]},
    )
    chat = Chat(_client(), 5)

    chat.select_servers([9], selected_namespaces={9: ["__all__"]})

    assert requests_mock.last_request.json() == {
        "selected_server_ids": [9],
        "selected_namespaces": {"9": ["__all__"]},
    }


def test_select_servers_can_clear_scope_and_namespaces(requests_mock):
    requests_mock.post(
        f"{BASE}/api/v1/agent/chat/5/select-servers/",
        json={"success": True, "selected_server_ids": [], "selected_namespaces": {}},
    )

    _client().chat.select_servers(5, [], selected_namespaces={})

    assert requests_mock.last_request.json() == {
        "selected_server_ids": [],
        "selected_namespaces": {},
>>>>>>> origin/main
    }


def test_cancel_posts_optional_reason(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/cancel/",
                       json={"success": True, "status": "cancelled"})
    _client().chat.cancel(5, reason="No longer needed")
    assert requests_mock.last_request.json() == {"reason": "No longer needed"}


def test_cancel_conflict_maps_to_api_error(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/5/cancel/", status_code=409,
                       json={"success": False, "error": "Cannot cancel idle"})
    with pytest.raises(APIError) as exc:
        _client().chat.cancel(5)
    assert exc.value.status_code == 409


def test_wait_returns_first_settled_status(requests_mock):
    requests_mock.get(
        f"{BASE}/api/v1/agent/chat/5/status/",
        [{"json": {"status": "uninitialized"}},
         {"json": {"status": "processing"}},
         {"json": {"status": "idle", "workflow_type": "react"}}],
    )
    status = _client().chat.wait(5, timeout=30.0, poll_interval=0.0)
    assert status.status == "idle"
    assert requests_mock.call_count == 3


def test_wait_without_callback_returns_awaiting_approval(requests_mock):
    requests_mock.get(f"{BASE}/api/v1/agent/chat/5/status/",
                      json={"status": "awaiting_approval",
                            "pending_approvals": [{"approval_id": "a1"}]})
    status = _client().chat.wait(5, timeout=30.0, poll_interval=0.0)
    assert status.status == "awaiting_approval"
    assert requests_mock.call_count == 1


def test_wait_on_approval_true_approves_and_keeps_polling(requests_mock):
    requests_mock.get(
        f"{BASE}/api/v1/agent/chat/5/status/",
        [{"json": {"status": "awaiting_approval",
                   "pending_approvals": [{"approval_id": "a1",
                                          "type": "bash_command",
                                          "command": "ls -la"}]}},
         {"json": {"status": "processing"}},
         {"json": {"status": "idle"}}],
    )
    approve = requests_mock.post(f"{BASE}/api/v1/agent/chat/5/approve/a1/",
                                 json={"success": True, "decision": "approved"})
    seen = []
    status = _client().chat.wait(5, timeout=30.0, poll_interval=0.0,
                                 on_approval=lambda a: seen.append(a) or True)
    assert status.status == "idle"
    assert [a.approval_id for a in seen] == ["a1"]
    assert approve.last_request.json() == {"decision": "approved",
                                           "type": "bash_command",
                                           "command": "ls -la"}


def test_wait_on_approval_false_rejects(requests_mock):
    requests_mock.get(
        f"{BASE}/api/v1/agent/chat/5/status/",
        [{"json": {"status": "awaiting_approval",
                   "pending_approvals": [{"approval_id": "a1",
                                          "type": "bash_command"}]}},
         {"json": {"status": "error"}}],
    )
    submit = requests_mock.post(f"{BASE}/api/v1/agent/chat/5/approve/a1/",
                                json={"success": True, "decision": "rejected"})
    status = _client().chat.wait(5, timeout=30.0, poll_interval=0.0,
                                 on_approval=lambda a: False)
    assert status.status == "error"
    assert submit.last_request.json()["decision"] == "rejected"


def test_wait_on_approval_none_returns_for_caller(requests_mock):
    requests_mock.get(f"{BASE}/api/v1/agent/chat/5/status/",
                      json={"status": "awaiting_approval",
                            "pending_approvals": [{"approval_id": "a1"}]})
    status = _client().chat.wait(5, timeout=30.0, poll_interval=0.0,
                                 on_approval=lambda a: None)
    assert status.status == "awaiting_approval"
    assert requests_mock.call_count == 1


def test_wait_times_out(requests_mock):
    requests_mock.get(f"{BASE}/api/v1/agent/chat/5/status/",
                      json={"status": "processing"})
    with pytest.raises(WaitTimeoutError):
        _client().chat.wait(5, timeout=0.0, poll_interval=0.0)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get_execution_status", "/api/v1/agent/chat/5/execution-status/"),
        ("get_tool_calls", "/api/v1/agent/chat/5/tool-calls/"),
        ("get_reasoning", "/api/v1/agent/chat/5/reasoning/"),
        ("get_plan", "/api/v1/agent/chat/5/plan/"),
        ("get_evaluations", "/api/v1/agent/chat/5/evaluations/"),
        ("get_environment", "/api/v1/agent/chat/5/environment/"),
    ],
)
def test_observability_endpoints_hit_expected_paths(requests_mock, method, path):
    requests_mock.get(f"{BASE}{path}", json={"ok": True})
    result = getattr(_client().chat, method)(5)
    assert result == {"ok": True}
    assert requests_mock.last_request.path == path


def test_get_events_sends_filters(requests_mock):
    requests_mock.get(f"{BASE}/api/v1/agent/chat/5/events/",
                      json={"events": [], "has_more": False})
    _client().chat.get_events(5, after_timestamp="2026-01-01T00:00:00Z",
                              event_types=["StartEvent", "StopEvent"], limit=10)
    assert requests_mock.last_request.qs == {
        "after_timestamp": ["2026-01-01t00:00:00z"],
        "event_types": ["startevent,stopevent"],
        "limit": ["10"],
    }


def test_chat_handle_methods_delegate(requests_mock):
    requests_mock.post(f"{BASE}/api/v1/agent/chat/", status_code=202,
                       json={"chat_id": 7, "status": "processing"})
    requests_mock.get(f"{BASE}/api/v1/agent/chat/7/status/",
                      json={"status": "idle"})
    requests_mock.post(f"{BASE}/api/v1/agent/chat/7/message/", status_code=202,
                       json={"status": "processing"})

    chat = _client().chat.create_chat("hello")
    assert chat.wait(timeout=5.0, poll_interval=0.0).status == "idle"
    assert chat.status().status == "idle"
    assert chat.send("more") is None


def test_auth_failure_maps_to_authentication_error(requests_mock):
    requests_mock.get(f"{BASE}/api/v1/agent/chat/5/status/", status_code=401,
                      json={"detail": "bad key"})
    with pytest.raises(AuthenticationError):
        _client().chat.get_status(5)


def test_message_content_blocks_flatten_to_text(requests_mock):
    # Real wire shape: the server stores content as typed blocks, not a string.
    requests_mock.get(
        f"{BASE}/api/v1/agent/chat/5/messages/",
        json={"messages": [{"role": "assistant", "sequence": 3,
                            "content": [{"type": "text", "text": "line one"},
                                        {"type": "tool_use", "id": "t1"},
                                        {"type": "text", "text": "line two"}]}],
              "has_more": False},
    )
    page = _client().chat.get_messages(5)
    msg = page.messages[0]
    assert msg.content == "line one\nline two"
    assert isinstance(msg.raw["content"], list)


def test_message_content_none_becomes_empty_string(requests_mock):
    requests_mock.get(
        f"{BASE}/api/v1/agent/chat/5/messages/",
        json={"messages": [{"role": "system", "sequence": 1, "content": None}],
              "has_more": False},
    )
    page = _client().chat.get_messages(5)
    assert page.messages[0].content == ""
