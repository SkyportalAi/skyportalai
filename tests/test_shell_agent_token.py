"""The shell hands an agent token minted in chat to kubectl, never to disk or the transcript (#3575)."""

from io import StringIO

import pytest
from rich.console import Console

from skyportalai.shell import agent_setup
from skyportalai.shell.interactive import InteractiveShell
from skyportalai.shell.portal import ChatTurnResult, PortalError


def _delivery(sequence=5, cluster="prod"):
    return {
        "role": "assistant",
        "sequence": sequence,
        "content": [{"type": "text", "text": "Agent token created for cluster **prod**."}],
        "metadata": {
            "type": "agent_token_delivery", "handle": "h-1", "cluster_name": cluster,
            "server_id": 2, "token_id": 3, "expires_at": None,
        },
    }


class _Session:
    def prompt(self, _message):
        raise AssertionError("the history-backed session must not be used for these prompts")


class Client:
    base_url = "https://app.skyportal.ai"

    def __init__(self, error=None):
        self.collected = []
        self.error = error

    def is_authenticated(self):
        return True

    def collect_agent_token(self, handle):
        self.collected.append(handle)
        if self.error:
            raise self.error
        return {"key": "agt_SECRET", "token_id": 3, "cluster": "prod", "expires_at": None}


def _shell(tmp_path, monkeypatch, client, answers):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    replies = iter(answers)
    prompts = []

    def confirm(message):
        prompts.append(message)
        return next(replies)

    shell = InteractiveShell(
        console=Console(file=StringIO(), force_terminal=False, width=200),
        client_factory=lambda: client,
        session=_Session(),
        token_prompt=lambda _prompt: "",
        confirm_prompt=confirm,
    )
    return shell, prompts


def _turn(messages=None, status="idle"):
    return ChatTurnResult(
        chat_id=42, status=status, messages=messages or [_delivery()], pending_approvals=[], latest_sequence=5,
    )


def test_confirmed_secret_uses_the_token_and_never_prints_it(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent_setup, "current_context", lambda: "kind-x")
    monkeypatch.setattr(agent_setup, "create_secret", lambda ctx, token: seen.update(ctx=ctx, token=token))
    monkeypatch.setattr(agent_setup, "helm_install", lambda ctx, name: seen.update(helm=(ctx, name)))
    client = Client()
    shell, prompts = _shell(tmp_path, monkeypatch, client, ["y", "n"])

    shell._process_turn(_turn())

    output = shell.console.file.getvalue()
    assert client.collected == ["h-1"]
    assert seen == {"ctx": "kind-x", "token": "agt_SECRET"}
    assert "agt_SECRET" not in output
    assert "kind-x" in prompts[0]
    assert "privileged" in prompts[0]
    assert "helm" not in seen


def test_declined_prints_the_token_once_with_the_manual_command(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_setup, "current_context", lambda: "kind-x")
    monkeypatch.setattr(agent_setup, "create_secret", lambda ctx, token: (_ for _ in ()).throw(AssertionError))
    shell, _ = _shell(tmp_path, monkeypatch, Client(), ["n"])

    shell._process_turn(_turn())

    output = shell.console.file.getvalue()
    assert output.count("agt_SECRET") == 1
    assert "--from-file=SKYPORTALAI_AGENT_TOKEN=/dev/stdin" in output
    assert "won't be shown again" in output


def test_without_kubectl_the_token_is_printed_once(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_setup, "current_context", lambda: None)
    shell, prompts = _shell(tmp_path, monkeypatch, Client(), [])

    shell._process_turn(_turn())

    assert shell.console.file.getvalue().count("agt_SECRET") == 1
    assert prompts == []


def test_a_failed_kubectl_step_falls_back_to_printing(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_setup, "current_context", lambda: "kind-x")
    monkeypatch.setattr(agent_setup, "create_secret", lambda ctx, token: "namespaces is forbidden")
    shell, _ = _shell(tmp_path, monkeypatch, Client(), ["y"])

    shell._process_turn(_turn())

    output = shell.console.file.getvalue()
    assert "namespaces is forbidden" in output
    assert output.count("agt_SECRET") == 1


def test_helm_after_a_second_confirmation(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent_setup, "current_context", lambda: "kind-x")
    monkeypatch.setattr(agent_setup, "create_secret", lambda ctx, token: None)
    monkeypatch.setattr(agent_setup, "helm_install", lambda ctx, name: seen.update(helm=(ctx, name)))
    shell, prompts = _shell(tmp_path, monkeypatch, Client(), ["y", "y"])

    shell._process_turn(_turn())

    assert seen["helm"] == ("kind-x", "prod")
    assert len(prompts) == 2


def test_a_collect_failure_is_reported_without_crashing(tmp_path, monkeypatch):
    client = Client(error=PortalError("This token was already collected or has expired.", status_code=404))
    shell, _ = _shell(tmp_path, monkeypatch, client, [])

    shell._process_turn(_turn())

    output = shell.console.file.getvalue()
    assert "Agents page" in output
    assert "agt_" not in output


def test_a_turn_that_errors_still_delivers_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_setup, "current_context", lambda: None)
    client = Client()
    shell, _ = _shell(tmp_path, monkeypatch, client, [])

    with pytest.raises(PortalError):
        shell._process_turn(_turn(status="error"))

    assert client.collected == ["h-1"]


def test_helm_gets_the_cluster_name_not_its_display_label(tmp_path, monkeypatch):
    seen = {}
    name = "gpu-" + "x" * 130
    monkeypatch.setattr(agent_setup, "current_context", lambda: "kind-x")
    monkeypatch.setattr(agent_setup, "create_secret", lambda ctx, token: None)
    monkeypatch.setattr(agent_setup, "helm_install", lambda ctx, cluster_name: seen.update(helm=cluster_name))
    shell, _ = _shell(tmp_path, monkeypatch, Client(), ["y", "y"])

    shell._process_turn(_turn([_delivery(cluster=name)]))

    assert seen["helm"] == name


def test_a_failed_approval_still_delivers_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_setup, "current_context", lambda: None)
    client = Client()
    shell, _ = _shell(tmp_path, monkeypatch, client, [])

    with pytest.raises(PortalError, match="without approval details"):
        shell._process_turn(_turn(status="awaiting_approval"))

    assert client.collected == ["h-1"]


class _InterruptedClient(Client):
    def __init__(self):
        super().__init__()
        self.cancelled = []

    def begin_chat_turn(self, message, **kwargs):
        return 42

    def wait_for_chat(self, chat_id, on_progress=None, **kwargs):
        on_progress([_delivery()])
        raise KeyboardInterrupt

    def cancel_chat(self, chat_id, reason=""):
        self.cancelled.append((chat_id, list(self.collected)))


def test_ctrl_c_while_waiting_cancels_then_delivers_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_setup, "current_context", lambda: None)
    client = _InterruptedClient()
    shell, _ = _shell(tmp_path, monkeypatch, client, [])

    shell._send_prompt("connect my cluster")

    assert client.cancelled == [(42, [])]
    assert client.collected == ["h-1"]


def test_resume_replay_never_collects(tmp_path, monkeypatch):
    client = Client()
    shell, _ = _shell(tmp_path, monkeypatch, client, [])

    shell._render_history([_delivery()])

    assert client.collected == []
