"""Persistent conversational Skyportal terminal shell."""

import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import prompt as secure_prompt
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from skyportal.portal import ChatTurnResult, CredentialStore, PortalError, SkyportalClient

_PERMISSION_MODES = frozenset({"ask", "autoapprove"})
_AUTOAPPROVE_TYPES = frozenset({"", "bash_command", "plan"})
_APPROVAL_SETTLEMENT_TIMEOUT = 300.0
_APPROVAL_SETTLEMENT_POLL_INTERVAL = 0.25


class _AutoapprovalPolicyConflict(RuntimeError):
    """The shared mode changed between its GET and marked approval POST."""


@dataclass(frozen=True)
class CommandInfo:
    """Help and completion metadata for a slash command."""

    usage: str
    description: str


COMMANDS: Dict[str, CommandInfo] = {
    "/help": CommandInfo("/help", "Show commands and keyboard shortcuts"),
    "/login": CommandInfo("/login [--no-browser]", "Create an API key and connect this CLI"),
    "/token": CommandInfo("/token", "Open the key page and securely paste a key"),
    "/logout": CommandInfo("/logout", "Remove local CLI credentials"),
    "/github-token": CommandInfo(
        "/github-token <set|status|remove>", "Manage the GitHub PAT used for git clone"
    ),
    "/status": CommandInfo("/status", "Show connection, chat, and server status"),
    "/permission": CommandInfo(
        "/permission [autoapprove|ask]",
        "Show or change the shared account approval policy",
    ),
    "/new": CommandInfo("/new", "Start a fresh Skyportal chat"),
    "/resume": CommandInfo(
        "/resume [chat_id] [--verbose]",
        "Reattach to a chat (defaults to your previous one); --verbose replays its history",
    ),
    "/servers": CommandInfo("/servers", "List your Skyportal servers"),
    "/server": CommandInfo(
        "/server <id> [id ...] | auto",
        "Select one or more servers for agent execution",
    ),
    "/clear": CommandInfo("/clear", "Clear the terminal"),
    "/about": CommandInfo("/about", "Show Skyportal CLI information"),
    "/exit": CommandInfo("/exit", "Leave Skyportal"),
    "/quit": CommandInfo("/quit", "Leave Skyportal"),
}


class SkyportalCompleter(Completer):
    """Complete slash commands and their fixed options."""

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            for command, info in COMMANDS.items():
                if command.startswith(text):
                    yield Completion(
                        command,
                        start_position=-len(text),
                        display_meta=info.description,
                    )
            return

        command, remainder = text.split(" ", 1)
        word = remainder.rsplit(" ", 1)[-1]
        options: Sequence[Tuple[str, str]] = ()
        if command == "/login":
            options = (("--no-browser", "print the API-key URL"),)
        elif command == "/server":
            options = (("auto", "let the agent choose a server"),)
        elif command == "/github-token":
            options = (
                ("set", "save a GitHub PAT"),
                ("status", "show whether a PAT is saved"),
                ("remove", "delete the saved PAT"),
            )
        elif command == "/permission":
            options = (
                ("autoapprove", "approve supported pending actions automatically"),
                ("ask", "prompt before each gated action"),
            )
        for value, metadata in options:
            if value.startswith(word.lower()):
                yield Completion(value, start_position=-len(word), display_meta=metadata)


