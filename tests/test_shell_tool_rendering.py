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


def _generic_tool_message(*, tool_name="add_host", success=True, sequence=1):
    """A non-bash tool result — no terminal_* fields, just the generic
    tool_name/success metadata every tool result carries (see
    tool_result.py server-side)."""
    return {
        "role": "tool",
        "sequence": sequence,
        "content": [{"type": "text", "text": "done"}],
        "metadata": {
            "tool_name": tool_name,
            "tool_args": {"hostname": "my-host"},
            "success": success,
        },
    }


def _react_thought_message(text="Let me check the logs.", sequence=0):
    return {
        "role": "assistant",
        "sequence": sequence,
        "content": [{"type": "text", "text": text}],
        "metadata": {"type": "react_thought", "reasoning": text},
    }


def _react_action_message(text="run_command(command=ls -la)", sequence=1):
    return {
        "role": "assistant",
        "sequence": sequence,
        "content": [{"type": "text", "text": text}],
        "metadata": {"type": "react_action", "tool_name": "run_command", "tool_params": {}},
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
        long_output = "x" * 6000
        line = InteractiveShell._tool_result_line(_tool_message(output=long_output))
        assert len(line.plain) < len(long_output)
        assert "output truncated" in line.plain

    def test_multiline_inventory_output_remains_useful(self):
        output = "\n".join("field-{}: value".format(index) for index in range(16))

        line = InteractiveShell._tool_result_line(_tool_message(output=output))

        assert line is not None
        assert "field-0: value" in line.plain
        assert "field-15: value" in line.plain
        assert "output truncated" not in line.plain

    def test_server_text_is_literal_and_terminal_controls_are_removed(self):
        line = InteractiveShell._tool_result_line(
            _tool_message(
                hostname="[red]host[/red]",
                command="printf '[bold]not markup[/bold]'",
                output="\x1b[31m[green]literal[/green]\x1b[0m",
            )
        )

        assert line is not None
        assert "[red]host[/red]" in line.plain
        assert "[bold]not markup[/bold]" in line.plain
        assert "[green]literal[/green]" in line.plain
        assert "\x1b" not in line.plain


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


class TestGenericToolResultLine:
    """Non-bash tools (add_host, search_repo, query_monitoring, ...) have no
    terminal_* fields — before this, _tool_result_line() only recognized
    terminal_command, so every non-bash tool call was silently invisible in
    the transcript (a real contributor to turns that appeared to complete
    "without a text response" even though a tool genuinely ran)."""

    def test_generic_tool_result_shows_name_and_success(self):
        line = InteractiveShell._tool_result_line(_generic_tool_message())
        assert line is not None
        assert "add_host" in line.plain

    def test_generic_tool_failure_shown_distinctly(self):
        ok_line = InteractiveShell._tool_result_line(_generic_tool_message(success=True))
        fail_line = InteractiveShell._tool_result_line(_generic_tool_message(success=False))
        assert ok_line.plain != fail_line.plain

    def test_no_tool_name_and_no_terminal_command_is_invisible(self):
        message = {"role": "tool", "metadata": {"success": True}}
        assert InteractiveShell._tool_result_line(message) is None

    def test_generic_tool_result_appears_in_rendered_output(self):
        console = _console()
        shell = InteractiveShell(
            console=console,
            client_factory=lambda: object(),
            session=object(),
            token_prompt=lambda _prompt: "",
        )
        rendered = shell._render_assistant_messages([_generic_tool_message()])
        assert rendered is True
        assert "add_host" in console.file.getvalue()


class TestAssistantMessageLine:
    """react_thought/react_action messages carry real, human-readable
    content (see react_action.py server-side) but previously rendered
    pixel-identical to the turn's actual final answer — no way to tell
    mid-turn narration apart from a real response while reading a
    transcript."""

    def _shell(self):
        console = _console()
        shell = InteractiveShell(
            console=console,
            client_factory=lambda: object(),
            session=object(),
            token_prompt=lambda _prompt: "",
        )
        return shell, console

    def test_react_thought_rendered_distinctly_from_plain_answer(self):
        shell, console = self._shell()
        shell._render_assistant_messages([_react_thought_message("Let me check the logs.")])
        text = console.file.getvalue()
        assert "Let me check the logs." in text

    def test_react_action_rendered_distinctly_from_plain_answer(self):
        shell, console = self._shell()
        shell._render_assistant_messages([_react_action_message("run_command(command=ls -la)")])
        text = console.file.getvalue()
        assert "run_command(command=ls -la)" in text
        assert "calling" in text

    def test_plain_final_answer_unaffected(self):
        shell, console = self._shell()
        shell._render_assistant_messages([_assistant_message("Here's your answer.")])
        text = console.file.getvalue()
        assert "Here's your answer." in text
        assert "calling" not in text

    def test_thought_and_action_and_answer_all_distinguishable_in_one_turn(self):
        shell, console = self._shell()
        messages = [
            _react_thought_message("I'll check disk usage.", sequence=0),
            _react_action_message("run_command(command=df -h)", sequence=1),
            _tool_message(command="df -h", output="/dev/sda1 50%", sequence=2),
            _assistant_message("Disk usage is at 50%.", sequence=3),
        ]
        rendered = shell._render_assistant_messages(messages)
        assert rendered is True
        text = console.file.getvalue()
        assert "I'll check disk usage." in text
        assert "calling" in text
        assert "df -h" in text
        assert "Disk usage is at 50%." in text


class TestEmptyTurnMessage:
    """A turn producing genuinely zero renderable messages (not even a
    thought/action/tool-result) is a much rarer case now that those all
    render — most often a routing issue (e.g. a stray character before a
    /command), not an agent failure, so the fallback message should say
    that rather than a bare dead end."""

    def test_empty_turn_message_names_chat_and_status(self):
        from skyportal.portal import ChatTurnResult

        console = _console()
        shell = InteractiveShell(
            console=console,
            client_factory=lambda: object(),
            session=object(),
            token_prompt=lambda _prompt: "",
        )
        turn = ChatTurnResult(
            chat_id=42, status="idle", messages=[], latest_sequence=0, pending_approvals=[],
        )
        shell._process_turn(turn)
        text = console.file.getvalue()
        assert "#42" in text
        assert "idle" in text
