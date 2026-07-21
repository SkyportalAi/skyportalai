"""Tests for the Skyportal API client"""

import json
import os
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from skyportal.portal import (
    CLI_USER_AGENT,
    ChatTurnResult,
    CredentialStore,
    PortalError,
    SkyportalClient,
    _MESSAGES_RETRY_MAX_ATTEMPTS,
)


class FakeResponse:
    """Minimal URL response for client tests."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def api_error(status, payload):
    return HTTPError(
        "https://app.skyportal.ai/api/test",
        status,
        "error",
        hdrs=None,
        fp=BytesIO(json.dumps(payload).encode()),
    )


@pytest.fixture
def credential_path(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    monkeypatch.setenv("SKYPORTAL_CREDENTIALS_PATH", str(path))
    monkeypatch.delenv("SKYPORTAL_ACCESS_TOKEN", raising=False)
    return path


def test_credentials_are_private(credential_path):
    CredentialStore.save({"access_token": "sk_test"})

    assert CredentialStore.load() == {"access_token": "sk_test"}
    if os.name != "nt":
        assert credential_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("content", ["not json", "[]"])
def test_invalid_credentials_file_has_a_clear_error(credential_path, content):
    credential_path.write_text(content)

    with pytest.raises(PortalError, match="credentials"):
        CredentialStore.load()


def test_production_marketing_host_is_normalized_to_app(credential_path):
    client = SkyportalClient("https://skyportal.ai/")

    assert client.base_url == "https://app.skyportal.ai"
    assert client.api_key_url() == "https://app.skyportal.ai/keys/?source=cli"


def test_custom_deployment_is_not_rewritten(credential_path):
    client = SkyportalClient("https://skyportal.example/")

    assert client.base_url == "https://skyportal.example"


def test_cli_client_refuses_remote_cleartext(credential_path):
    with pytest.raises(PortalError, match="non-HTTPS"):
        SkyportalClient("http://skyportal.example")


def test_cli_client_allows_loopback_cleartext(credential_path):
    client = SkyportalClient("http://127.0.0.1:8000")
    assert client.base_url == "http://127.0.0.1:8000"


def test_cli_client_rejects_non_positive_timeout(credential_path):
    with pytest.raises(PortalError, match="timeout"):
        SkyportalClient("https://app.skyportal.ai", timeout=0)


def test_login_opens_account_api_key_page(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    callback = []

    with patch("skyportal.portal.webbrowser.open", return_value=True) as browser:
        result = client.login(
            authorization_callback=lambda url, code: callback.append((url, code))
        )

    expected = "https://app.skyportal.ai/keys/?source=cli"
    assert callback == [(expected, None)]
    assert result == {"verification_url": expected, "browser_opened": True}
    browser.assert_called_once_with(expected)


@pytest.mark.parametrize("token", ["sk_valid-account-key", "skt_valid-access-token"])
def test_supported_token_is_validated_before_save(credential_path, token):
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportal.portal.urlopen", return_value=FakeResponse([])) as call:
        client.set_access_token("  {}  ".format(token))

    request = call.call_args.args[0]
    assert request.full_url == "https://app.skyportal.ai/api/v1/experiments/my-servers/"
    assert request.get_header("Authorization") == "Bearer " + token
    assert request.get_header("User-agent") == CLI_USER_AGENT
    assert CredentialStore.load() == {
        "access_token": token,
        "token_type": "Bearer",
        "base_url": "https://app.skyportal.ai",
    }


def test_invalid_token_is_not_saved_over_working_credential(credential_path):
    CredentialStore.save(
        {
            "access_token": "sk_working",
            "base_url": "https://app.skyportal.ai",
        }
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch(
        "skyportal.portal.urlopen",
        side_effect=api_error(403, {"detail": "Invalid API key"}),
    ):
        with pytest.raises(PortalError, match="Invalid API key"):
            client.set_access_token("sk_invalid")

    assert CredentialStore.load()["access_token"] == "sk_working"


def test_agent_deployment_token_is_rejected_with_key_page(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportal.portal.urlopen") as call:
        with pytest.raises(PortalError, match="Agent deployment tokens.*agt_.*account API key"):
            client.set_access_token("agt_host-observability-token")

    call.assert_not_called()
    assert CredentialStore.load() is None


def test_saved_agent_token_does_not_report_connected(credential_path):
    CredentialStore.save(
        {
            "access_token": "agt_host-observability-token",
            "base_url": "https://app.skyportal.ai",
        }
    )

    assert SkyportalClient("https://app.skyportal.ai").is_authenticated() is False


def test_server_list_uses_real_owned_server_endpoint(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")
    payload = [{"id": "7", "hostname": "gpu-7"}]

    with patch("skyportal.portal.urlopen", return_value=FakeResponse(payload)) as call:
        assert client.servers() == payload

    request = call.call_args.args[0]
    assert request.full_url == "https://app.skyportal.ai/api/v1/experiments/my-servers/"
    assert request.get_header("Authorization") == "Bearer sk_test"


def test_create_chat_posts_message_and_optional_server(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch(
        "skyportal.portal.urlopen",
        return_value=FakeResponse({"chat_id": 42, "status": "processing"}),
    ) as call:
        response = client.create_chat("Check GPU health", server_id=7)

    request = call.call_args.args[0]
    assert response["chat_id"] == 42
    assert request.method == "POST"
    assert request.full_url == "https://app.skyportal.ai/api/v1/agent/chat/"
    assert json.loads(request.data) == {"message": "Check GPU health", "server_id": 7}


def test_follow_up_message_uses_existing_chat(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportal.portal.urlopen", return_value=FakeResponse({"status": "processing"})) as call:
        client.send_chat_message(42, "Now show memory")

    request = call.call_args.args[0]
    assert request.full_url == "https://app.skyportal.ai/api/v1/agent/chat/42/message/"
    assert json.loads(request.data) == {"message": "Now show memory"}


def test_wait_for_chat_polls_and_returns_message_cursor(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    messages = {
        "messages": [
            {"sequence": 4, "role": "user", "content": [{"type": "text", "text": "Hi"}]},
            {
                "sequence": 5,
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
            },
        ],
        "has_more": False,
    }

    with patch.object(
        client,
        "chat_status",
        side_effect=[
            {"status": "uninitialized", "pending_approvals": []},
            {"status": "processing", "pending_approvals": []},
            {"status": "idle", "pending_approvals": []},
        ],
    ), patch.object(client, "chat_messages", return_value=messages) as get_messages, patch(
        "skyportal.portal.time.sleep"
    ):
        result = client.wait_for_chat(42, after_sequence=3, poll_interval=0)

    assert result == ChatTurnResult(
        chat_id=42,
        status="idle",
        messages=messages["messages"],
        pending_approvals=[],
        latest_sequence=5,
    )
    get_messages.assert_called_once_with(42, after_sequence=3)


def test_wait_for_chat_invokes_on_progress_each_poll_tick(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    seen = []

    with patch.object(
        client,
        "chat_status",
        side_effect=[
            {"status": "processing", "pending_approvals": []},
            {"status": "processing", "pending_approvals": []},
            {"status": "idle", "pending_approvals": []},
        ],
    ), patch.object(
        client,
        "get_execution_status",
        side_effect=[
            {"live_command_output": {"command": "ls", "output": "a\n"}},
            {"live_command_output": {"command": "ls", "output": "a\nb\n"}},
            {"live_command_output": None},
        ],
    ), patch.object(
        client, "chat_messages", return_value={"messages": [], "has_more": False},
    ), patch("skyportal.portal.time.sleep"):
        client.wait_for_chat(42, poll_interval=0, on_progress=seen.append)

    assert seen == [
        {"command": "ls", "output": "a\n"},
        {"command": "ls", "output": "a\nb\n"},
        None,
    ]


def test_wait_for_chat_on_progress_exception_does_not_break_wait(credential_path):
    """A callback bug (or a failure in the extra get_execution_status() call)
    must never interrupt the wait — the status-poll loop's own correctness
    is unrelated and must not be put at risk by this side channel."""
    client = SkyportalClient("https://app.skyportal.ai")

    def _raises(_info):
        raise RuntimeError("rendering bug")

    with patch.object(
        client,
        "chat_status",
        side_effect=[
            {"status": "processing", "pending_approvals": []},
            {"status": "idle", "pending_approvals": []},
        ],
    ), patch.object(
        client, "get_execution_status", side_effect=RuntimeError("network hiccup"),
    ), patch.object(
        client, "chat_messages", return_value={"messages": [], "has_more": False},
    ), patch("skyportal.portal.time.sleep"):
        result = client.wait_for_chat(42, poll_interval=0, on_progress=_raises)

    assert result.status == "idle"


def test_wait_for_chat_without_on_progress_never_calls_execution_status(credential_path):
    """Callers that don't pass on_progress shouldn't pay for the extra
    per-tick HTTP round trip at all."""
    client = SkyportalClient("https://app.skyportal.ai")

    with patch.object(
        client, "chat_status", return_value={"status": "idle", "pending_approvals": []},
    ), patch.object(client, "get_execution_status") as get_exec_status, patch.object(
        client, "chat_messages", return_value={"messages": [], "has_more": False},
    ), patch("skyportal.portal.time.sleep"):
        client.wait_for_chat(42, poll_interval=0)

    get_exec_status.assert_not_called()


def test_wait_for_chat_retries_when_first_fetch_is_empty(credential_path):
    """Reproduced live: chat_status() can flip to a terminal status (e.g.
    awaiting_input) before the message that justifies it is queryable via
    chat_messages() — a fast, LLM-free turn (one host-collection step) can
    win that race and come back empty on the first fetch. wait_for_chat
    must keep retrying (with backoff, bounded by the same deadline as the
    status poll) rather than report an empty turn for a response that
    exists but isn't visible yet."""
    client = SkyportalClient("https://app.skyportal.ai")
    real_messages = {
        "messages": [
            {"sequence": 12, "role": "assistant", "content": [{"type": "text", "text": "What's the name?"}]},
        ],
        "has_more": False,
    }
    empty_messages = {"messages": [], "has_more": False}

    # monotonic() is called once for the initial deadline, then once per
    # loop-condition check in each while loop — keep it comfortably inside
    # the deadline throughout so the retry loop runs on chat_messages'
    # side_effect exhausting, not on hitting the (mocked) clock.
    with patch.object(
        client, "chat_status", return_value={"status": "awaiting_input", "pending_approvals": []},
    ), patch.object(
        client, "chat_messages", side_effect=[empty_messages, empty_messages, real_messages],
    ) as get_messages, patch("skyportal.portal.time.sleep") as sleep, patch(
        "skyportal.portal.time.monotonic", return_value=0.0,
    ):
        result = client.wait_for_chat(983, after_sequence=10, poll_interval=0, timeout=300)

    assert result.messages == real_messages["messages"]
    assert result.latest_sequence == 12
    assert get_messages.call_count == 3
    # Backoff doubles each retry starting at 0.25s, capped at 2.0s.
    assert [call.args[0] for call in sleep.call_args_list] == [0.25, 0.5]


