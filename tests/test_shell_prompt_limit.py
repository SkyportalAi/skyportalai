"""Tests for the CLI's explicit prompt-limit branch (skyportal-website#3120).

When a turn is rejected for quota, the backend persists an assistant message
whose metadata carries type=prompt_limit_reached plus the quota payload and
upgrade suggestion (skyportal-website#3119). Before this, that notice rendered
through the generic Markdown prose path — no usage numbers and no way to act
on it from the terminal."""

from io import StringIO

from rich.console import Console

from skyportalai.shell.interactive import InteractiveShell
from skyportalai.shell.portal import ChatTurnResult, SkyportalClient

_NOTICE_TEXT = (
    "Monthly prompt limit reached (150 prompts). Upgrade to Pro for more prompts/month."
)
_UPGRADE = {"type": "upgrade_to_pro", "message": "Upgrade to Pro for more prompts/month."}


def _prompt_limit_message(*, text=_NOTICE_TEXT, quota=..., upgrade_suggestion=..., sequence=0):
    """Mirror the message agent_service.py persists on a rate-limited turn:
    metadata always carries the quota dict and an upgrade_suggestion key
    (None when the backend has no suggestion)."""
    metadata = {"type": "prompt_limit_reached"}
    if quota is ...:
        quota = {"tier_used": 150, "tier_limit": 150, "purchased_remaining": 0}
    if quota is not None:
        metadata["quota"] = quota
    metadata["upgrade_suggestion"] = dict(_UPGRADE) if upgrade_suggestion is ... else upgrade_suggestion
    return {
        "role": "assistant",
        "sequence": sequence,
        "content": [{"type": "text", "text": text}] if text else [],
        "metadata": metadata,
    }


def _shell(client=None):
    console = Console(file=StringIO(), force_terminal=False, width=200)
    shell = InteractiveShell(
        console=console,
        client_factory=lambda: client if client is not None else object(),
        session=object(),
        token_prompt=lambda _prompt: "",
    )
    return shell, console


def _render(message, client=None):
    shell, console = _shell(client)
    rendered = shell._render_assistant_messages([message])
    return rendered, console.file.getvalue()


class TestPromptLimitNotice:
    def test_usage_is_rendered_from_quota_payload(self):
        rendered, out = _render(_prompt_limit_message())

        assert rendered is True
        assert "Prompt limit reached" in out
        assert "You've used 150 of 150 prompts this month." in out

    def test_upgrade_suggestion_is_rendered_from_metadata(self):
        """The suggestion must come from the metadata payload, not rely on
        the backend having embedded it in the notice's content string."""
        _, out = _render(
            _prompt_limit_message(text="Monthly prompt limit reached (150 prompts).")
        )

        assert "Upgrade to Pro for more prompts/month." in out

    def test_action_url_is_present_without_an_upgrade_suggestion(self):
        """The notice must never be a dead end (#3120) — even with no
        suggestion from the backend, point at the billing page."""
        _, out = _render(_prompt_limit_message(upgrade_suggestion=None))

        assert "https://app.skyportal.ai/billing/?upgrade=true" in out

    def test_action_url_follows_a_custom_deployment_base_url(self):
        client = SkyportalClient("https://skyportal.example/")

        _, out = _render(_prompt_limit_message(), client=client)

        assert "https://skyportal.example/billing/?upgrade=true" in out

    def test_action_url_falls_back_to_production_without_client_support(self):
        """Shell tests (and defensive paths) build the shell around a bare
        object with no billing_url(); the notice still links somewhere real."""
        _, out = _render(_prompt_limit_message())

        assert "https://app.skyportal.ai/billing/?upgrade=true" in out

    def test_missing_quota_payload_falls_back_to_notice_text(self):
        rendered, out = _render(_prompt_limit_message(quota=None))

        assert rendered is True
        assert "Monthly prompt limit reached (150 prompts)." in out
        assert "You've used" not in out
        # Still not a dead end even when the quota payload is malformed.
        assert "https://app.skyportal.ai/billing/?upgrade=true" in out

    def test_renders_from_metadata_when_content_is_empty(self):
        """The branch must fire on metadata type alone — an empty content
        string must not fall into the generic 'nothing to render' path."""
        rendered, out = _render(_prompt_limit_message(text=""))

        assert rendered is True
        assert "You've used 150 of 150 prompts this month." in out
        assert "https://app.skyportal.ai/billing/?upgrade=true" in out

    def test_history_replay_renders_the_quota_notice(self):
        shell, console = _shell()

        shell._render_history([_prompt_limit_message()])
        out = console.file.getvalue()

        assert "You've used 150 of 150 prompts this month." in out
        assert "https://app.skyportal.ai/billing/?upgrade=true" in out


def test_rate_limited_turn_shows_the_notice_not_the_fallback(tmp_path, monkeypatch):
    """End to end through _process_turn (#3120's headline requirement): a
    rate-limited turn ends with the pre-turn status and, before this branch,
    an empty-content notice fell into the misleading 'finished with no
    messages to show — check for a typo' fallback."""
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    shell, console = _shell()
    turn = ChatTurnResult(42, "idle", [_prompt_limit_message(text="")], [], 1)

    shell._process_turn(turn)
    out = console.file.getvalue()

    assert "You've used 150 of 150 prompts this month." in out
    assert "https://app.skyportal.ai/billing/?upgrade=true" in out
    assert "no messages to show" not in out
