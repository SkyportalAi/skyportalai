"""The shell connects this machine in a trusted folder and binds its chats to it (skyportal-website#3665)."""

import http.client
import os
import signal
import ssl
import threading
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from skyportalai.cli import shell_commands
from skyportalai.shell.interactive import InteractiveShell, _HeldInput
from skyportalai.shell.local_target import LocalTarget, TrustStore
from skyportalai.shell.portal import ChatTurnResult, PortalError


class _Session:
    def prompt(self, _message):
        raise EOFError


class _Target:
    """A LocalTarget that connects without a network."""

    made = []

    def __init__(self, client, folder):
        self.client = client
        self.folder = folder
        self.server_id = None
        self.hostname = "altaf-laptop"
        self.instance = "session-1"
        self.closed = False
        self.fail = False
        _Target.made.append(self)

    @property
    def connected(self):
        return self.server_id is not None and not self.closed

    @property
    def session(self):
        return {"server_id": self.server_id, "instance": self.instance}

    def describe(self):
        return "altaf-laptop · o/r @ main"

    def connect(self):
        if self.fail:
            raise PortalError("offline")
        self.server_id = 5

    def close(self):
        self.closed = True


class _Client:
    base_url = "https://app.skyportal.ai"

    def __init__(self):
        self.turns = []

    def is_authenticated(self):
        return True

    def begin_chat_turn(self, message, chat_id=None, server_id=None, **scope):
        self.turns.append({"message": message, "chat_id": chat_id, "server_id": server_id, **scope})
        return chat_id or 42

    def wait_for_chat(self, chat_id, after_sequence=0, timeout=None, on_progress=None, on_status=None):
        return ChatTurnResult(chat_id, "idle", [], [], after_sequence)

    def servers(self):
        return [{"id": 5, "hostname": "altaf-laptop", "target_kind": "local"}, {"id": 9, "hostname": "web-01"}]

    def select_chat_server(self, chat_id, server_id):
        return {}


def _shell(tmp_path, monkeypatch, *, answers=(), factory=_Target, trusted=False):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    monkeypatch.chdir(tmp_path)
    store = TrustStore(tmp_path / "trusted.json")
    if trusted:
        store.trust(tmp_path)
    replies = iter(answers)
    asked = []

    def confirm(message):
        asked.append(message)
        return next(replies)

    client = _Client()
    shell = InteractiveShell(
        console=Console(file=StringIO(), width=200),
        client_factory=lambda: client,
        session=_Session(),
        token_prompt=lambda _prompt: "",
        confirm_prompt=confirm,
        local_target_factory=factory,
        trust_store=store,
    )
    return shell, client, store, asked


def test_trusting_the_folder_connects_this_machine_and_remembers_the_answer(tmp_path, monkeypatch):
    shell, _, store, asked = _shell(tmp_path, monkeypatch, answers=["y"])
    shell._offer_local_target()

    assert len(asked) == 1
    assert store.is_trusted(tmp_path)
    assert shell.local_target is not None and shell.local_target.connected
    assert shell.local_target.folder == Path.cwd()


def test_ctrl_c_or_ctrl_d_at_the_trust_prompt_is_a_no(tmp_path, monkeypatch):
    for pressed in (KeyboardInterrupt, EOFError):
        shell, _, store, _ = _shell(tmp_path, monkeypatch)

        def press(_message, pressed=pressed):
            raise pressed

        shell._confirm_prompt = press
        shell._offer_local_target()

        assert shell.local_target is None
        assert not store.is_trusted(tmp_path)


