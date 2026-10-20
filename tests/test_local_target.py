"""This machine as a command target: identity, trust, repo context, running a leased command (skyportal-website#3665)."""

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from skyportalai.agent.kubernetes.commands import CommandPoller
from skyportalai.shell import local_target
from skyportalai.shell.local_target import (
    TIMEOUT_EXIT_CODE,
    LocalTarget,
    ShellRunner,
    TrustStore,
    display_remote,
    machine_id,
    repo_context,
)
from skyportalai.shell.portal import PortalError

BASE_URL = "https://app.skyportal.ai"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="runs commands in a POSIX shell")


@pytest.mark.parametrize(
    ("remote", "shown"),
    [
        ("https://x-access-token:ghp_secret@github.com/SkyportalAi/skyportal-website.git",
         "github.com/SkyportalAi/skyportal-website"),
        ("git@github.com:SkyportalAi/skyportalai.git", "github.com/SkyportalAi/skyportalai"),
        ("ssh://git@gitlab.example.com:2222/team/app.git", "gitlab.example.com:2222/team/app"),
        # A remote on this disk would say where the user keeps things: nothing is sent.
        ("/home/altaf/mirrors/app.git", ""),
        ("file:///srv/git/app.git", ""),
        ("C:\\repos\\app", ""),
        ("../sibling", ""),
        ("", ""),
    ],
)
def test_a_remote_is_shown_as_host_and_path_without_credentials(remote, shown):
    assert display_remote(remote) == shown


def test_the_machine_id_is_made_once_and_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_MACHINE_ID_PATH", str(tmp_path / "machine_id"))
    first = machine_id()
    assert first == machine_id()
    assert (tmp_path / "machine_id").read_text() == first


def test_trusting_a_folder_covers_what_is_below_it_and_nothing_beside_it(tmp_path):
    store = TrustStore(tmp_path / "trusted.json")
    repo = tmp_path / "repo"
    (repo / "api").mkdir(parents=True)
    (tmp_path / "other").mkdir()

    assert not store.is_trusted(repo)
    store.trust(repo)

    assert store.is_trusted(repo)
    assert store.is_trusted(repo / "api")
    assert not store.is_trusted(tmp_path / "other")
    assert not store.is_trusted(tmp_path)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_repo_context_names_the_remote_branch_and_folder_never_an_absolute_path(tmp_path):
    repo = tmp_path / "work"
    (repo / "services" / "api").mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-b", "feature/x")
    git("remote", "add", "origin", "https://user:tok@github.com/o/r.git")

    context = repo_context(repo / "services" / "api")

    assert context.as_payload() == {"remote": "github.com/o/r", "branch": "feature/x", "path": "services/api"}
    assert str(tmp_path) not in repr(context)
    assert context.label() == "o/r @ feature/x"
    assert repo_context(tmp_path) is None


@POSIX_ONLY
def test_a_command_runs_in_the_folder_and_reports_both_streams(tmp_path):
    result = ShellRunner(tmp_path, threading.Event())("pwd; echo oops >&2; exit 3", 10)

    assert result["exit_code"] == 3
    assert Path(result["stdout"].strip()).resolve() == tmp_path.resolve()
    assert "oops" in result["stderr"]


@POSIX_ONLY
def test_a_command_past_its_timeout_is_stopped_with_124(tmp_path):
    started = time.monotonic()
    result = ShellRunner(tmp_path, threading.Event())("sleep 30", 1)

    assert result["exit_code"] == TIMEOUT_EXIT_CODE
    assert time.monotonic() - started < 10
    assert "timeout" in result["stderr"]


@POSIX_ONLY
def test_closing_the_cli_stops_a_running_command(tmp_path):
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    result = ShellRunner(tmp_path, stop)("sleep 30", 60)

    assert result["exit_code"] == local_target.STOPPED_EXIT_CODE
    assert "closed" in result["stderr"]


