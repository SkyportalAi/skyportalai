"""First-turn multi-server scope for `skyportalai ask`.

Ported from the standalone Click CLI in 0.2.0. Driven through the real Typer
entry point so it also covers command registration, not just the callback.
"""

from typer.testing import CliRunner

from skyportalai.cli.main import app
from skyportalai.shell.portal import ChatTurnResult


class FakeClient:
    def __init__(self):
        self.calls = []

    def run_chat_turn(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return ChatTurnResult(42, "idle", [], [], 0)

    @staticmethod
    def assistant_text(_messages):
        return ""


def _client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("skyportalai.cli.shell_commands._portal_client", lambda: client)
    return client


def test_ask_repeated_server_options_scope_the_first_turn(monkeypatch):
    client = _client(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["ask", "compare hosts", "--server", "7", "--server", "9", "--server", "7"],
    )

    assert result.exit_code == 0, result.output
    assert client.calls == [("compare hosts", {"server_ids": [7, 9], "active_server_id": 7})]


def test_ask_one_server_preserves_the_legacy_singular_request(monkeypatch):
    client = _client(monkeypatch)

    result = CliRunner().invoke(app, ["ask", "check host", "--server", "7"])

    assert result.exit_code == 0, result.output
    assert client.calls == [("check host", {"server_id": 7})]


def test_ask_without_server_sends_an_unscoped_turn(monkeypatch):
    client = _client(monkeypatch)

    result = CliRunner().invoke(app, ["ask", "status?"])

    assert result.exit_code == 0, result.output
    assert client.calls == [("status?", {})]