def test_wait_for_chat_retries_indefinitely_with_no_timeout(credential_path):
    """Regression test: timeout=None (the default) must not crash when the
    messages-retry loop's own deadline check runs. This is a genuinely
    separate code path from the status-poll loop above (test_wait_for_chat_
    retries_when_first_fetch_is_empty only exercises timeout=None there) —
    reproduced live via `>=` not supported between 'float' and 'NoneType',
    a second raw deadline comparison further down wait_for_chat that the
    None-timeout support initially missed."""
    client = SkyportalClient("https://app.skyportal.ai")
    real_messages = {
        "messages": [
            {"sequence": 12, "role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        ],
        "has_more": False,
    }
    empty_messages = {"messages": [], "has_more": False}

    with patch.object(
        client, "chat_status", return_value={"status": "idle", "pending_approvals": []},
    ), patch.object(
        client, "chat_messages", side_effect=[empty_messages, real_messages],
    ) as get_messages, patch("skyportal.portal.time.sleep"):
        result = client.wait_for_chat(983, after_sequence=10, poll_interval=0)

    assert result.messages == real_messages["messages"]
    assert result.latest_sequence == 12
    assert get_messages.call_count == 2


def test_wait_for_chat_gives_up_at_deadline_on_genuinely_empty_turn(credential_path):
    """A turn that really did produce no new messages must still return an
    empty result once the deadline passes, not hang or raise."""
    client = SkyportalClient("https://app.skyportal.ai")
    empty_messages = {"messages": [], "has_more": False}

    # First monotonic() call sets the deadline (0.0 + timeout); the status
    # loop's own condition check needs to stay under that deadline so
    # chat_status() gets a chance to report "idle" and break out normally.
    # Only once we reach the messages-retry loop does the clock jump past
    # the deadline, so it exits on its first empty fetch instead of
    # looping (or sleeping) in the test.
    clock = iter([0.0, 0.1] + [1000.0] * 10)

    with patch.object(
        client, "chat_status", return_value={"status": "idle", "pending_approvals": []},
    ), patch.object(
        client, "chat_messages", return_value=empty_messages,
    ) as get_messages, patch("skyportal.portal.time.sleep") as sleep, patch(
        "skyportal.portal.time.monotonic", side_effect=lambda: next(clock),
    ):
        result = client.wait_for_chat(983, after_sequence=10, poll_interval=0, timeout=1)

    assert result.messages == []
    assert result.latest_sequence == 10
    assert get_messages.call_count == 1
    sleep.assert_not_called()


def test_wait_for_chat_messages_retry_loop_bounded_even_with_no_deadline(credential_path):
    """Regression test: with timeout=None (the default), a genuinely empty
    turn must NOT spin the messages-retry loop forever. Reproduced live —
    the loop's old exit condition only checked the overall deadline, which
    is None by default now that wait_for_chat polls indefinitely, so a
    turn with truly no new messages hung at 100% CPU with no progress."""
    client = SkyportalClient("https://app.skyportal.ai")
    empty_messages = {"messages": [], "has_more": False}

    with patch.object(
        client, "chat_status", return_value={"status": "idle", "pending_approvals": []},
    ), patch.object(
        client, "chat_messages", return_value=empty_messages,
    ) as get_messages, patch("skyportal.portal.time.sleep") as sleep:
        result = client.wait_for_chat(983, after_sequence=10, poll_interval=0)

    assert result.messages == []
    assert result.latest_sequence == 10
    # Must actually stop — the loop's own fixed retry cap, not a borrowed
    # deadline that doesn't exist in this mode.
    assert get_messages.call_count == _MESSAGES_RETRY_MAX_ATTEMPTS
    assert sleep.call_count == _MESSAGES_RETRY_MAX_ATTEMPTS - 1


def test_run_chat_turn_continues_existing_chat(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    completed = ChatTurnResult(42, "idle", [], [], 9)

    with patch.object(client, "send_chat_message") as send, patch.object(
        client, "wait_for_chat", return_value=completed
    ) as wait:
        result = client.run_chat_turn(
            "Continue",
            chat_id=42,
            after_sequence=8,
            poll_interval=0,
        )

    assert result is completed
    send.assert_called_once_with(42, "Continue")
    wait.assert_called_once_with(42, after_sequence=8, timeout=None, poll_interval=0, on_progress=None)


def test_approval_and_server_selection_use_headless_endpoints(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportal.portal.urlopen", return_value=FakeResponse({"success": True})) as call:
        client.submit_chat_approval(
            42,
            {"approval_id": "approval/id", "type": "bash_command", "command": "df -h"},
            "approved",
        )
        approval_request = call.call_args.args[0]
        client.select_chat_server(42, 7)
        server_request = call.call_args.args[0]

    assert approval_request.full_url.endswith(
        "/api/v1/agent/chat/42/approve/approval%2Fid/"
    )
    assert json.loads(approval_request.data) == {
        "decision": "approved",
        "type": "bash_command",
        "command": "df -h",
    }
    assert server_request.full_url.endswith("/api/v1/agent/chat/42/select-server/")
    assert json.loads(server_request.data) == {"server_id": 7}


def test_assistant_text_uses_newest_assistant_message(credential_path):
    messages = [
        {"sequence": 2, "role": "assistant", "content": [{"type": "text", "text": "old"}]},
        {"sequence": 4, "role": "tool", "content": [{"type": "text", "text": "tool"}]},
        {
            "sequence": 5,
            "role": "assistant",
            "content": [
                {"type": "text", "text": "new"},
                {"type": "text", "text": "answer"},
            ],
        },
    ]

    assert SkyportalClient.assistant_text(messages) == "new\nanswer"


def test_missing_credentials_are_reported(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")

    with pytest.raises(PortalError, match="Not connected"):
        client.servers()


def test_credentials_are_scoped_to_deployment(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://another.example"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with pytest.raises(PortalError, match="another Skyportal deployment"):
        client.servers()


def test_begin_chat_turn_creates_new_chat(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    with patch.object(client, "create_chat", return_value={"chat_id": 77}) as create:
        chat_id = client.begin_chat_turn("hello", server_id=3)
    assert chat_id == 77
    create.assert_called_once_with("hello", server_id=3)


def test_begin_chat_turn_continues_existing_chat(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    with patch.object(client, "send_chat_message") as send:
        chat_id = client.begin_chat_turn("more", chat_id=42)
    assert chat_id == 42
    send.assert_called_once_with(42, "more")


def test_begin_chat_turn_rejects_missing_chat_id(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    with patch.object(client, "create_chat", return_value={}):
        with pytest.raises(PortalError):
            client.begin_chat_turn("hello")


def test_cancel_chat_posts_reason_to_cancel_endpoint(credential_path):
    CredentialStore.save({"access_token": "sk_test", "base_url": "https://app.skyportal.ai"})
    client = SkyportalClient("https://app.skyportal.ai")
    with patch(
        "skyportal.portal.urlopen",
        return_value=FakeResponse({"success": True, "status": "cancelled"}),
    ) as call:
        result = client.cancel_chat(42, reason="user hit ctrl-c")
        request = call.call_args.args[0]
    assert result["status"] == "cancelled"
    assert request.full_url.endswith("/api/v1/agent/chat/42/cancel/")
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"reason": "user hit ctrl-c"}


def test_cancel_chat_without_reason_sends_empty_body(credential_path):
    CredentialStore.save({"access_token": "sk_test", "base_url": "https://app.skyportal.ai"})
    client = SkyportalClient("https://app.skyportal.ai")
    with patch(
        "skyportal.portal.urlopen", return_value=FakeResponse({"success": True})
    ) as call:
        client.cancel_chat(42)
        request = call.call_args.args[0]
    assert json.loads(request.data) == {}


def test_get_github_token_status_calls_correct_endpoint(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")
    payload = {"has_token": True, "masked_token": "ghp_****abc"}

    with patch("skyportal.portal.urlopen", return_value=FakeResponse(payload)) as call:
        result = client.get_github_token_status()

    request = call.call_args.args[0]
    assert result == payload
    assert request.method == "GET"
    assert request.full_url == "https://app.skyportal.ai/api/v1/agent/github-token/"
    assert request.get_header("Authorization") == "Bearer sk_test"


def test_save_github_token_posts_token_and_optional_repo(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")
    payload = {"success": True, "masked_token": "ghp_****abc", "login": "octocat"}

    with patch("skyportal.portal.urlopen", return_value=FakeResponse(payload)) as call:
        result = client.save_github_token("ghp_realtoken", repo="owner/repo")

    request = call.call_args.args[0]
    assert result == payload
    assert request.method == "POST"
    assert request.full_url == "https://app.skyportal.ai/api/v1/agent/github-token/save/"
    assert json.loads(request.data) == {"token": "ghp_realtoken", "repo": "owner/repo"}


def test_save_github_token_omits_repo_when_not_given(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")
    payload = {"success": True, "masked_token": "ghp_****abc", "login": "octocat"}

    with patch("skyportal.portal.urlopen", return_value=FakeResponse(payload)) as call:
        client.save_github_token("ghp_realtoken")

    request = call.call_args.args[0]
    assert json.loads(request.data) == {"token": "ghp_realtoken"}


def test_save_github_token_raises_portal_error_on_400(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch(
        "skyportal.portal.urlopen",
        side_effect=api_error(400, {"error": "Invalid token or insufficient scope"}),
    ):
        with pytest.raises(PortalError, match="Invalid token or insufficient scope"):
            client.save_github_token("ghp_bad")


def test_delete_github_token_calls_delete_endpoint(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportal.portal.urlopen", return_value=FakeResponse({"success": True})) as call:
        client.delete_github_token()

    request = call.call_args.args[0]
    assert request.method == "DELETE"
    assert request.full_url == "https://app.skyportal.ai/api/v1/agent/github-token/delete/"