def _running(marker: str) -> int:
    """How many processes have `marker` in their command line."""
    if os.name == "nt":
        listing = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process | Where-Object CommandLine -like '*%s*').Count" % marker],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        return int(listing or 0)
    found = 0
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            found += marker.encode() in cmdline.read_bytes().replace(bytes(1), b" ")
        except OSError:
            continue
    return found


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_stopping_a_command_stops_everything_it_started(tmp_path):
    # On Windows, Git Bash's children aren't Windows children of bash.exe, so taskkill /T missed them.
    stop = threading.Event()
    threading.Timer(1.5, stop.set).start()
    result = ShellRunner(tmp_path, stop)("sleep 41 & sleep 43; wait", 60)
    time.sleep(0.5)

    assert result["exit_code"] == local_target.STOPPED_EXIT_CODE
    assert _running("sleep 41") == 0
    assert _running("sleep 43") == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_ctrl_c_stops_the_running_command_and_says_so(tmp_path):
    runner = ShellRunner(tmp_path, threading.Event())
    threading.Timer(1.5, runner.stop_all, args=("interrupted",)).start()
    result = runner("sleep 53", 60)

    assert result["exit_code"] == local_target.STOPPED_EXIT_CODE
    assert "Ctrl-C" in result["stderr"]
    assert _running("sleep 53") == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_closing_the_target_kills_the_running_command_before_it_returns(tmp_path, monkeypatch):
    # The CLI exits right after close(): a worker thread polling every 0.5 s would never get to kill it.
    monkeypatch.setenv("SKYPORTALAI_MACHINE_ID_PATH", str(tmp_path / "machine_id"))
    client = _Client()
    target = LocalTarget(client, tmp_path, poller_factory=_Poller)
    target.connect()
    results = []
    worker = threading.Thread(target=lambda: results.append(target._runner("sleep 59", 60)))
    worker.start()
    deadline = time.monotonic() + 10
    while _running("sleep 59") == 0 and time.monotonic() < deadline:
        time.sleep(0.1)

    target.close()

    assert _running("sleep 59") == 0
    worker.join(5)
    assert results[0]["exit_code"] == local_target.STOPPED_EXIT_CODE
    assert "closed" in results[0]["stderr"]


@pytest.mark.skipif(os.name != "nt", reason="a job object is Windows-only")
def test_on_windows_a_running_command_dies_with_a_cli_that_is_killed(tmp_path):
    script = tmp_path / "cli.py"
    script.write_text(
        "import os, sys, threading, time\n"
        "from pathlib import Path\n"
        "from skyportalai.shell.local_target import ShellRunner\n"
        "threading.Thread(target=ShellRunner(Path(sys.argv[1]), threading.Event()), args=('sleep 61', 60)).start()\n"
        "time.sleep(2)\n"
        "os._exit(9)\n"
    )
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=False, timeout=30)
    time.sleep(1)

    assert _running("sleep 61") == 0


def test_a_finished_commands_background_work_outlives_it(tmp_path):
    marker = tmp_path / "late"
    python, target = Path(sys.executable).as_posix(), marker.as_posix()
    command = f"'{python}' -c \"import time, pathlib; time.sleep(2); pathlib.Path('{target}').write_text('ok')\" &"
    result = ShellRunner(tmp_path, threading.Event())(command, 30)

    assert result["exit_code"] == 0
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert marker.exists()


@POSIX_ONLY
def test_long_output_keeps_its_head_and_tail(tmp_path):
    command = f"{sys.executable} -c \"print('A' * 200000 + 'END')\""
    result = ShellRunner(tmp_path, threading.Event())(command, 30)

    out = result["stdout"]
    assert len(out) < 200_000
    assert out.startswith("A")
    assert out.rstrip().endswith("END")
    assert "bytes omitted" in out


def test_output_is_read_head_and_tail_without_loading_the_whole_file(tmp_path):
    big = tmp_path / "out"
    big.write_bytes(b"H" * 1000 + b"x" * 5_000_000 + b"T" * 1000)
    with big.open("rb") as file:
        text = local_target._read_bounded(file)
    assert len(text) < local_target.OUTPUT_HEAD_BYTES + local_target.OUTPUT_TAIL_BYTES + 100
    assert text.startswith("H") and text.endswith("T")


