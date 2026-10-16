"""The shell answers a scope card with y/N, and asks which namespace when it must (skyportal-website#3724)."""

from io import StringIO

from rich.console import Console

from skyportalai.shell.interactive import InteractiveShell
from skyportalai.shell.portal import ChatTurnResult

LABEL = "Add namespace e2e on kind-smoke to this chat and run `kubectl logs -f slow-logger -n e2e`"


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
        return [{"id": 12, "hostname": "kind-smoke", "target_kind": "kubernetes", "namespaces": ["e2e", "kube-system"]}]

    def submit_chat_approval(self, chat_id, approval, decision, *, autoapproved=False, rejection_reason=None,
                             namespace=None):
        self.submit_calls.append((approval["type"], decision, autoapproved, namespace))
        return {"success": True}

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=300, on_progress=None, on_status=None):
        return _turn("idle")


def _turn(status, approvals=()):
    return ChatTurnResult(
        chat_id=42, status=status, messages=[], pending_approvals=list(approvals), latest_sequence=0,
    )


def _scope_approval(**scope):
    return {
        "approval_id": "s1", "type": "scope", "command": "kubectl logs -f slow-logger -n e2e",
        "scope": {"kind": "namespace", "server_id": 12, "hostname": "kind-smoke", "namespace": "e2e",
                  "namespace_choices": [], "combined": False, "label": LABEL, **scope},
    }


def _shell(client, tmp_path, monkeypatch, answers=(), picks=()):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    console = Console(file=StringIO(), force_terminal=False, width=200)
    session = _Session(answers)
    confirm = _Session(picks)
    shell = InteractiveShell(
        console=console, client_factory=lambda: client, session=session, token_prompt=lambda _p: "",
        confirm_prompt=confirm.prompt,
    )
    return shell, console, confirm


def test_y_adds_the_target_and_runs(tmp_path, monkeypatch):
    client = _Client()
    shell, console, _ = _shell(client, tmp_path, monkeypatch, answers=["y"])

    shell._process_turn(_turn("awaiting_approval", [_scope_approval(combined=True)]))

    output = console.file.getvalue()
    assert LABEL in output
    assert "Approving also approves this command." in output
    assert client.submit_calls == [("scope", "approved", False, None)]


def test_autoapprove_still_asks(tmp_path, monkeypatch):
    client = _Client(mode="autoapprove")
    shell, console, _ = _shell(client, tmp_path, monkeypatch, answers=["n"])

    shell._process_turn(_turn("awaiting_approval", [_scope_approval()]))

    assert "requires an explicit decision" in console.file.getvalue()
    assert client.submit_calls == [("scope", "rejected", False, None)]
    assert shell.selected_namespaces == {}


def test_an_approved_namespace_joins_the_shell_scope(tmp_path, monkeypatch):
    client = _Client()
    shell, _, _ = _shell(client, tmp_path, monkeypatch, answers=["y"])
    shell.selected_server_ids, shell.selected_server_id = [12], 12
    shell.selected_namespaces = {12: ["kube-system"]}

    shell._process_turn(_turn("awaiting_approval", [_scope_approval()]))

    assert shell.selected_namespaces == {12: ["kube-system", "e2e"]}


def test_an_approved_host_joins_the_shell_scope(tmp_path, monkeypatch):
    client = _Client()
    client.servers = lambda: [{"id": 7, "hostname": "gpu-box", "target_kind": "ssh"}]
    shell, _, _ = _shell(client, tmp_path, monkeypatch, answers=["y"])
    shell.selected_server_ids, shell.selected_server_names, shell.selected_server_id = [12], ["kind-smoke"], 12

    shell._process_turn(_turn("awaiting_approval", [
        _scope_approval(kind="server", server_id=7, hostname="gpu-box", namespace=None),
    ]))

    assert shell.selected_server_ids == [12, 7]
    assert shell._server_name(7) == "gpu-box"
    assert shell.selected_server_id == 12


def test_a_cluster_with_no_namespace_picked_asks_which(tmp_path, monkeypatch):
    client = _Client()
    shell, console, confirm = _shell(
        client, tmp_path, monkeypatch, answers=["y"], picks=["default", "kube-system"],
    )

    shell._process_turn(_turn("awaiting_approval", [
        _scope_approval(namespace=None, namespace_choices=["e2e", "kube-system"]),
    ]))

    assert "default is not one of: e2e, kube-system" in console.file.getvalue()
    assert len(confirm.prompts) == 2
    assert "kind-smoke" in confirm.prompts[0]
    assert client.submit_calls == [("scope", "approved", False, "kube-system")]
    assert shell.selected_namespaces == {12: ["kube-system"]}


def test_no_namespace_picked_declines(tmp_path, monkeypatch):
    client = _Client()
    shell, _, _ = _shell(client, tmp_path, monkeypatch, answers=["y"], picks=[""])

    shell._process_turn(_turn("awaiting_approval", [
        _scope_approval(namespace=None, namespace_choices=["e2e"]),
    ]))

    assert client.submit_calls == [("scope", "rejected", False, None)]
