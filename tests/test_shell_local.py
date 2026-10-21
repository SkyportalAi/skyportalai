"""The shell connects this machine in a trusted folder and binds its chats to it (skyportal-website#3665)."""

import os
import signal
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from skyportalai.cli import shell_commands
from skyportalai.shell.interactive import InteractiveShell
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


def test_ctrl_c_on_a_turn_also_stops_the_command_running_here(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell._offer_local_target()
    calls = []
    client.cancel_chat = lambda chat_id, reason=None: calls.append("cancel")
    shell.local_target.interrupt = lambda: calls.append("interrupt") or 1

    shell._cancel_active_turn(42)

    # The turn is cancelled first, so the stopped command's result can't resume it.
    assert calls == ["cancel", "interrupt"]


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
    # Bound once: a follow-up carries nothing more.
    assert second == {"message": "now lint", "chat_id": 42, "server_id": None}


def test_chosen_servers_stay_the_scope_and_the_chat_is_still_bound(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell.selected_server_ids = [9]
    shell.selected_server_id = 9
    shell._send_prompt("check disk")

    [turn] = client.turns
    assert turn["server_ids"] == [9]
    assert turn["local_session"]["server_id"] == 5


def test_a_resumed_chat_is_bound_on_its_next_message_without_changing_its_scope(tmp_path, monkeypatch):
    shell, client, _, _ = _shell(tmp_path, monkeypatch, trusted=True)
    shell.chat_id = 17
    shell._send_prompt("continue")
    shell._send_prompt("and again")

    first, second = client.turns
    assert first == {"message": "continue", "chat_id": 17, "server_id": None,
                     "local_session": {"server_id": 5, "instance": "session-1"}}
    assert "local_session" not in second and "server_ids" not in second


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
    with pytest.raises(SystemExit):
        shell.run()

    assert _Target.made[-1].closed
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL


def test_the_real_entry_point_wires_this_machine():
    with (
        patch.object(shell_commands, "InteractiveShell") as shell_class,
        patch.object(shell_commands, "show_startup_animation"),
    ):
        shell_commands.run_shell()

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
    assert "only where they start, not a limit" in said
    assert "anything you can" in said
    assert "runs what Skyportal sends it" in said
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
    assert shell._bound_chats == {42}

    first.gone = True
    assert "local#" not in "".join(text for _, text in shell._prompt_fragments())

    shell._cmd_local(["on"])

    assert first.closed
    assert shell.local_target is not first and shell.local_target.connected
    assert shell._bound_chats == set()


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