def test_ctrl_c_on_a_turn_also_stops_that_chats_command_running_here(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    calls = []
    client.cancel_chat = lambda chat_id, reason=None: calls.append("cancel")
    shell.local_target.interrupt = lambda chat_id=None: calls.append(("interrupt", chat_id)) or 1

    shell._cancel_active_turn(42)

    # The turn is cancelled first, so the stopped command's result can't resume it; only chat 42's commands stop.
    assert calls == ["cancel", ("interrupt", 42)]
    assert "Stopped the command running on this machine" in _said(shell)


def test_ctrl_c_on_a_turn_that_already_ended_stops_nothing_here(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    interrupted = []

    def finished(chat_id, reason=None):
        raise PortalError("no active turn", status_code=409)

    client.cancel_chat = finished
    shell.local_target.interrupt = lambda chat_id=None: interrupted.append(chat_id) or 1

    shell._cancel_active_turn(42)

    assert interrupted == []
    assert "Nothing to stop" in _said(shell)


def test_declining_connects_nothing(tmp_path, monkeypatch):
    _Target.made.clear()
    shell, _, store, _ = _shell(tmp_path, monkeypatch, answers=["n"])
    shell._offer_local_target()

    assert shell.local_target is None
    assert not store.is_trusted(tmp_path)
    assert _Target.made == []


def test_a_trusted_folder_connects_without_asking(tmp_path, monkeypatch):
    shell, _, _, asked = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()

    assert asked == []
    assert shell.local_target.connected


def test_a_failed_connect_leaves_the_shell_working_without_it(tmp_path, monkeypatch):
    class _Offline(_Target):
        def connect(self):
            raise PortalError("offline")

    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True, factory=_Offline)
    shell._offer_local_target()

    assert shell.local_target is None


def test_without_a_factory_nothing_is_asked_or_connected(tmp_path, monkeypatch):
    shell, _, _, asked = _shell(tmp_path, monkeypatch, factory=None)
    shell._offer_local_target()

    assert asked == []
    assert shell.local_target is None


def test_a_new_chat_is_scoped_to_this_machine_and_bound_to_this_session(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._send_prompt("run the tests")
    shell._send_prompt("now lint")

    first, second = client.turns
    assert first["chat_id"] is None
    assert first["server_ids"] == [5]
    assert first["active_server_id"] == 5
    assert first["local_session"] == {"server_id": 5, "instance": "session-1"}
    # A follow-up names the session too, so its turn counts as typed here; the scope travels only on create.
    assert second == {
        "message": "now lint", "chat_id": 42, "server_id": None, "local_session": {"server_id": 5, "instance": "session-1"},
    }


def test_chosen_servers_stay_the_scope_and_the_chat_is_still_bound(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell.selected_server_ids = [9]
    shell.selected_server_id = 9
    shell._send_prompt("check disk")

    [turn] = client.turns
    assert turn["server_ids"] == [9]
    assert turn["local_session"]["server_id"] == 5


def test_every_message_names_this_session_without_changing_the_chats_scope(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell.chat_id = 17
    shell._send_prompt("continue")
    shell._send_prompt("and again")

    # Each turn is typed here, so Skyportal tells this CLI to run its commands without asking.
    session = {"server_id": 5, "instance": "session-1"}
    assert client.turns == [
        {"message": "continue", "chat_id": 17, "server_id": None, "local_session": session},
        {"message": "and again", "chat_id": 17, "server_id": None, "local_session": session},
    ]


def test_server_local_selects_this_machine(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    shell._cmd_server(["local"])

    assert shell.selected_server_ids == [5]


def test_local_off_disconnects_and_on_connects_again(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    first = shell.local_target

    shell._cmd_local(["off"])
    assert first.closed and shell.local_target is None

    shell._cmd_local(["on"])
    assert shell.local_target is not None and shell.local_target is not first


def _prompt_says(shell) -> str:
    return "".join(text for _, text in shell._prompt_fragments())


def test_logout_disconnects_this_machine_while_the_key_still_works(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    target = shell.local_target
    logged_out = []
    client.logout = lambda: logged_out.append(target.closed)

    shell._cmd_logout([])

    # Closed before the key was removed: its disconnect still reaches Skyportal.
    assert logged_out == [True]
    assert shell.local_target is None
    assert "local#" not in _prompt_says(shell)
    assert "no longer runs commands on this machine" in _said(shell)


def test_a_new_key_disconnects_this_machine_and_the_next_message_connects_it_with_that_key(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    first = shell.local_target
    saved = []
    client.login = lambda open_browser: {"verification_url": "https://app.skyportal.ai/api-keys", "browser_opened": True}
    client.set_access_token = lambda token: saved.append((token, first.closed))
    shell._token_prompt = lambda _prompt: "sk_another_account"

    shell._cmd_token([])

    # The poller kept the old key, which may be another account's: that session ends before the new key is saved.
    assert saved == [("sk_another_account", True)]
    assert shell.local_target is None
    shell._send_prompt("hello")
    assert shell.local_target is not first and shell.local_target.connected
    assert client.turns[-1]["local_session"]["server_id"] == 5


def test_leaving_the_shell_disconnects_this_machine(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell.run()

    assert _Target.made[-1].closed


@pytest.mark.skipif(os.name == "nt", reason="SIGHUP is POSIX")
def test_closing_the_terminal_window_disconnects_this_machine(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True)

    class _HangUp:
        def prompt(self, _message):
            os.kill(os.getpid(), signal.SIGHUP)
            time.sleep(1)

    shell.session = _HangUp()
    before = signal.getsignal(signal.SIGHUP)  # SIG_IGN under nohup, SIG_DFL otherwise
    with pytest.raises(SystemExit):
        shell.run()

    assert _Target.made[-1].closed
    assert signal.getsignal(signal.SIGHUP) == before


def test_the_real_entry_point_wires_this_machine():
    with (
        patch.object(shell_commands, "InteractiveShell") as shell_class,
        patch.object(shell_commands, "show_startup_animation"),
    ):
        # The settings only reach the client factory, which this test never calls.
        shell_commands.run_shell(SimpleNamespace())

    assert shell_class.call_args.kwargs["local_target_factory"] is LocalTarget


def test_every_command_this_machine_runs_is_printed_in_the_terminal(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, answers=["y"])
    shell._offer_local_target()

    # A chat this CLI started may be continued from the browser: its commands still show here.
    shell.local_target.show("▸ running: git log  (chat #123)")

    assert "▸ running: git log  (chat #123)" in shell.console.file.getvalue()


def test_a_line_from_a_worker_is_printed_above_the_prompt_the_user_is_at(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch)
    scheduled = []
    shell.session = SimpleNamespace(
        app=SimpleNamespace(is_running=True, loop=SimpleNamespace(call_soon_threadsafe=scheduled.append)),
    )

    with patch("skyportalai.shell.interactive.run_in_terminal", side_effect=lambda func: func()) as above:
        shell._print_from_worker("▸ exit 0: git log  (chat #123)")
        # Not from the worker's thread, over the prompt: on the prompt's own loop, above it.
        assert shell.console.file.getvalue() == ""
        [callback] = scheduled
        callback()

    above.assert_called_once()
    assert "▸ exit 0: git log  (chat #123)" in shell.console.file.getvalue()


def _said(shell) -> str:
    return " ".join(shell.console.file.getvalue().split())


def test_the_trust_prompt_says_the_folder_is_where_commands_start_not_a_limit(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, answers=["n"])
    shell._offer_local_target()

    said = _said(shell)
    assert "starts in this folder, but that is not a limit" in said
    assert "anything you can" in said
    assert "this folder and everything inside it" in said
    assert "typed anywhere but here (your browser, say) also waits for your y in this terminal" in said
    assert "home folder" not in said


def test_the_trust_prompt_warns_when_the_folder_is_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    shell, _, _, asked = _shell(tmp_path, monkeypatch, answers=["n"])
    shell._offer_local_target()

    assert "This is your home folder" in _said(shell)
    assert len(asked) == 1


def test_a_filesystem_root_is_refused_without_a_trust_prompt_or_a_connection(tmp_path, monkeypatch):
    _Target.made.clear()
    shell, _, store, asked = _shell(tmp_path, monkeypatch, answers=["y"])
    monkeypatch.chdir(tmp_path.anchor)
    shell._offer_local_target()

    assert asked == []
    assert _Target.made == []
    assert shell.local_target is None
    assert not store.is_trusted(Path.cwd())
    assert "root of a filesystem" in _said(shell)


def test_a_machine_removed_in_skyportal_leaves_the_prompt_and_reconnects_with_local_on(tmp_path, monkeypatch):
    class _Removable(_Target):
        gone = False

        @property
        def connected(self):
            return self.server_id is not None and not self.closed and not self.gone

    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True, factory=_Removable)
    shell._offer_local_target()
    first = shell.local_target
    shell._send_prompt("run the tests")
    assert "local#altaf-laptop" in "".join(text for _, text in shell._prompt_fragments())

    first.gone = True
    assert "local#" not in "".join(text for _, text in shell._prompt_fragments())

    shell._cmd_local(["on"])

    assert first.closed
    assert shell.local_target is not first and shell.local_target.connected
    # The next message binds the chat to the new session.
    shell._send_prompt("and now")
    assert shell.client.turns[-1]["local_session"]["instance"] == shell.local_target.instance


def test_the_idle_prompt_is_drawn_from_the_current_context_so_a_notice_redraws_it(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    seen = []

    class _Idle:
        def prompt(self, message):
            seen.append(message)
            raise EOFError

    shell.session = _Idle()
    shell._loop()

    # A callable: prompt_toolkit calls it again on every redraw, such as after a worker's notice.
    assert callable(seen[0])
    assert "local#altaf-laptop" in "".join(text for _, text in seen[0]())
    shell.local_target.closed = True
    assert "local#" not in "".join(text for _, text in seen[0]())


class _Interrupted:
    """A prompt that is interrupted once, then ends the shell."""

    def __init__(self):
        self.asked = 0

    def prompt(self, _message):
        self.asked += 1
        raise KeyboardInterrupt if self.asked == 1 else EOFError


@pytest.mark.parametrize(
    ("running", "said", "not_said"),
    [
        (2, "Stopped 2 commands running on this machine", "Press Ctrl-D"),
        (1, "Stopped 1 command running on this machine", "Press Ctrl-D"),
        (0, "Press Ctrl-D", "Stopped"),
    ],
)
def test_ctrl_c_at_the_idle_prompt_stops_what_runs_on_this_machine(tmp_path, monkeypatch, running, said, not_said):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    shell.local_target.interrupt = lambda: running
    shell.session = _Interrupted()

    shell._loop()

    assert said in _said(shell)
    assert not_said not in _said(shell)


def test_ctrl_c_at_the_idle_prompt_without_this_machine_keeps_the_old_message(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, factory=None)
    shell.session = _Interrupted()

    shell._loop()

    assert "Press Ctrl-D or type /exit" in _said(shell)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("connection reset"),
        http.client.RemoteDisconnected("closed without a response"),
        ssl.SSLError("handshake failed"),
        OSError("network unreachable"),
        ValueError("odd"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_a_failure_connecting_this_machine_never_aborts_the_shell(tmp_path, monkeypatch, error):
    class _Dropped(_Target):
        def connect(self):
            raise error

    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True, factory=_Dropped)

    shell.run()

    said = _said(shell)
    assert shell.local_target is None
    assert "Could not connect this machine" in said and "/local on" in said


def test_a_factory_that_fails_never_aborts_the_shell(tmp_path, monkeypatch):
    def broken(client, folder):
        raise RuntimeError("no target for you")

    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True, factory=broken)

    shell.run()

    assert shell.local_target is None
    assert "Could not connect this machine" in _said(shell)


def test_a_folder_that_no_longer_exists_is_not_a_crash(tmp_path, monkeypatch):
    _Target.made.clear()
    shell, _, _, asked = _shell(tmp_path, monkeypatch, answers=["y"])

    def gone(cls):
        raise FileNotFoundError("the current folder was deleted")

    monkeypatch.setattr(Path, "cwd", classmethod(gone))
    shell._offer_local_target()

    assert asked == [] and _Target.made == []
    assert "no longer exists" in _said(shell)


def test_a_trust_choice_that_cannot_be_saved_still_connects_for_this_session(tmp_path, monkeypatch):
    blocker = tmp_path / "blocked"
    blocker.write_text("")
    shell, _, _, _ = _shell(tmp_path, monkeypatch, answers=["y"])
    shell._trust_store = TrustStore(blocker / "trusted.json")

    shell._offer_local_target()

    assert shell.local_target is not None and shell.local_target.connected
    assert "only until you leave the shell" in _said(shell)
    assert shell._trust_store.is_trusted(tmp_path)


def test_server_auto_says_new_chats_still_start_on_this_machine(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()

    shell._cmd_server(["auto"])

    assert "New chats still start on this machine" in _said(shell)
    assert "/local off" in _said(shell)


def test_server_auto_without_this_machine_says_nothing_about_it(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch, factory=None)

    shell._cmd_server(["auto"])

    assert "Server selection set to automatic" in _said(shell)
    assert "this machine" not in _said(shell)


def test_a_line_from_a_worker_falls_back_to_a_direct_print_when_the_prompts_loop_closed(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch)

    def closed(_callback):
        raise RuntimeError("Event loop is closed")

    loop = SimpleNamespace(call_soon_threadsafe=closed)
    shell.session = SimpleNamespace(app=SimpleNamespace(is_running=True, loop=loop))

    shell._print_from_worker("▸ exit 0: git log  (chat #123)")

    assert "▸ exit 0: git log  (chat #123)" in shell.console.file.getvalue()


def test_a_line_from_a_worker_never_carries_a_terminal_escape(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch)

    shell._print_from_worker("▸ running: echo \x1b[2K\x1b]0;title\x07evil")

    assert "\x1b" not in shell.console.file.getvalue() and "\x07" not in shell.console.file.getvalue()
    assert "evil" in shell.console.file.getvalue()


def _ask_on_a_worker(shell, seconds=30):
    """What a command thread gets back, asked as LocalTarget asks for a turn typed elsewhere."""
    got = []
    worker = threading.Thread(target=lambda: got.append(shell._ask_from_worker(
        "rm -rf build  (chat #12)\nIts turn wasn't typed in this terminal: it came from the browser, Slack or the API.",
        "Run this on altaf-laptop? [y/N] ", seconds,
    )))
    worker.start()
    deadline = time.monotonic() + 5
    while shell._questions.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    return worker, got


def _pressed(error):
    def press(_message, _seconds):
        raise error

    return press


@pytest.mark.parametrize(
    ("prompt", "answer"),
    [
        (lambda _message, _seconds: "y", True),
        (lambda _message, _seconds: "n", False),
        (_pressed(KeyboardInterrupt), False),
        (lambda _message, _seconds: None, None),  # nobody answered before the deadline
    ],
)
def test_a_command_threads_question_is_asked_here_and_its_answer_handed_back(tmp_path, monkeypatch, prompt, answer):
    shell, _, _, _ = _shell(tmp_path, monkeypatch)
    # Autoapprove answers approval cards, never this question.
    shell._permission_mode_for_approval = lambda: "autoapprove"
    asked = []
    shell._question_prompt = lambda message, seconds: asked.append((message, seconds)) or prompt(message, seconds)

    worker, got = _ask_on_a_worker(shell)
    shell._answer_questions()
    worker.join(5)

    assert got == [answer]
    [(message, seconds)] = asked
    assert message == "Run this on altaf-laptop? [y/N] " and 0 < seconds <= 30
    said = _said(shell)
    assert "A command from outside this terminal" in said
    assert "rm -rf build (chat #12)" in said and "the browser, Slack or the API" in said
    # No prompt was running (a turn, say), so it said one was waiting.
    assert "waiting for your y/N" in said


def test_a_question_takes_over_the_idle_prompt_and_what_was_typed_comes_back(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch)
    exited = []
    shell.session = SimpleNamespace(app=SimpleNamespace(
        is_running=True, current_buffer=SimpleNamespace(text="git sta"), exit=lambda result: exited.append(result),
    ))
    shell._at_idle_prompt = True
    shell._take_the_prompt_for_a_question()
    assert exited == [_HeldInput("git sta")]

    prompted, asked = [], []

    class _Prompt:
        def prompt(self, _message, **typed):
            prompted.append(typed)
            if len(prompted) == 1:
                return _HeldInput("git sta")
            raise EOFError

    shell.session = _Prompt()
    shell._answer_questions = lambda: asked.append("asked")
    shell._loop()

    assert prompted == [{}, {"default": "git sta"}]
    assert asked == ["asked", "asked"]


def test_a_question_waits_for_any_other_prompt_and_says_so(tmp_path, monkeypatch):
    shell, _, _, _ = _shell(tmp_path, monkeypatch)
    exited = []
    shell.session = SimpleNamespace(app=SimpleNamespace(
        is_running=True, current_buffer=SimpleNamespace(text="y"), exit=lambda result: exited.append(result),
    ))
    shell._at_idle_prompt = False  # an approval card's y/N, say

    with patch("skyportalai.shell.interactive.run_in_terminal", side_effect=lambda func: func()):
        shell._take_the_prompt_for_a_question()

    assert exited == []
    assert "waiting for your y/N" in _said(shell)