class InteractiveShell:
    """Resilient command center that remains active after request failures."""

    _THINKING_STATUS = (
        "[bold cyan]Skyportal is thinking…[/bold cyan]  "
        "[dim](press Ctrl-C to stop)[/dim]"
    )
    _CONTINUING_STATUS = (
        "[bold cyan]Skyportal is continuing…[/bold cyan]  "
        "[dim](press Ctrl-C to stop)[/dim]"
    )
    _ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    _CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

    PROMPT_STYLE = Style.from_dict(
        {
            "brand": "bold #3b82f6",
            "connected": "#059669",
            "guest": "#d97706",
            "context": "#3b82f6",
            "arrow": "bold",
        }
    )

    def __init__(
        self,
        console: Console,
        client_factory: Callable[[], SkyportalClient],
        session: Optional[Any] = None,
        token_prompt: Optional[Callable[[str], str]] = None,
    ):
        self.console = console
        self.client = client_factory()
        self.running = True
        self.browser_login_started = False
        self.chat_id: Optional[int] = None
        self.last_sequence = 0
        self.selected_server_id: Optional[int] = None
        self.selected_server_ids: List[int] = []
        self.previous_chat_id: Optional[int] = self._load_previous_chat_id()
        self._token_prompt = token_prompt or self._default_token_prompt
        self.session = session or self._create_prompt_session()
        self._handlers: Dict[str, Callable[[List[str]], None]] = {
            "/help": self._cmd_help,
            "/login": self._cmd_login,
            "/token": self._cmd_token,
            "/logout": self._cmd_logout,
            "/github-token": self._cmd_github_token,
            "/status": self._cmd_status,
            "/permission": self._cmd_permission,
            "/new": self._cmd_new,
            "/resume": self._cmd_resume,
            "/servers": self._cmd_servers,
            "/server": self._cmd_server,
            "/clear": self._cmd_clear,
            "/about": self._cmd_about,
            "/exit": self._cmd_exit,
            "/quit": self._cmd_exit,
        }

    @staticmethod
    def _history_path() -> Path:
        path = os.environ.get("SKYPORTAL_HISTORY_PATH")
        return Path(path).expanduser() if path else Path.home() / ".skyportal" / "history"

    @staticmethod
    def _last_chat_path() -> Path:
        path = os.environ.get("SKYPORTAL_LAST_CHAT_PATH")
        return Path(path).expanduser() if path else Path.home() / ".skyportal" / "last_chat"

    def _load_previous_chat_id(self) -> Optional[int]:
        try:
            text = self._last_chat_path().read_text().strip()
        except OSError:
            return None
        try:
            return int(text) if text else None
        except ValueError:
            return None

    def _remember_chat(self, chat_id: Optional[int]) -> None:
        if chat_id is None:
            return
        self.previous_chat_id = chat_id
        try:
            path = self._last_chat_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(chat_id))
            path.chmod(0o600)
        except OSError:
            pass

    def _forget_chat(self) -> None:
        self.previous_chat_id = None
        try:
            self._last_chat_path().unlink()
        except OSError:
            pass

    def _create_prompt_session(self) -> PromptSession:
        history_path = self._history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.touch(mode=0o600, exist_ok=True)
        try:
            history_path.chmod(0o600)
        except OSError:
            pass
        return PromptSession(
            history=FileHistory(str(history_path)),
            completer=SkyportalCompleter(),
            complete_while_typing=True,
            style=self.PROMPT_STYLE,
        )

    @staticmethod
    def _default_token_prompt(message: str) -> str:
        return secure_prompt(message, is_password=True)

    def run(self) -> None:
        """Run until `/exit` or Ctrl-D, preserving the prompt after errors."""
        self._show_onboarding()
        while self.running:
            try:
                line = self.session.prompt(self._prompt_fragments())
            except KeyboardInterrupt:
                self.console.print("[dim]Press Ctrl-D or type /exit to leave Skyportal.[/dim]")
                continue
            except EOFError:
                self.console.print("\n[dim]Session closed. See you in orbit.[/dim]")
                break

            line = line.strip()
            if not line:
                continue
            try:
                if line.startswith("/"):
                    self._dispatch(line)
                else:
                    self._send_prompt(line)
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Cancelled — the prompt is still active.[/yellow]")
            except PortalError as error:
                self._show_portal_error(error)
            except Exception as error:
                self._print_section("Command failed", style="red")
                self.console.print(error)
                self.console.print("[dim]The shell is still running. Try /help or retry.[/dim]\n")

    def _prompt_fragments(self) -> List[Tuple[str, str]]:
        connected = self.client.is_authenticated()
        if connected:
            state_style, state = "class:connected", "connected"
        elif self.browser_login_started:
            state_style, state = "class:guest", "key setup"
        else:
            state_style, state = "class:guest", "guest"
        fragments: List[Tuple[str, str]] = [
            ("class:brand", "skyportal"),
            ("", " ["),
            (state_style, state),
            ("", "]"),
        ]
        if self.chat_id is not None:
            fragments.append(("class:context", " chat#{}".format(self.chat_id)))
        if len(self.selected_server_ids) > 1:
            fragments.append((
                "class:context",
                " servers#{}".format(",".join(str(value) for value in self.selected_server_ids)),
            ))
        elif self.selected_server_id is not None:
            fragments.append(("class:context", " server#{}".format(self.selected_server_id)))
        fragments.append(("class:arrow", "  > "))
        return fragments

    def _print_section(self, title: str, style: str = "#6b7280") -> None:
        """Print a lightweight terminal section divider."""
        self.console.print(Rule(title, characters="─", style=style, align="center"))

    @classmethod
    def _clean_terminal_text(cls, value: Any) -> str:
        """Remove terminal control sequences from server-supplied display text."""
        text = str(value) if value is not None else ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = cls._ANSI_ESCAPE_RE.sub("", text)
        return cls._CONTROL_CHAR_RE.sub("", text)

    @classmethod
    def _bounded_one_line(cls, value: Any, limit: int) -> str:
        text = " ".join(cls._clean_terminal_text(value).splitlines()).strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    @classmethod
    def _bounded_multiline(cls, value: Any, max_lines: int = 24, max_chars: int = 4096) -> str:
        """Keep command results useful while bounding terminal transcript size."""
        cleaned = cls._clean_terminal_text(value).strip()
        if not cleaned:
            return ""
        lines = cleaned.splitlines()
        truncated = len(lines) > max_lines
        visible = lines[:max_lines]
        rendered = "\n".join(visible)
        if len(rendered) > max_chars:
            truncated = True
            rendered = rendered[:max_chars]
        if truncated:
            marker = "… output truncated"
            rendered = rendered.rstrip()
            if len(rendered) + len(marker) + 1 > max_chars:
                rendered = rendered[: max(0, max_chars - len(marker) - 1)].rstrip()
            rendered = (rendered + "\n" + marker).lstrip("\n")
        return rendered

    @staticmethod
    def _sequence(message: Dict[str, Any]) -> Optional[int]:
        try:
            return int(message.get("sequence"))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _message_key(cls, message: Dict[str, Any]) -> Tuple[str, Any]:
        sequence = cls._sequence(message)
        if sequence is not None:
            return ("sequence", sequence)
        # Persisted messages should always have a sequence, but exact-payload
        # identity keeps a malformed/replayed row from scrolling forever.
        try:
            payload = json.dumps(message, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            payload = repr(message)
        return ("payload", payload)

    def _show_onboarding(self) -> None:
        status = (
            "[green]● API connected[/green]"
            if self.client.is_authenticated()
            else "[yellow]○ Guest mode[/yellow] — start with [bold cyan]/login[/bold cyan]"
        )
        self._print_section("Welcome aboard")
        body = Text.from_markup(
            "{}\n\n"
            "[bold cyan]/login[/bold cyan]      Create an API key and connect this terminal\n"
            "[bold cyan]/servers[/bold cyan]    List or select a server\n"
            "[bold cyan]/new[/bold cyan]        Start a fresh agent conversation\n"
            "[bold cyan]/help[/bold cyan]       See every slash command\n\n"
            "Type naturally to talk to the Skyportal Agent.".format(
                status
            )
        )
        self.console.print(body)
        self.console.print()

    def _dispatch(self, line: str) -> None:
        try:
            parts = shlex.split(line)
        except ValueError as error:
            self.console.print("[red]Could not parse command:[/red] {}".format(error))
            return
        if not parts:
            return
        handler = self._handlers.get(parts[0].lower())
        if handler is None:
            self.console.print(
                "[yellow]Unknown command {}.[/yellow] Type [bold cyan]/help[/bold cyan].".format(
                    parts[0]
                )
            )
            return
        handler(parts[1:])

    def _cmd_help(self, args: List[str]) -> None:
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()
        for info in COMMANDS.values():
            if info.usage != "/quit":
                table.add_row(info.usage, info.description)
        self._print_section("Skyportal commands", style="#3b82f6")
        self.console.print(table)
        self.console.print("[dim]Type a message to talk to the agent.[/dim]\n")

    def _cmd_login(self, args: List[str]) -> None:
        if any(argument != "--no-browser" for argument in args):
            self.console.print("[yellow]Usage:[/yellow] /login [--no-browser]")
            return
        self._connect_from_key_page(open_browser="--no-browser" not in args)

    def _cmd_token(self, args: List[str]) -> None:
        if args:
            self.console.print(
                "[yellow]Do not put credentials in command history. Type /token by itself.[/yellow]"
            )
            return
        self._connect_from_key_page(open_browser=True)

    def _connect_from_key_page(self, open_browser: bool) -> None:
        result = self.client.login(open_browser=open_browser)
        self.browser_login_started = True
        url = str(result["verification_url"])
        details = Text()
        details.append("Connect this terminal in four steps:\n\n", style="bold green")
        details.append("1. Open the account API-key page:\n")
        details.append(url, style="bold cyan link {}".format(url))
        details.append(
            "\n\n2. Sign in if prompted.\n"
            "3. Create a key named Skyportal CLI and copy the sk_ value.\n"
            "4. Return here and paste it into the hidden prompt.\n\n"
            "Do not use an agt_ deployment token; those only upload observability data.",
        )
        if open_browser and not result.get("browser_opened"):
            details.append("\nYour browser did not open; use the link above.", style="yellow")
        self._print_section("Connect Skyportal CLI")
        self.console.print(details)
        self.console.print()
        try:
            token = self._token_prompt(
                "Paste API key (input hidden, Enter to cancel): "
            ).strip()
        except (KeyboardInterrupt, EOFError):
            self.console.print("[yellow]API-key entry cancelled.[/yellow]")
            return
        if not token:
            self.console.print(
                "[yellow]Connection paused.[/yellow] Run [bold]/token[/bold] when the key is ready."
            )
            return
        with self.console.status("[cyan]Validating API credential…[/cyan]", spinner="dots"):
            self.client.set_access_token(token)
        self.browser_login_started = False
        self.console.print("[green]✓ Credential validated and saved securely.[/green]")

    def _cmd_logout(self, args: List[str]) -> None:
        self.client.logout()
        self.browser_login_started = False
        self.chat_id = None
        self.last_sequence = 0
        self.selected_server_id = None
        self.selected_server_ids = []
        self._forget_chat()
        self.console.print("[green]✓ Local Skyportal credentials removed.[/green]")

    def _cmd_github_token(self, args: List[str]) -> None:
        subcommand = args[0].lower() if args else ""
        if subcommand == "status":
            self._require_api_connection()
            with self.console.status("[cyan]Checking GitHub token…[/cyan]", spinner="dots"):
                result = self.client.get_github_token_status()
            if result.get("has_token"):
                self.console.print(
                    "[green]✓ GitHub PAT is set:[/green] [bold]{}[/bold]".format(
                        result.get("masked_token", "****")
                    )
                )
            else:
                self.console.print("[yellow]No GitHub PAT saved.[/yellow]")
        elif subcommand == "set":
            self._require_api_connection()
            repo = args[1] if len(args) > 1 else None
            try:
                pat = self._token_prompt(
                    "GitHub Personal Access Token (input hidden, Enter to cancel): "
                ).strip()
            except (KeyboardInterrupt, EOFError):
                self.console.print("[yellow]GitHub PAT entry cancelled.[/yellow]")
                return
            if not pat:
                self.console.print("[yellow]No token entered; GitHub PAT unchanged.[/yellow]")
                return
            with self.console.status("[cyan]Saving GitHub token…[/cyan]", spinner="dots"):
                result = self.client.save_github_token(pat, repo=repo)
            self.console.print(
                "[green]✓ GitHub PAT saved for[/green] [bold]{}[/bold] "
                "(masked: [bold]{}[/bold])".format(
                    result.get("login", "unknown"),
                    result.get("masked_token", "****"),
                )
            )
        elif subcommand == "remove":
            self._require_api_connection()
            with self.console.status("[cyan]Removing GitHub token…[/cyan]", spinner="dots"):
                self.client.delete_github_token()
            self.console.print("[green]✓ GitHub PAT removed.[/green]")
        else:
            self.console.print(
                "[yellow]Usage:[/yellow] /github-token <set [owner/repo] | status | remove>"
            )

    @staticmethod
    def _permission_mode_value(value: Any) -> str:
        mode = value.get("permission_mode") if isinstance(value, dict) else value
        if mode not in _PERMISSION_MODES:
            raise PortalError("Skyportal returned an invalid permission mode")
        return str(mode)

    @staticmethod
    def _approval_id_value(value: Any) -> str:
        return "" if value is None else str(value).strip()

    def _fetch_permission_mode(self) -> str:
        getter = getattr(self.client, "get_permission_mode", None)
        if getter is None:
            raise PortalError("This Skyportal deployment does not expose permission settings")
        return self._permission_mode_value(getter())

    def _permission_mode_for_approval(self) -> str:
        """Read the shared mode, failing closed to an interactive prompt."""
        try:
            return self._fetch_permission_mode()
        except PortalError as error:
            self.console.print(
                "[yellow]Could not verify autoapprove ({}); asking for safety.[/yellow]".format(
                    self._bounded_one_line(error, 160)
                )
            )
            return "ask"

    def _cmd_permission(self, args: List[str]) -> None:
        if len(args) > 1 or (args and args[0].lower() not in _PERMISSION_MODES):
            self.console.print(
                "[yellow]Usage:[/yellow] /permission [autoapprove|ask]"
            )
            return
        self._require_api_connection()
        requested = args[0].lower() if args else None
        with self.console.status("[cyan]Checking approval policy…[/cyan]", spinner="dots"):
            if requested is None:
                mode = self._fetch_permission_mode()
            else:
                setter = getattr(self.client, "set_permission_mode", None)
                if setter is None:
                    raise PortalError(
                        "This Skyportal deployment does not expose permission settings"
                    )
                mode = self._permission_mode_value(setter(requested))

        if mode == "autoapprove":
            self.console.print(
                "[yellow]Permission mode: autoapprove.[/yellow] Supported pending actions "
                "will be submitted automatically; server safety policies still apply."
            )
        else:
            self.console.print(
                "[green]Permission mode: ask.[/green] Gated actions will prompt for a decision."
            )

    def _cmd_status(self, args: List[str]) -> None:
        connected = self.client.is_authenticated()
        remote: Optional[Dict[str, Any]] = None
        remote_error: Optional[str] = None
        permission_mode: Optional[str] = None
        permission_error: Optional[str] = None
        if connected:
            try:
                permission_mode = self._fetch_permission_mode()
            except PortalError as error:
                permission_error = str(error)
        if connected and self.chat_id is not None:
            try:
                detailed_status = getattr(self.client, "get_execution_status", None)
                if detailed_status is None:
                    remote = self.client.chat_status(self.chat_id)
                else:
                    remote = detailed_status(self.chat_id)
            except PortalError as error:
                # Older deployments may not expose /execution-status/. The
                # lightweight status endpoint still gives useful workflow and
                # approval state, so degrade to it for that compatibility case.
                if error.status_code in (404, 405):
                    try:
                        remote = self.client.chat_status(self.chat_id)
                    except PortalError as fallback_error:
                        remote_error = str(fallback_error)
                else:
                    remote_error = str(error)

        rows = Table.grid(padding=(0, 2))
        rows.add_column(style="dim")
        rows.add_column()
        rows.add_row("Portal", self.client.base_url)
        rows.add_row(
            "API",
            "[green]connected[/green]"
            if connected
            else "[yellow]not connected[/yellow]",
        )
        rows.add_row("Agent", "Skyportal Agent")
        if permission_mode is not None:
            permission_style = "yellow" if permission_mode == "autoapprove" else "green"
            rows.add_row("Permission", Text(permission_mode, style=permission_style))
        elif permission_error:
            rows.add_row(
                "Permission",
                Text("ask (unverified; prompting for safety)", style="yellow"),
            )
        else:
            rows.add_row("Permission", "[dim]unknown (not connected)[/dim]")
        rows.add_row(
            "Chat",
            "#{}".format(self.chat_id) if self.chat_id is not None else "[dim]new chat[/dim]",
        )
        rows.add_row(
            "Servers",
            ", ".join(str(value) for value in self.selected_server_ids)
            if self.selected_server_ids
            else "[dim]automatic[/dim]",
        )
        if len(self.selected_server_ids) > 1:
            rows.add_row("Default", str(self.selected_server_id))
        if isinstance(remote, dict):
            workflow_status = self._clean_terminal_text(remote.get("status", "unknown"))
            status_style = {
                "processing": "cyan",
                "awaiting_approval": "yellow",
                "error": "red",
                "cancelled": "yellow",
            }.get(workflow_status, "green")
            rows.add_row("Workflow", Text(workflow_status or "unknown", style=status_style))

            pending = remote.get("pending_approvals", [])
            pending = pending if isinstance(pending, list) else []
            rows.add_row("Approvals", Text(str(len(pending))))
            if pending and isinstance(pending[0], dict):
                detail = (
                    pending[0].get("command")
                    or pending[0].get("reason")
                    or pending[0].get("type")
                    or pending[0].get("approval_id")
                    or "pending decision"
                )
                rows.add_row("Next approval", Text(self._bounded_one_line(detail, 160)))

            live_command = remote.get("live_command_output")
            if isinstance(live_command, dict) and live_command.get("command"):
                # Deliberately do not render raw live output here. Older server
                # versions expose an unredacted Redis preview; persisted tool
                # messages are the safe/authoritative output surface.
                rows.add_row(
                    "Running",
                    Text("$ " + self._bounded_one_line(live_command.get("command"), 160)),
                )

            live_plan = remote.get("live_plan")
            if isinstance(live_plan, dict):
                current = live_plan.get("current_step_index")
                total = live_plan.get("total_steps")
                if isinstance(current, int) and isinstance(total, int) and total > 0:
                    rows.add_row("Plan", Text("step {}/{}".format(current + 1, total)))
        elif remote_error:
            rows.add_row("Remote", Text("unavailable: " + self._bounded_one_line(remote_error, 160), style="yellow"))
        rows.add_row("Credentials", str(CredentialStore.get_path()))
        self._print_section("Session status", style="#3b82f6")
        self.console.print(rows)
        self.console.print()

    def _cmd_new(self, args: List[str]) -> None:
        self.chat_id = None
        self.last_sequence = 0
        self.console.print("[green]✓ Started a fresh Skyportal chat.[/green]")

    def _cmd_resume(self, args: List[str]) -> None:
        verbose = "--verbose" in args
        chat_id_args = [a for a in args if a != "--verbose"]
        if len(chat_id_args) > 1:
            self.console.print("[yellow]Usage:[/yellow] /resume [chat_id] [--verbose]")
            return
        self._require_api_connection()
        if chat_id_args:
            try:
                chat_id = int(chat_id_args[0])
            except ValueError:
                self.console.print("[yellow]Chat ID must be a number.[/yellow]")
                return
        elif self.previous_chat_id is not None:
            chat_id = self.previous_chat_id
        else:
            self.console.print(
                "[yellow]No previous chat to resume.[/yellow] Use [bold]/resume <chat_id>[/bold]."
            )
            return
        try:
            with self.console.status("[cyan]Loading chat…[/cyan]", spinner="dots"):
                self.client.chat_status(chat_id)
                payload = self.client.chat_messages(chat_id, after_sequence=0)
        except PortalError as error:
            if error.status_code in (403, 404):
                self.console.print(
                    "[yellow]Chat #{} was not found or is not yours.[/yellow]".format(chat_id)
                )
                return
            raise
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        self.chat_id = chat_id
        self.last_sequence = max(
            (int(message.get("sequence", 0)) for message in messages), default=0
        )
        self._remember_chat(chat_id)
        # Reloading context (chat_id/last_sequence) doesn't require replaying
        # the transcript — the common case is reattaching to keep answering
        # an in-progress flow, not reviewing history. --verbose opts back
        # into the full render for when the history itself is what's wanted.
        if verbose:
            self._render_history(messages)
        truncated = " Older messages were hidden." if payload.get("has_more") else ""
        self.console.print(
            "[green]✓ Resumed chat #{}.[/green]{} Type a message to continue.".format(
                chat_id, truncated
            )
        )

    def _cmd_servers(self, args: List[str]) -> None:
        self._require_api_connection()
        with self.console.status("[cyan]Contacting Skyportal…[/cyan]", spinner="dots"):
            servers = self._items(self.client.servers())
        if not servers:
            self.console.print("[yellow]No servers found.[/yellow]")
            return
        table = Table(title="Skyportal servers", border_style="blue", header_style="bold cyan")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Status")
        table.add_column("Environment")
        table.add_column("Resources")
        for server in servers:
            status = str(server.get("status", "-"))
            status_style = (
                "green" if status.lower() in ("connected", "running", "ready", "online") else "yellow"
            )
            table.add_row(
                str(server.get("id", "-")),
                str(server.get("name") or server.get("hostname") or "Unnamed"),
                "[{}]{}[/{}]".format(status_style, status, status_style),
                str(server.get("host_type") or server.get("location") or "Custom"),
                "{} vCPU / {} GB RAM / {} GPU".format(
                    server.get("vcpu", 0), server.get("ram", 0), server.get("gpus", 0)
                ),
            )
        self.console.print(table)
        self.console.print(
            "[dim]Select one or more with /server <id> [id ...], "
            "or reset with /server auto.[/dim]"
        )

    def _cmd_server(self, args: List[str]) -> None:
        if not args:
            self.console.print("[yellow]Usage:[/yellow] /server <id> [id ...] | auto")
            return
        if len(args) == 1 and args[0].lower() == "auto":
            if self.chat_id is not None:
                with self.console.status("[cyan]Clearing server scope…[/cyan]", spinner="dots"):
                    self.client.select_chat_servers(self.chat_id, [])
            self.selected_server_id = None
            self.selected_server_ids = []
            self.console.print("[green]✓ Server selection set to automatic.[/green]")
            return
        if any(argument.lower() == "auto" for argument in args):
            self.console.print("[yellow]Use 'auto' by itself, or provide server IDs.[/yellow]")
            return
        self._require_api_connection()
        raw_ids = [part for argument in args for part in argument.split(",") if part]
        try:
            server_ids = list(dict.fromkeys(int(value) for value in raw_ids))
        except ValueError:
            self.console.print("[yellow]Server IDs must be numbers, or use 'auto'.[/yellow]")
            return
        if not server_ids or any(server_id < 1 for server_id in server_ids):
            self.console.print("[yellow]Server IDs must be positive numbers.[/yellow]")
            return
        if self.chat_id is not None:
            if len(server_ids) == 1:
                # Keep the one-host path compatible with older website
                # deployments that only expose /select-server/.
                with self.console.status("[cyan]Selecting server…[/cyan]", spinner="dots"):
                    self.client.select_chat_server(self.chat_id, server_ids[0])
            else:
                with self.console.status("[cyan]Selecting servers…[/cyan]", spinner="dots"):
                    self.client.select_chat_servers(
                        self.chat_id,
                        server_ids,
                        active_server_id=server_ids[0],
                    )
        else:
            servers = self._items(self.client.servers())
            available_ids = {str(server.get("id")) for server in servers}
            missing = [server_id for server_id in server_ids if str(server_id) not in available_ids]
            if missing:
                raise PortalError(
                    "Server{} {} {} not found in your account".format(
                        "s" if len(missing) > 1 else "",
                        ", ".join(str(server_id) for server_id in missing),
                        "were" if len(missing) > 1 else "was",
                    )
                )
        self.selected_server_ids = server_ids
        self.selected_server_id = server_ids[0]
        if len(server_ids) == 1:
            message = "Server {} selected.".format(server_ids[0])
        else:
            message = "Servers {} selected; {} is the default.".format(
                ", ".join(str(server_id) for server_id in server_ids),
                server_ids[0],
            )
        self.console.print("[green]✓ {}[/green]".format(message))

    def _cmd_clear(self, args: List[str]) -> None:
        self.console.clear()
        self.console.print("[bold #3b82f6]S[/bold #3b82f6]  [bold]Skyportal[/bold]")
        self.console.print("[#3b82f6]YOUR AI COMMAND CENTER[/#3b82f6]\n")

    def _cmd_about(self, args: List[str]) -> None:
        self._print_section("Skyportal CLI", style="#3b82f6")
        self.console.print(
            "A persistent command center for the Skyportal Agent and your servers.\n\n"
            "[dim]CLI auth: account API keys from /keys/\n"
            "Prompt history: {}[/dim]\n".format(self._history_path())
        )

    def _cmd_exit(self, args: List[str]) -> None:
        self.running = False
        self.console.print("[bold cyan]See you in orbit.[/bold cyan]")

    def _send_prompt(self, message: str) -> None:
        self._require_api_connection()
        # Grab the chat ID before waiting so a Ctrl-C can cancel the turn
        # server-side, not just stop the shell from listening.
        if len(self.selected_server_ids) > 1:
            chat_id = self.client.begin_chat_turn(
                message,
                chat_id=self.chat_id,
                server_ids=self.selected_server_ids,
                active_server_id=self.selected_server_id,
            )
        else:
            chat_id = self.client.begin_chat_turn(
                message,
                chat_id=self.chat_id,
                server_id=self.selected_server_id,
            )
        self.chat_id = chat_id
        render_state = self._new_render_state()
        try:
            with self.console.status(self._THINKING_STATUS, spinner="dots12") as status:
                turn = self.client.wait_for_chat(
                    chat_id,
                    after_sequence=self.last_sequence,
                    timeout=None,
                    on_progress=lambda messages: self._render_incremental_messages(
                        messages, render_state
                    ),
                    on_status=lambda snapshot: status.update(
                        self._live_status_line(snapshot)
                    ),
                )
        except KeyboardInterrupt:
            self._cancel_active_turn(chat_id)
            return
        self._process_turn(turn, render_state=render_state)

    def _cancel_active_turn(self, chat_id: int) -> None:
        """Stop the running agent turn after a Ctrl-C, then keep the prompt."""
        self.console.print()
        try:
            with self.console.status("[yellow]Stopping the agent…[/yellow]", spinner="dots"):
                self.client.cancel_chat(chat_id, reason="Cancelled from the CLI")
        except PortalError:
            self.console.print("[dim]Nothing to stop; the turn had already finished.[/dim]")
            return
        self.console.print(
            "[yellow]■ Stopped.[/yellow] Chat #{} is still open; type to continue.".format(chat_id)
        )

    @staticmethod
    def _new_render_state() -> Dict[str, Any]:
        return {"seen": set(), "rendered": False}

    def _live_status_line(self, snapshot: Dict[str, Any]) -> Text:
        """Build a safe live activity line from the server's public status."""
        activity = snapshot.get("activity") if isinstance(snapshot, dict) else None
        label = activity.get("label") if isinstance(activity, dict) else None
        if not label:
            label = "Skyportal is thinking…"
        line = Text(self._bounded_one_line(label, 180), style="bold cyan")
        line.append("  Ctrl-C to stop", style="dim")
        return line

    def _render_incremental_messages(
        self,
        messages: List[Dict[str, Any]],
        render_state: Dict[str, Any],
    ) -> bool:
        """Render one persisted-message batch once and advance the local cursor."""
        seen = render_state.setdefault("seen", set())
        fresh: List[Dict[str, Any]] = []
        fresh_keys: List[Tuple[str, Any]] = []
        sequences: List[int] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            key = self._message_key(message)
            if key in seen:
                continue
            fresh.append(message)
            fresh_keys.append(key)
            sequence = self._sequence(message)
            if sequence is not None:
                sequences.append(sequence)

        rendered = self._render_assistant_messages(
            fresh,
            show_section=not bool(render_state.get("rendered")),
        )
        if rendered:
            render_state["rendered"] = True
        # Commit dedupe/cursor state only after the renderer accepted the
        # whole batch. If a callback render raises, PortalClient retains every
        # message in ChatTurnResult so the final pass can safely retry it.
        seen.update(fresh_keys)
        if sequences:
            self.last_sequence = max(self.last_sequence, max(sequences))
        return rendered

    def _approval_was_accepted(
        self,
        chat_id: int,
        approval_id: str,
    ) -> bool:
        """Reconcile an ambiguous approval POST timeout against server state."""
        state = self.client.chat_status(chat_id)
        if not isinstance(state, dict):
            return False
        if state.get("status") != "awaiting_approval":
            return True
        pending = state.get("pending_approvals", [])
        if not isinstance(pending, list) or not pending:
            return False
        pending_ids = {
            str(item.get("approval_id"))
            for item in pending
            if isinstance(item, dict) and item.get("approval_id") is not None
        }
        return approval_id not in pending_ids

    def _submit_approval_with_recovery(
        self,
        chat_id: int,
        approval: Dict[str, Any],
        decision: str,
        *,
        autoapproved: bool = False,
    ) -> bool:
        """Submit a decision; recover when an older synchronous server times out.

        Older deployments execute the resumed workflow inside the approval POST.
        The client can therefore hit its request timeout after the decision was
        durably accepted. A single status reconciliation avoids resubmitting (and
        potentially re-running) that approval. Explicit HTTP failures still fail.
        Returns True when timeout recovery was needed.
        """
        try:
            if autoapproved:
                self.client.submit_chat_approval(
                    chat_id,
                    approval,
                    decision,
                    autoapproved=True,
                )
            else:
                self.client.submit_chat_approval(chat_id, approval, decision)
            return False
        except PortalError as error:
            if (
                autoapproved
                and error.status_code == 409
                and error.code == "autoapproval_policy_conflict"
            ):
                raise _AutoapprovalPolicyConflict from error
            if error.status_code is not None:
                raise
            original_error: BaseException = error
        except TimeoutError as error:
            original_error = error

        try:
            accepted = self._approval_was_accepted(
                chat_id, str(approval.get("approval_id", ""))
            )
        except (PortalError, TimeoutError) as reconciliation_error:
            raise original_error from reconciliation_error
        if not accepted:
            raise original_error
        return True

    def _process_turn(
        self,
        turn: ChatTurnResult,
        render_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Render a completed turn and resolve requested approvals."""
        if render_state is None:
            render_state = self._new_render_state()
        handled_approvals: set[str] = set()
        approval_settlement_deadline: Optional[float] = None
        while True:
            self.chat_id = turn.chat_id
            self._remember_chat(turn.chat_id)
            self._render_incremental_messages(turn.messages, render_state)
            self.last_sequence = max(self.last_sequence, turn.latest_sequence)
            if turn.status == "error":
                raise PortalError(
                    "The Skyportal agent reported an error for chat #{}".format(turn.chat_id)
                )
            if turn.status != "awaiting_approval":
                if not render_state.get("rendered"):
                    # _render_assistant_messages() now surfaces thoughts,
                    # tool-call announcements, and generic tool results (not
                    # just a final text answer), so reaching here means the
                    # turn genuinely produced nothing at all — most often a
                    # chat_id/command typo routing the input somewhere the
                    # active flow never saw it, not an agent failure. Surface
                    # the chat id and status so that's diagnosable instead of
                    # a bare dead-end message.
                    self.console.print(
                        "[dim]Chat #{} finished (status: {}) with no messages to show — "
                        "if you were expecting a reply, check the message actually reached "
                        "this chat (e.g. a leading space or stray character before a /command "
                        "sends it as a new chat message instead).[/dim]".format(
                            turn.chat_id, turn.status
                        )
                    )
                return
            if not turn.pending_approvals:
                raise PortalError(
                    "Chat #{} is awaiting approval without approval details".format(turn.chat_id)
                )

            visible_approval_ids = {
                self._approval_id_value(item.get("approval_id"))
                for item in turn.pending_approvals
                if isinstance(item, dict)
                and self._approval_id_value(item.get("approval_id"))
            }
            if visible_approval_ids & handled_approvals:
                # The approval POST can finish before the status snapshot that
                # still advertises it is replaced. Do not submit a second id
                # from that stale snapshot (or resubmit the first). Poll at a
                # bounded cadence until the handled id disappears or the
                # workflow advances.
                if (
                    approval_settlement_deadline is None
                    or time.monotonic() >= approval_settlement_deadline
                ):
                    raise PortalError(
                        "Skyportal did not clear an approval that was already submitted. "
                        "Use /status before retrying; it will not be submitted twice."
                    )
                try:
                    time.sleep(_APPROVAL_SETTLEMENT_POLL_INTERVAL)
                    turn = self.client.wait_for_chat(
                        turn.chat_id,
                        after_sequence=self.last_sequence,
                        timeout=None,
                        on_progress=lambda messages: self._render_incremental_messages(
                            messages, render_state
                        ),
                    )
                except KeyboardInterrupt:
                    self._cancel_active_turn(turn.chat_id)
                    return
                continue

            remaining = [
                item
                for item in turn.pending_approvals
                if self._approval_id_value(item.get("approval_id"))
                not in handled_approvals
            ]
            if not remaining:
                raise PortalError(
                    "Chat #{} is awaiting approval without a usable approval ID".format(
                        turn.chat_id
                    )
                )
            approval = remaining[0]
            approval_id = self._approval_id_value(approval.get("approval_id"))
            if not approval_id:
                raise PortalError(
                    "Chat #{} returned an approval without an approval ID".format(turn.chat_id)
                )
            description = (
                approval.get("executed_command")
                or approval.get("command")
                or approval.get("reason")
                or json.dumps(approval, indent=2, sort_keys=True)
            )
            self._print_section("Approval requested", style="yellow")
            self.console.print(Text(self._clean_terminal_text(description)))
            approval_type = str(approval.get("type", "") or "")
            autoapprove = (
                approval_type in _AUTOAPPROVE_TYPES
                and self._permission_mode_for_approval() == "autoapprove"
            )
            if autoapprove:
                decision = "approved"
                self.console.print(
                    "[yellow]Auto-approving under the shared account policy.[/yellow]"
                )
            else:
                if approval_type not in _AUTOAPPROVE_TYPES:
                    self.console.print(
                        "[dim]This approval type requires an explicit decision.[/dim]"
                    )
                try:
                    answer = self.session.prompt("Approve this action? [y/N]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    answer = ""
                decision = "approved" if answer in ("y", "yes") else "rejected"
            try:
                while True:
                    try:
                        with self.console.status(
                            "[cyan]Submitting {}…[/cyan] "
                            "[dim](Ctrl-C to stop)[/dim]".format(decision),
                            spinner="dots",
                        ) as status:
                            recovered = self._submit_approval_with_recovery(
                                turn.chat_id,
                                approval,
                                decision,
                                autoapproved=autoapprove,
                            )
                            handled_approvals.add(approval_id)
                            approval_settlement_deadline = (
                                time.monotonic() + _APPROVAL_SETTLEMENT_TIMEOUT
                            )
                            status.update(self._CONTINUING_STATUS)
                            if recovered:
                                self.console.print(
                                    "[dim]Approval was accepted; reattached after the older "
                                    "server's response timed out.[/dim]"
                                )
                            turn = self.client.wait_for_chat(
                                turn.chat_id,
                                after_sequence=self.last_sequence,
                                timeout=None,
                                on_progress=lambda messages: self._render_incremental_messages(
                                    messages, render_state
                                ),
                                on_status=lambda snapshot: status.update(
                                    self._live_status_line(snapshot)
                                ),
                            )
                        break
                    except _AutoapprovalPolicyConflict:
                        autoapprove = False
                        self.console.print(
                            "[yellow]Autoapprove was disabled before this action was "
                            "submitted; an explicit decision is required.[/yellow]"
                        )
                        try:
                            answer = self.session.prompt(
                                "Approve this action? [y/N]: "
                            ).strip().lower()
                        except (KeyboardInterrupt, EOFError):
                            answer = ""
                        decision = "approved" if answer in ("y", "yes") else "rejected"
            except KeyboardInterrupt:
                self._cancel_active_turn(turn.chat_id)
                return

    def _render_assistant_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        show_section: bool = True,
    ) -> bool:
        rendered = False
        for message in sorted(messages, key=lambda item: self._sequence(item) or 0):
            role = message.get("role")
            if role == "assistant":
                line = self._assistant_message_line(message)
                if line is None:
                    continue
                if not rendered:
                    self.console.print()
                    if show_section:
                        self._print_section("[#3b82f6]Skyportal agent[/#3b82f6]")
                self.console.print(line)
                rendered = True
            elif role == "tool":
                line = self._tool_result_line(message)
                if line is None:
                    continue
                if not rendered:
                    self.console.print()
                    if show_section:
                        self._print_section("[#3b82f6]Skyportal agent[/#3b82f6]")
                self.console.print(line)
                rendered = True
        if rendered:
            self.console.print()
        return rendered

    def _assistant_message_line(self, message: Dict[str, Any]) -> Optional[Any]:
        """Render one assistant-role message, distinguishing the three kinds
        the server actually emits (see react_action.py/tool_execution_handler.py
        server-side) instead of showing all three as identical prose. Before
        this, a mid-turn "I'll check the logs" thought and a "run_command(...)"
        tool-call announcement rendered pixel-identical to the turn's real
        final answer — with no way to tell, while reading a transcript,
        which lines were the agent's actual response versus its own internal
        narration of what it was about to do."""
        text = self._message_text(message)
        if not text:
            return None
        metadata = message.get("metadata", {})
        msg_type = metadata.get("type") if isinstance(metadata, dict) else None
        if msg_type == "react_thought":
            line = Text("· ", style="dim italic")
            line.append(self._clean_terminal_text(text), style="dim italic")
            return line
        if msg_type == "react_action":
            line = Text("→ calling ", style="dim")
            line.append(self._clean_terminal_text(text), style="bold")
            return line
        return Markdown(self._clean_terminal_text(text))

    @staticmethod
    def _tool_result_line(message: Dict[str, Any]) -> Optional[Text]:
        """One compact result line per completed tool call. Bash/kube commands
        get the richer '[hostname] $ command -> output' form (the server
        always records which host a run_command call actually executed on
        via terminal_server_hostname, so a multi-server session can tell
        which host a given command ran against); every other tool (add_host,
        search_repo, query_monitoring, ...) falls back to a generic
        'tool_name -> ok/failed' line built from the metadata every tool
        result carries (tool_name/success — see tool_result.py server-side),
        rather than being silently invisible the way it was before."""
        metadata = message.get("metadata", {})
        if not isinstance(metadata, dict):
            return None
        if metadata.get("awaiting_approval"):
            return None
        command = metadata.get("terminal_command")
        if command:
            hostname = InteractiveShell._bounded_one_line(
                metadata.get("terminal_server_hostname") or "unknown host", 120
            )
            command_text = InteractiveShell._bounded_one_line(command, 400)
            success = metadata.get("terminal_success")
            marker = "?" if success is None else ("✓" if success else "✗")
            marker_style = "dim" if success is None else ("green" if success else "red")
            output = InteractiveShell._bounded_multiline(
                metadata.get("terminal_output") or ""
            )
            line = Text()
            line.append("[{}]".format(hostname), style="dim")
            line.append(" ")
            line.append(marker, style=marker_style)
            line.append(" $ ", style="bold")
            line.append(command_text)
            if output:
                for output_line in output.splitlines():
                    line.append("\n  ")
                    line.append(output_line, style="dim")
            return line

        tool_name = metadata.get("tool_name")
        if not tool_name:
            return None
        success = metadata.get("success")
        marker = "?" if success is None else ("✓" if success else "✗")
        marker_style = "dim" if success is None else ("green" if success else "red")
        line = Text("tool", style="dim")
        line.append(" ")
        line.append(marker, style=marker_style)
        line.append(" ")
        line.append(InteractiveShell._bounded_one_line(tool_name, 160), style="bold")
        return line

    def _render_history(self, messages: List[Dict[str, Any]]) -> None:
        ordered = sorted(messages, key=lambda item: self._sequence(item) or 0)
        printable = []
        for m in ordered:
            role = m.get("role")
            if role == "user":
                text = self._message_text(m)
                if text:
                    printable.append(("user", text, None))
            elif role == "assistant":
                text = self._message_text(m)
                if not text:
                    continue
                metadata = m.get("metadata", {})
                msg_type = metadata.get("type") if isinstance(metadata, dict) else None
                printable.append(("assistant", text, msg_type))
        if not printable:
            self.console.print("[dim]This chat has no earlier messages yet.[/dim]")
            return
        self._print_section("earlier conversation", style="#6b7280")
        for role, text, msg_type in printable:
            if role == "user":
                line = Text()
                line.append("you  ", style="bold #f0b429")
                line.append(self._clean_terminal_text(text))
                self.console.print(line)
            elif msg_type == "react_thought":
                line = Text("· ", style="dim italic")
                line.append(self._clean_terminal_text(text), style="dim italic")
                self.console.print(line)
            elif msg_type == "react_action":
                line = Text("→ calling ", style="dim")
                line.append(self._clean_terminal_text(text), style="bold")
                self.console.print(line)
            else:
                self.console.print("[bold #3b82f6]agent[/bold #3b82f6]")
                self.console.print(Markdown(self._clean_terminal_text(text)))
        self.console.print()

    @staticmethod
    def _message_text(message: Dict[str, Any]) -> str:
        content = message.get("content", [])
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        )

    def _require_api_connection(self) -> None:
        if self.client.is_authenticated():
            return
        raise PortalError(
            "Run /login to create and paste an account API key (sk_). "
            "Agent deployment tokens (agt_) cannot authenticate this CLI."
        )

    def _show_portal_error(self, error: PortalError) -> None:
        denied = error.status_code in (401, 403)
        title = "Access denied" if denied else "Skyportal request failed"
        guidance = (
            "Use /login to create or paste a valid account API key."
            if denied
            else "Check /status and retry."
        )
        status = " ({})".format(error.status_code) if error.status_code else ""
        self._print_section(title, style="red")
        self.console.print("{}{}\n\n[dim]{}\nThe command line is still active.[/dim]\n".format(
            error, status, guidance
        ))

    @staticmethod
    def _items(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("data", "items", "servers"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
