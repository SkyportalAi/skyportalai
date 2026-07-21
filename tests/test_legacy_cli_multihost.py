"""Click CLI coverage for first-turn multi-server scope."""

from click.testing import CliRunner

from skyportal.cli import main
from skyportal.portal import ChatTurnResult


class FakeClient:
    def __init__(self):
        self.calls = []

    def run_chat_turn(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return ChatTurnResult(42, "idle", [], [], 0)

    @staticmethod
    def assistant_text(_messages):
        return ""


def test_ask_repeated_server_options_scope_the_first_turn(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("skyportal.cli._portal_client", lambda: client)

    result = CliRunner().invoke(
        main,
        ["ask", "compare hosts", "--server", "7", "--server", "9", "--server", "7"],
    )

    assert result.exit_code == 0
    assert client.calls == [
        (
            "compare hosts",
            {"server_ids": [7, 9], "active_server_id": 7},
        )
    ]


def test_ask_one_server_preserves_the_legacy_singular_request(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("skyportal.cli._portal_client", lambda: client)

    result = CliRunner().invoke(main, ["ask", "check host", "--server", "7"])

    assert result.exit_code == 0
    assert client.calls == [("check host", {"server_id": 7})]
