"""Tests for InteractiveShell rendering tool-role messages with the host
they actually ran on. Before this, _render_assistant_messages only rendered
role == "assistant" messages, silently dropping the server's own
terminal_server_hostname/terminal_command/terminal_output metadata on
role == "tool" messages — a multi-server session gave no way to tell which
host a given command ran against."""

from io import StringIO

from rich.console import Console

from skyportal.shell import InteractiveShell


def _tool_message(
    *,
    command="whoami",
    output="root",
    hostname="test-host",
    success=True,
    awaiting_approval=False,
    sequence=1,
):
    return {
        "role": "tool",
        "sequence": sequence,
        "content": [{"type": "text", "text": output}],
        "metadata": {
            "terminal_command": command,
            "terminal_output": output,
            "terminal_server_hostname": hostname,
            "terminal_success": success,
            "awaiting_approval": awaiting_approval,
        },
    }


def _assistant_message(text, sequence=0):
    return {
        "role": "assistant",
        "sequence": sequence,
        "content": [{"type": "text", "text": text}],
        "metadata": {},
    }


def _console():
    return Console(file=StringIO(), force_terminal=False, width=200)


class TestToolResultLine:
    def test_includes_hostname_command_and_output(self):
        line = InteractiveShell._tool_result_line(_tool_message())
        assert line is not None
        plain = line.plain
        assert "test-host" in plain
        assert "whoami" in plain
        assert "root" in plain

    def test_none_for_awaiting_approval(self):
        """A command still pending approval hasn't run anywhere yet — no
        host to report."""
        line = InteractiveShell._tool_result_line(_tool_message(awaiting_approval=True))
        assert line is None

    def test_none_when_no_terminal_command(self):
        message = {"role": "tool", "metadata": {}}
        assert InteractiveShell._tool_result_line(message) is None

    def test_falls_back_to_unknown_host_when_hostname_missing(self):
        line = InteractiveShell._tool_result_line(_tool_message(hostname=None))
        assert "unknown host" in line.plain

    def test_failed_command_shown_distinctly_from_success(self):
        ok_line = InteractiveShell._tool_result_line(_tool_message(success=True))
        fail_line = InteractiveShell._tool_result_line(_tool_message(success=False))
        assert ok_line.plain != fail_line.plain

    def test_long_output_is_truncated(self):
        long_output = "x" * 500
        line = InteractiveShell._tool_result_line(_tool_message(output=long_output))
        assert len(line.plain) < 500


class TestRenderAssistantMessagesIncludesToolLines:
    def _shell(self):
        console = _console()
        shell = InteractiveShell(
            console=console,
            client_factory=lambda: object(),
            session=object(),
            token_prompt=lambda _prompt: "",
        )
        return shell, console

    def test_tool_hostname_line_appears_in_output(self):
        shell, console = self._shell()
        messages = [
            _assistant_message("I'll run whoami.", sequence=0),
            _tool_message(sequence=1),
        ]
        rendered = shell._render_assistant_messages(messages)
        assert rendered is True
        text = console.file.getvalue()
        assert "test-host" in text
        assert "whoami" in text
        assert "root" in text

    def test_multi_server_plan_shows_each_host(self):
        shell, console = self._shell()
        messages = [
            _tool_message(command="whoami", output="root", hostname="web-01", sequence=1),
            _tool_message(command="pwd", output="/srv/app", hostname="db-02", sequence=2),
        ]
        shell._render_assistant_messages(messages)
        text = console.file.getvalue()
        assert "web-01" in text
        assert "db-02" in text

    def test_pending_approval_tool_message_not_rendered_as_result(self):
        shell, console = self._shell()
        messages = [_tool_message(awaiting_approval=True)]
        rendered = shell._render_assistant_messages(messages)
        assert rendered is False
        assert console.file.getvalue().strip() == ""
