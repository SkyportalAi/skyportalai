"""Tests for the Skyportal API client"""

import json
import os
from io import BytesIO
from unittest.mock import call, patch
from urllib.error import HTTPError

import pytest

from skyportal.portal import (
    CLI_USER_AGENT,
    ChatTurnResult,
    CredentialStore,
    PortalError,
    SkyportalClient,
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
    monkeypatch.delenv("SKYPORTAL_API_KEY", raising=False)
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


def test_api_key_env_alias_is_used_for_authentication(credential_path, monkeypatch):
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk_env_alias")

    client = SkyportalClient("https://app.skyportal.ai")
    assert client.is_authenticated() is True

    with patch("skyportal.portal.urlopen", return_value=FakeResponse([])) as call:
        assert client.servers() == []

    request = call.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer " + "sk_env_alias"


def test_access_token_env_precedes_api_key_alias(credential_path, monkeypatch):
    monkeypatch.setenv("SKYPORTAL_ACCESS_TOKEN", "skt_primary")
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk_alias")

    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportal.portal.urlopen", return_value=FakeResponse([])) as call:
        assert client.servers() == []

    request = call.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer " + "skt_primary"


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


def test_create_chat_posts_atomic_multi_server_scope(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch(
        "skyportal.portal.urlopen",
        return_value=FakeResponse({"chat_id": 42, "status": "processing"}),
    ) as call:
        client.create_chat(
            "Compare hosts",
            server_ids=[7, 9, 7],
            selected_namespaces={9: ["default"]},
        )

    request = call.call_args.args[0]
    assert json.loads(request.data) == {
        "message": "Compare hosts",
        "selected_server_ids": [7, 9],
        "active_server_id": 7,
        "selected_namespaces": {"9": ["default"]},
    }


def test_create_chat_rejects_singular_and_plural_server_options(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")

    with pytest.raises(PortalError, match="not both"):
        client.create_chat("Compare hosts", server_id=7, server_ids=[7, 9])


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


def test_get_execution_status_uses_detailed_status_endpoint(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")
    payload = {"status": "processing", "current_step": "Inspect logs"}

    with patch("skyportal.portal.urlopen", return_value=FakeResponse(payload)) as request_call:
        assert client.get_execution_status(42) == payload

    request = request_call.call_args.args[0]
    assert request.full_url.endswith("/api/v1/agent/chat/42/execution-status/")


def test_wait_for_chat_polls_and_returns_message_cursor(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    busy_message = {
        "sequence": 4,
        "role": "assistant",
        "content": [{"type": "text", "text": "Checking"}],
    }
    terminal_message = {
        "sequence": 5,
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello"}],
    }

    with patch.object(
        client,
        "chat_status",
        side_effect=[
            {"status": "processing", "pending_approvals": []},
            {"status": "idle", "pending_approvals": []},
        ],
    ), patch.object(
        client,
        "chat_messages",
        side_effect=[
            {"messages": [busy_message], "has_more": False},
            {"messages": [terminal_message], "has_more": False},
        ],
    ) as get_messages, patch.object(client, "get_execution_status") as detailed_status, patch(
        "skyportal.portal.time.sleep"
    ):
        result = client.wait_for_chat(42, after_sequence=3, poll_interval=0)

    assert result == ChatTurnResult(
        chat_id=42,
        status="idle",
        messages=[busy_message, terminal_message],
        pending_approvals=[],
        latest_sequence=5,
    )
    assert get_messages.call_args_list == [
        call(42, after_sequence=3),
        call(42, after_sequence=4),
    ]
    detailed_status.assert_not_called()


def test_wait_for_chat_streams_only_new_messages_and_returns_all_observed_messages(
    credential_path,
):
    client = SkyportalClient("https://app.skyportal.ai")
    first = {"sequence": 4, "role": "assistant", "content": "first"}
    second = {"sequence": 5, "role": "tool", "content": "second"}
    final = {"sequence": 6, "role": "assistant", "content": "final"}
    delivered = []

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
        "chat_messages",
        side_effect=[
            {"messages": [first], "has_more": False},
            # Deliberately replay sequence 4 even though the cursor is 4.
            {"messages": [first, second, second], "has_more": False},
            {"messages": [final], "has_more": False},
        ],
    ) as get_messages, patch.object(client, "get_execution_status") as detailed_status, patch(
        "skyportal.portal.time.sleep"
    ):
        result = client.wait_for_chat(
            42,
            after_sequence=3,
            poll_interval=0,
            on_progress=delivered.append,
        )

    assert delivered == [[first], [second]]
    assert result.messages == [first, second, final]
    assert result.latest_sequence == 6
    assert get_messages.call_args_list == [
        call(42, after_sequence=3),
        call(42, after_sequence=4),
        call(42, after_sequence=5),
    ]
    detailed_status.assert_not_called()


def test_wait_for_chat_preserves_batch_when_progress_callback_fails(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    message = {"sequence": 4, "role": "assistant", "content": "still visible"}

    def fail_to_render(_messages):
        raise RuntimeError("renderer failed")

    with patch.object(
        client,
        "chat_status",
        side_effect=[
            {"status": "processing", "pending_approvals": []},
            {"status": "idle", "pending_approvals": []},
        ],
    ), patch.object(
        client,
        "chat_messages",
        side_effect=[
            {"messages": [message], "has_more": False},
            {"messages": [], "has_more": False},
            {"messages": [], "has_more": False},
            {"messages": [], "has_more": False},
            {"messages": [], "has_more": False},
            {"messages": [], "has_more": False},
        ],
    ), patch("skyportal.portal.time.sleep"):
        result = client.wait_for_chat(
            42,
            after_sequence=3,
            poll_interval=0,
            on_progress=fail_to_render,
        )

    assert result.messages == [message]
    assert result.latest_sequence == 4


def test_wait_for_chat_retries_once_when_only_earlier_messages_were_observed(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    prompt = {"sequence": 4, "role": "user", "content": "inspect the host"}
    final = {"sequence": 5, "role": "assistant", "content": "finished"}

    with patch.object(
        client,
        "chat_status",
        side_effect=[
            {"status": "processing", "pending_approvals": []},
            {"status": "idle", "pending_approvals": []},
        ],
    ), patch.object(
        client,
        "chat_messages",
        side_effect=[
            {"messages": [prompt], "has_more": False},
            {"messages": [], "has_more": False},
            {"messages": [final], "has_more": False},
        ],
    ) as get_messages, patch("skyportal.portal.time.sleep") as sleep:
        result = client.wait_for_chat(42, after_sequence=3, poll_interval=0)

    assert result.messages == [prompt, final]
    assert get_messages.call_count == 3
    # Main poll plus exactly one short terminal-settlement retry.
    assert [item.args[0] for item in sleep.call_args_list] == [0, 0.25]


def test_wait_for_chat_retries_when_first_fetch_is_empty(credential_path):
    """Reproduced live: chat_status() can flip to a terminal status (e.g.
    awaiting_input) before the message that justifies it is queryable via
    chat_messages() — a fast, LLM-free turn (one host-collection step) can
    win that race and come back empty on the first fetch. wait_for_chat
    must keep retrying with its own bounded settlement budget rather than
    report an empty turn for a response that exists but isn't visible yet."""
    client = SkyportalClient("https://app.skyportal.ai")
    real_messages = {
        "messages": [
            {"sequence": 12, "role": "assistant", "content": [{"type": "text", "text": "What's the name?"}]},
        ],
        "has_more": False,
    }
    empty_messages = {"messages": [], "has_more": False}

    with patch.object(
        client, "chat_status", return_value={"status": "awaiting_input", "pending_approvals": []},
    ), patch.object(
        client, "chat_messages", side_effect=[empty_messages, empty_messages, real_messages],
    ) as get_messages, patch("skyportal.portal.time.sleep") as sleep:
        result = client.wait_for_chat(983, after_sequence=10, poll_interval=0, timeout=300)

    assert result.messages == real_messages["messages"]
    assert result.latest_sequence == 12
    assert get_messages.call_count == 3
    # Backoff doubles each retry starting at 0.25s, capped at 2.0s.
    assert [call.args[0] for call in sleep.call_args_list] == [0.25, 0.5]


def test_wait_for_chat_terminal_message_settlement_is_independently_bounded(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    empty_messages = {"messages": [], "has_more": False}

    with patch.object(
        client, "chat_status", return_value={"status": "idle", "pending_approvals": []},
    ), patch.object(
        client, "chat_messages", return_value=empty_messages,
    ) as get_messages, patch("skyportal.portal.time.sleep") as sleep, patch(
        "skyportal.portal.time.monotonic", return_value=0.0,
    ):
        result = client.wait_for_chat(983, after_sequence=10, poll_interval=0, timeout=1)

    assert result.messages == []
    assert result.latest_sequence == 10
    assert get_messages.call_count == 5
    assert [item.args[0] for item in sleep.call_args_list] == [0.25, 0.5, 1.0, 2.0]


@pytest.mark.parametrize("status", ["awaiting_approval", "error"])
def test_wait_for_chat_approval_and_error_return_after_one_message_fetch(
    credential_path,
    status,
):
    client = SkyportalClient("https://app.skyportal.ai")
    pending = [{"approval_id": "a1"}] if status == "awaiting_approval" else []

    with patch.object(
        client,
        "chat_status",
        return_value={"status": status, "pending_approvals": pending},
    ), patch.object(
        client,
        "chat_messages",
        return_value={"messages": [], "has_more": False},
    ) as get_messages, patch("skyportal.portal.time.sleep") as sleep:
        result = client.wait_for_chat(42, after_sequence=7)

    assert result.status == status
    assert result.pending_approvals == pending
    get_messages.assert_called_once_with(42, after_sequence=7)
    sleep.assert_not_called()


def test_wait_for_chat_new_messages_extend_idle_deadline(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    now = [0.0]
    progress = {"sequence": 4, "role": "assistant", "content": "working"}
    final = {"sequence": 5, "role": "assistant", "content": "done"}

    def advance(seconds):
        now[0] += seconds

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
        "chat_messages",
        side_effect=[
            {"messages": [], "has_more": False},
            {"messages": [progress], "has_more": False},
            {"messages": [final], "has_more": False},
        ],
    ), patch("skyportal.portal.time.monotonic", side_effect=lambda: now[0]), patch(
        "skyportal.portal.time.sleep", side_effect=advance
    ):
        result = client.wait_for_chat(42, after_sequence=3, timeout=3, poll_interval=2)

    # The second poll's new message at t=2 extends the original t=3 deadline,
    # allowing the terminal status to be observed at t=4.
    assert result.messages == [progress, final]
    assert result.latest_sequence == 5


def test_wait_for_chat_streams_public_status_snapshots(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    processing = {
        "status": "processing",
        "pending_approvals": [],
        "activity": {"phase": "executing_plan", "label": "Working on plan step 2/13"},
    }
    idle = {
        "status": "idle",
        "pending_approvals": [],
        "activity": {"phase": "idle", "label": "Idle"},
    }
    snapshots = []

    with patch.object(client, "chat_status", side_effect=[processing, idle]), patch.object(
        client, "chat_messages", return_value={"messages": [], "has_more": False}
    ), patch("skyportal.portal.time.sleep"):
        client.wait_for_chat(42, poll_interval=0, on_status=snapshots.append)

    assert snapshots == [processing, idle]


def test_wait_for_chat_duplicate_message_snapshot_does_not_extend_idle_deadline(
    credential_path,
):
    client = SkyportalClient("https://app.skyportal.ai")
    now = [0.0]
    progress = {"sequence": 4, "role": "assistant", "content": "working"}
    delivered = []

    def advance(seconds):
        now[0] += seconds

    with patch.object(
        client,
        "chat_status",
        return_value={"status": "processing", "pending_approvals": []},
    ) as get_status, patch.object(
        client,
        "chat_messages",
        return_value={"messages": [progress], "has_more": False},
    ) as get_messages, patch("skyportal.portal.time.monotonic", side_effect=lambda: now[0]), patch(
        "skyportal.portal.time.sleep", side_effect=advance
    ):
        with pytest.raises(PortalError, match="no progress for 3.5 seconds"):
            client.wait_for_chat(
                42,
                after_sequence=3,
                timeout=3.5,
                poll_interval=2,
                on_progress=delivered.append,
            )

    assert delivered == [[progress]]
    assert get_status.call_count == 2
    assert get_messages.call_args_list == [
        call(42, after_sequence=3),
        call(42, after_sequence=4),
    ]


def test_wait_for_chat_status_transition_extends_idle_deadline(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    now = [0.0]
    final = {"sequence": 1, "role": "assistant", "content": "done"}

    def advance(seconds):
        now[0] += seconds

    with patch.object(
        client,
        "chat_status",
        side_effect=[
            {"status": "uninitialized", "pending_approvals": []},
            {"status": "processing", "pending_approvals": []},
            {"status": "idle", "pending_approvals": []},
        ],
    ), patch.object(
        client,
        "chat_messages",
        side_effect=[
            {"messages": [], "has_more": False},
            {"messages": [], "has_more": False},
            {"messages": [final], "has_more": False},
        ],
    ), patch("skyportal.portal.time.monotonic", side_effect=lambda: now[0]), patch(
        "skyportal.portal.time.sleep", side_effect=advance
    ):
        result = client.wait_for_chat(42, timeout=3, poll_interval=2)

    assert result.messages == [final]


def test_wait_for_chat_default_disables_idle_deadline(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    final = {"sequence": 1, "role": "assistant", "content": "done"}

    with patch.object(
        client,
        "chat_status",
        side_effect=[
            {"status": "processing", "pending_approvals": []},
            {"status": "idle", "pending_approvals": []},
        ],
    ), patch.object(
        client,
        "chat_messages",
        side_effect=[
            {"messages": [], "has_more": False},
            {"messages": [final], "has_more": False},
        ],
    ), patch("skyportal.portal.time.monotonic", side_effect=AssertionError("deadline used")), patch(
        "skyportal.portal.time.sleep"
    ):
        result = client.wait_for_chat(42, poll_interval=0)

    assert result.messages == [final]


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
    wait.assert_called_once_with(
        42,
        after_sequence=8,
        timeout=None,
        poll_interval=0,
        on_progress=None,
        on_status=None,
    )


def test_run_chat_turn_forwards_progress_callback(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    completed = ChatTurnResult(42, "idle", [], [], 9)
    progress_batches = []

    with patch.object(client, "begin_chat_turn", return_value=42), patch.object(
        client, "wait_for_chat", return_value=completed
    ) as wait:
        result = client.run_chat_turn(
            "Continue",
            chat_id=42,
            on_progress=progress_batches.append,
        )

    assert result is completed
    wait.assert_called_once_with(
        42,
        after_sequence=0,
        timeout=None,
        poll_interval=1,
        on_progress=progress_batches.append,
        on_status=None,
    )


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
        client.submit_chat_approval(
            42,
            {"approval_id": "auto", "type": "plan"},
            "approved",
            autoapproved=True,
        )
        autoapproval_request = call.call_args.args[0]
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
    assert json.loads(autoapproval_request.data) == {
        "decision": "approved",
        "type": "plan",
        "autoapproved": True,
    }
    assert server_request.full_url.endswith("/api/v1/agent/chat/42/select-server/")
    assert json.loads(server_request.data) == {"server_id": 7}


def test_multi_server_selection_uses_plural_headless_endpoint(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportal.portal.urlopen", return_value=FakeResponse({"success": True})) as call:
        client.select_chat_servers(
            42,
            [7, 9, 7],
            selected_namespaces={9: ["__all__"]},
        )

    request = call.call_args.args[0]
    assert request.full_url.endswith("/api/v1/agent/chat/42/select-servers/")
    assert json.loads(request.data) == {
        "selected_server_ids": [7, 9],
        "active_server_id": 7,
        "selected_namespaces": {"9": ["__all__"]},
    }


@pytest.mark.parametrize(
    "scope_kwargs",
    [
        {"active_server_id": 7},
        {"active_host_id": 7},
        {"selected_namespaces": {7: ["default"]}},
    ],
)
def test_create_chat_rejects_plural_scope_fields_without_server_ids(
    credential_path, scope_kwargs
):
    client = SkyportalClient("https://app.skyportal.ai")

    with pytest.raises(PortalError, match="server_ids is required"):
        client.create_chat("check host", **scope_kwargs)


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


def test_raw_request_timeout_is_wrapped_as_portal_error(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportal.portal.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(PortalError, match="request timed out"):
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


def test_begin_chat_turn_creates_new_chat_with_multi_server_scope(credential_path):
    client = SkyportalClient("https://app.skyportal.ai")
    with patch.object(client, "create_chat", return_value={"chat_id": 77}) as create:
        chat_id = client.begin_chat_turn(
            "hello",
            server_ids=[3, 4],
            active_server_id=4,
            selected_namespaces={4: ["default"]},
        )

    assert chat_id == 77
    create.assert_called_once_with(
        "hello",
        server_id=None,
        server_ids=[3, 4],
        active_server_id=4,
        active_host_id=None,
        selected_namespaces={4: ["default"]},
    )


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


def test_get_permission_mode_uses_shared_account_endpoint(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch(
        "skyportal.portal.urlopen",
        return_value=FakeResponse({"permission_mode": "ask", "read_only_mode": False}),
    ) as call:
        assert client.get_permission_mode() == "ask"

    request = call.call_args.args[0]
    assert request.method == "GET"
    assert request.full_url == "https://app.skyportal.ai/api/v1/agent/permission/"


def test_set_permission_mode_puts_only_supported_mode(credential_path):
    CredentialStore.save(
        {"access_token": "sk_test", "base_url": "https://app.skyportal.ai"}
    )
    client = SkyportalClient("https://app.skyportal.ai")

    with patch(
        "skyportal.portal.urlopen",
        return_value=FakeResponse({"permission_mode": "autoapprove"}),
    ) as call:
        assert client.set_permission_mode("autoapprove") == "autoapprove"

    request = call.call_args.args[0]
    assert request.method == "PUT"
    assert json.loads(request.data) == {"permission_mode": "autoapprove"}

    with patch("skyportal.portal.urlopen") as invalid_call:
        with pytest.raises(PortalError, match="ask.*autoapprove"):
            client.set_permission_mode("everything")
    invalid_call.assert_not_called()