def test_no_command_is_not_run():
    result = ShellRunner(Path("."), threading.Event())(None, 5)
    assert result["exit_code"] == 126


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _LocalServer:
    """Skyportal's local-machine queue for one session: one leased command, then nothing."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if url.endswith("/commands/lease/"):
            if any(call["url"].endswith("/commands/lease/") for call in self.calls[:-1]):
                return _Response(204)
            return _Response(200, {"id": 7, "command": "npm test", "timeout_seconds": 600, "report_by": None})
        return _Response(200, {"recorded": True})

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


def test_the_agents_poller_runs_the_local_queue_with_the_cli_session():
    server = _LocalServer()
    ran = []
    poller = CommandPoller(
        BASE_URL, "sk_key",
        session=server, session_factory=lambda: server,
        run=lambda command, timeout: ran.append((command, timeout)) or {"exit_code": 0, "stdout": "ok", "stderr": ""},
        lease_path="/api/v1/agent/local/5/commands/lease/",
        result_path="/api/v1/agent/local/5/commands/{id}/result/",
        extra_headers={"X-Agent-Instance": "session-1"},
        command_field="command",
        thread_prefix="local-command",
    )

    worker = poller.poll_once()
    worker.join(5)

    assert ran == [("npm test", 600)]
    lease, result = server.calls
    assert lease["url"] == BASE_URL + "/api/v1/agent/local/5/commands/lease/"
    assert result["url"] == BASE_URL + "/api/v1/agent/local/5/commands/7/result/"
    assert result["json"] == {"exit_code": 0, "stdout": "ok", "stderr": ""}
    assert lease["headers"]["Authorization"] == "Bearer sk_key"
    assert lease["headers"]["X-Agent-Instance"] == "session-1"
    assert worker.name == "local-command-7"


class _Client:
    base_url = BASE_URL

    def __init__(self, heartbeat_errors=()):
        self.connects = []
        self.disconnects = []
        self.heartbeat_errors = list(heartbeat_errors)

    def bearer_token(self):
        return "sk_key"

    def connect_local_machine(self, instance, **payload):
        self.connects.append((instance, payload))
        return {"server_id": 5, "hostname": "altaf-laptop"}

    def local_machine_heartbeat(self, server_id, instance):
        if self.heartbeat_errors:
            raise self.heartbeat_errors.pop(0)
        return {}

    def disconnect_local_machine(self, server_id, instance):
        self.disconnects.append((server_id, instance))
        return {"withdrawn": 0}


class _Poller:
    made = []

    def __init__(self, base_url, token, **options):
        self.options = options
        _Poller.made.append(self)

    def run_forever(self):
        self.options["stop_event"].wait()


def test_connecting_registers_the_machine_and_leases_for_this_session_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_MACHINE_ID_PATH", str(tmp_path / "machine_id"))
    client = _Client()
    target = LocalTarget(client, tmp_path, poller_factory=_Poller)

    target.connect()
    try:
        [(instance, payload)] = client.connects
        assert instance == target.instance
        assert payload["machine_id"] == (tmp_path / "machine_id").read_text()
        assert payload["repo"] is None  # not a git repository
        assert str(tmp_path) not in repr(payload)
        options = _Poller.made[-1].options
        assert options["lease_path"] == "/api/v1/agent/local/5/commands/lease/"
        assert options["extra_headers"]["X-Agent-Instance"] == target.instance
        assert options["command_field"] == "command"
        assert target.session == {"server_id": 5, "instance": target.instance}
        assert target.connected
    finally:
        target.close()
    target.close()

    assert client.disconnects == [(5, target.instance)]
    assert not target.connected


def test_a_session_skyportal_lost_is_opened_again_by_the_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_MACHINE_ID_PATH", str(tmp_path / "machine_id"))
    monkeypatch.setattr(local_target, "HEARTBEAT_SECONDS", 0.05)
    client = _Client(heartbeat_errors=[PortalError("closed", status_code=409, code="session_closed")])
    target = LocalTarget(client, tmp_path, poller_factory=_Poller)

    target.connect()
    deadline = time.monotonic() + 5
    while len(client.connects) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    target.close()

    assert len(client.connects) == 2
    assert {instance for instance, _ in client.connects} == {target.instance}
