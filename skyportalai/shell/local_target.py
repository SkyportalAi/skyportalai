"""Run SARA's commands on this machine, starting in the folder the user trusts (skyportal-website#3665)."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from skyportalai import _env
from skyportalai._version import __version__

from .portal import PortalError, SkyportalClient

# The shell configures no logging; without a handler, a failed poll would print over the prompt.
logging.getLogger("skyportalai").addHandler(logging.NullHandler())
logger = logging.getLogger(__name__)

# Skyportal's gate decides what is queued. The CLI asks one thing: a command from a turn it didn't type (the browser
# continuing its chat, say) runs only after a y at its terminal. Each command it runs is printed, and none starts
# before Skyportal confirms it is still wanted: a turn stopped while the terminal asked has withdrawn it.
LEASE_PATH = "/api/v1/agent/local/{server_id}/commands/lease/"
RESULT_PATH = "/api/v1/agent/local/{server_id}/commands/{{id}}/result/"
# Skyportal takes a CLI silent for 40 s as closed and withdraws its commands. A beat starts every
# HEARTBEAT_SECONDS whatever the last one took, and each socket operation of one is cut off after
# HEARTBEAT_TIMEOUT_SECONDS. That is not a wall-clock limit: a beat that connects slowly can run longer.
HEARTBEAT_SECONDS = 15
HEARTBEAT_TIMEOUT_SECONDS = 10
# After repeated failures the lease poll waits longer between tries, up to this.
LEASE_BACKOFF_MAX_SECONDS = 30
# A yes leaves the command its whole timeout and this much more before the lease's report_by, so its result is
# still taken; a no reaches Skyportal while the turn still waits for the command.
ANSWER_MARGIN_SECONDS = 15
# The longest Skyportal gives a command on this machine, whatever a lease asks for.
MAX_TIMEOUT_SECONDS = 1800
# Kept per stream: the head and the tail, where a failing build says why. The model reads less than this.
OUTPUT_HEAD_BYTES = 30_000
OUTPUT_TAIL_BYTES = 70_000
# Written to disk per stream, so a command that never stops printing can't fill it before its timeout.
MAX_STREAM_BYTES = 64 * 1024 * 1024
TIMEOUT_EXIT_CODE = 124
STOPPED_EXIT_CODE = 130
# 128 + SIGXFSZ, what a shell reports for a process that exceeded its file size limit.
OUTPUT_LIMIT_EXIT_CODE = 153
_NOT_RUN_BECAUSE = {
    "declined": "you said no",
    "no_answer": "no answer in time",
    "withdrawn": "its turn was stopped",
    "unconfirmed": "Skyportal couldn't confirm it was still wanted",
    "too_long": "too long for its y/N question to show it all",
    "too_late": "it could no longer finish before its result was due",
}
_STOPPED_BECAUSE = {
    "closed": "Stopped: the skyportalai CLI was closed.",
    "interrupted": "Stopped: interrupted with Ctrl-C in the skyportalai CLI.",
}
_GIT_SECONDS = 5
# How often a running command is checked for its timeout, a stop and the size of its output.
_POLL_SECONDS = 0.1
# How much of a command the terminal shows when it starts and ends: one line, with the rest counted.
_SHOWN_COMMAND_CHARS = 500
# The y/N question shows all of a command; one longer than this, as shown, is not asked about and never runs.
_ASKED_COMMAND_CHARS = 10_000
# In the question, a run of spaces this long is counted rather than printed: it could push the rest out of sight.
_SPACE_RUN = re.compile(r" {5,}")
# How long a killed process gets to exit before it is given up on: one in uninterruptible sleep never does.
_KILL_WAIT_SECONDS = 5
_CREATE_SUSPENDED = 0x00000004

if os.name == "nt":
    import ctypes.wintypes

    wintypes = ctypes.wintypes
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)


def machine_id() -> str:
    """A random id for this machine, made once and kept with the CLI's config. Not the hostname."""
    path = _env.config_path("machine_id", "SKYPORTALAI_MACHINE_ID_PATH")
    try:
        existing = path.read_text().strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    made = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(made)
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not save this machine's id; it gets a new one next time")
    return made


def machine_name() -> str:
    try:
        return socket.gethostname()[:60] or "my-machine"
    except OSError:
        return "my-machine"


def is_filesystem_root(folder: Path) -> bool:
    """`/`, a drive root like `C:\\`, or a network share's root: a folder with nothing above it."""
    folder = folder.resolve()
    return folder.parent == folder


def is_home_folder(folder: Path) -> bool:
    try:
        return folder.resolve() == Path.home().resolve()
    except (OSError, RuntimeError):
        return False


class TrustStore:
    """The folders the user trusts Skyportal to start commands in; each covers everything inside it."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _env.config_path("trusted_folders.json", "SKYPORTALAI_TRUSTED_FOLDERS_PATH")
        self._session: List[str] = []

    def _folders(self) -> List[str]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        folders = data.get("folders") if isinstance(data, dict) else None
        return [str(folder) for folder in folders] if isinstance(folders, list) else []

    def is_trusted(self, folder: Path) -> bool:
        folder = folder.resolve()
        for trusted in [*self._folders(), *self._session]:
            # A root trusted before roots were refused would cover every folder on the disk.
            if is_filesystem_root(Path(trusted)):
                continue
            try:
                folder.relative_to(Path(trusted))
            except ValueError:
                continue
            return True
        return False

    def trust(self, folder: Path) -> bool:
        """Trust a folder; False if it could only be remembered until this shell exits."""
        resolved = str(folder.resolve())
        if resolved not in self._session:
            self._session.append(resolved)
        folders = self._folders()
        if resolved not in folders:
            folders.append(resolved)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"folders": folders}, indent=2))
            self.path.chmod(0o600)
        except OSError:
            logger.warning("Could not save the trusted folders to %s", self.path)
            return False
        return True


def _git(folder: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(folder), *args],
            capture_output=True, text=True, errors="replace", timeout=_GIT_SECONDS, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def display_remote(url: str) -> str:
    """A remote as host/owner/repo, never with its credentials, and nothing for a remote on this disk."""
    url = url.strip()
    if not url:
        return ""
    if "://" in url:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
        except ValueError:
            return ""
        if parsed.scheme == "file" or not hostname:
            return ""
        try:
            port = parsed.port
        except ValueError:
            port = None  # not a port: show the host alone rather than guess
        host = hostname + (f":{port}" if port else "")
        path = parsed.path
    elif ":" in url and not url.startswith(("/", ".", "~")) and not (len(url) > 1 and url[1] == ":"):
        # scp form: [user@]host:owner/repo.git
        host, _, path = url.partition(":")
        host = host.rpartition("@")[2]
    else:
        return ""  # a path on this disk would say where the user keeps things
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}/{path}" if path else host


# What leaves this machine about a repository: its remote (without credentials), the branch and this
# folder's path inside it. Never an absolute path.
@dataclass(frozen=True)
class RepoContext:
    remote: str = ""
    branch: str = ""
    # This folder relative to the repository root; "" at the root.
    path: str = ""

    def as_payload(self) -> Dict[str, str]:
        return {"remote": self.remote, "branch": self.branch, "path": self.path}

    def label(self) -> str:
        name = self.remote.split("/", 1)[1] if "/" in self.remote else (self.remote or "this repository")
        return f"{name} @ {self.branch}" if self.branch else name


def repo_context(folder: Path) -> Optional[RepoContext]:
    """Where this folder sits in a git repository, or None outside one."""
    top = _git(folder, "rev-parse", "--show-toplevel")
    if not top:
        return None
    remotes = _git(folder, "remote").split()
    remote_name = "origin" if "origin" in remotes else (remotes[0] if remotes else "")
    remote = display_remote(_git(folder, "remote", "get-url", remote_name)) if remote_name else ""
    # Names an unborn branch too, and nothing on a detached HEAD.
    branch = _git(folder, "symbolic-ref", "--short", "-q", "HEAD")
    try:
        inside = folder.resolve().relative_to(Path(top).resolve()).as_posix()
    except ValueError:
        inside = ""
    return RepoContext(remote=remote, branch=branch, path="" if inside == "." else inside)


def _is_wsl_launcher(path: str) -> bool:
    """System32's bash.exe and the Store's WindowsApps alias start WSL: another filesystem, where this folder isn't."""
    lowered = path.lower().replace("\\", "/")
    return "system32" in lowered or "windowsapps" in lowered or lowered.rsplit("/", 1)[-1] == "wsl.exe"


def _windows_bash(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Git for Windows' bash, which is not on PATH in PowerShell or Windows Terminal; None without Git."""
    bash = which("bash")
    if bash and not _is_wsl_launcher(bash):
        return bash
    git = which("git")
    if not git:
        return None
    # git.exe sits in <Git>\cmd or <Git>\mingw64\bin; <Git>\bin\bash.exe sets up the PATH its tools need.
    for root in list(Path(git).parents)[:3]:
        candidate = root / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _shell_argv() -> List[str]:
    """How a command is run here: bash where there is one, else the system's own shell."""
    if os.name == "nt":
        bash = _windows_bash()
        # Windows PowerShell 5.1 can't even parse `a && b`, so bash is worth finding.
        return [bash, "-c"] if bash else ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    return [shutil.which("bash") or "/bin/sh", "-c"]


def shell_name() -> str:
    return Path(_shell_argv()[0]).stem.lower()


def platform_name() -> str:
    system = platform.system()
    return {"Darwin": "macOS"}.get(system, system)


def _child_env() -> Dict[str, str]:
    """This process's environment without the CLI's own settings and credentials, which a command would print."""
    # Upper-cased: Windows names are case-insensitive. The removed SKYPORTAL_ names can still hold a live key.
    own = (_env.PREFIX, _env.REMOVED_PREFIX)
    return {name: value for name, value in os.environ.items() if not name.upper().startswith(own)}


def _stream_bytes(file: Any) -> int:
    return os.fstat(file.fileno()).st_size


def _read_bounded(file: Any) -> str:
    """A temp file's output, reading no more than its head and tail: a command can print gigabytes."""
    size = file.seek(0, os.SEEK_END)
    file.seek(0)
    if size <= OUTPUT_HEAD_BYTES + OUTPUT_TAIL_BYTES:
        return file.read().decode("utf-8", errors="replace")
    head = file.read(OUTPUT_HEAD_BYTES).decode("utf-8", errors="replace")
    file.seek(size - OUTPUT_TAIL_BYTES)
    tail = file.read().decode("utf-8", errors="replace")
    omitted = size - OUTPUT_HEAD_BYTES - OUTPUT_TAIL_BYTES
    return f"{head}\n… [{omitted} bytes omitted] …\n{tail}"


def _kill_on_close(job: int, enabled: bool) -> bool:
    """Whether the job's processes die when its last handle closes, as it does when this CLI dies."""
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE if enabled else 0
    return bool(_kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits),
    ))


def _job_for(process: subprocess.Popen) -> Optional[int]:
    """Put a process started suspended in a Windows job of its own, then let it run; the job, or None."""
    handle = int(process._handle)
    job = _kernel32.CreateJobObjectW(None, None)
    try:
        if job and _kernel32.AssignProcessToJobObject(job, handle):
            # While it runs, the command dies with this CLI, even one killed without a chance to stop it.
            _kill_on_close(job, True)
            return job
        if job:
            _kernel32.CloseHandle(job)
        return None
    finally:
        # Resumed whatever happened above, or the command would never start.
        _ntdll.NtResumeProcess(handle)


class _ProcessTree:
    """A command's process and everything it starts, so stopping it stops them all."""

    # POSIX: a session of its own, killed as a process group. Windows: a job object, because Git
    # Bash's children aren't Windows children of bash.exe and taskkill /T leaves them running.
    def __init__(self, argv: List[str], **options: Any):
        # kill() can come from another thread (the CLI closing, a Ctrl-C) while the runner closes the job.
        self._lock = threading.Lock()
        self.stopped_because: Optional[str] = None
        if os.name == "nt":
            self.process = subprocess.Popen(
                argv, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED, **options,
            )
            self._job = _job_for(self.process)
        else:
            self.process = subprocess.Popen(argv, start_new_session=True, **options)
            self._job = None

    def kill(self, because: str) -> None:
        with self._lock:
            if self.stopped_because is not None or self.process.poll() is not None:
                return
            self.stopped_because = because
            try:
                if self._job is not None:
                    _kernel32.TerminateJobObject(self._job, 1)
                elif os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        capture_output=True, check=False, timeout=_KILL_WAIT_SECONDS,
                    )
                else:
                    os.killpg(self.process.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                self.process.kill()
        try:
            self.process.wait(timeout=_KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("Process %s was still running %ss after it was killed", self.process.pid, _KILL_WAIT_SECONDS)

    def close(self) -> None:
        with self._lock:
            if self._job is not None:
                # What it left running in the background outlives it, as it does on POSIX.
                _kill_on_close(self._job, False)
                _kernel32.CloseHandle(self._job)
                self._job = None


def _result(exit_code: int, stdout: str = "", stderr: str = "") -> Dict[str, Any]:
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


class ShellRunner:
    """Run one leased command, starting in this folder, for its timeout at most, and report what it printed."""

    def __init__(self, folder: Path, stop: threading.Event, *, shell: Optional[List[str]] = None):
        self.folder = folder
        self._stop = stop
        self._shell = shell or _shell_argv()
        # Each running command, with the chat it runs for.
        self._running: Dict[_ProcessTree, Optional[int]] = {}
        self._lock = threading.Lock()

    def stop(self, because: str, chat_id: Optional[int] = None) -> int:
        """Stop the commands running now, only that chat's if one is named, on the caller's thread; how many."""
        with self._lock:
            running = [tree for tree, chat in self._running.items() if chat_id is None or chat == chat_id]
        for tree in running:
            tree.kill(because)
        return len(running)

    def __call__(self, command: object, timeout: float, chat_id: Optional[int] = None) -> Dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return _result(126, stderr="The CLI was sent no command to run.")
        # A lease can ask for more than the server allows, even more than a float can hold (10**400).
        timeout = min(timeout, MAX_TIMEOUT_SECONDS)
        deadline = time.monotonic() + timeout
        # Files, not pipes: a chatty command can't fill a pipe and hang, or fill this process's memory.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            # One critical section with stop(): a command leased as the CLI closes is stopped by it, or never started.
            with self._lock:
                if self._stop.is_set():
                    return _result(STOPPED_EXIT_CODE, stderr=_STOPPED_BECAUSE["closed"])
                try:
                    tree = _ProcessTree(
                        [*self._shell, command], cwd=self.folder, env=_child_env(),
                        stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    )
                except OSError as error:
                    return _result(127, stderr=f"Could not start {self._shell[0]}: {error}")
                except Exception as error:  # a NUL in the command is a ValueError; the chat still needs its result
                    return _result(126, stderr=f"Could not run this command: {error}")
                self._running[tree] = chat_id
            try:
                self._wait(tree, deadline, (out, err))
            finally:
                with self._lock:
                    self._running.pop(tree, None)
                tree.close()
            stdout = _read_bounded(out)
            stderr = _read_bounded(err)
        exit_code = tree.process.returncode
        if tree.stopped_because == "timeout":
            exit_code = TIMEOUT_EXIT_CODE
            stderr += f"\nStopped after its {int(timeout)}s timeout."
        elif tree.stopped_because == "output":
            exit_code = OUTPUT_LIMIT_EXIT_CODE
            stderr += f"\nStopped: its output passed the {MAX_STREAM_BYTES // (1024 * 1024)} MiB limit on one stream."
        elif tree.stopped_because in _STOPPED_BECAUSE:
            exit_code = STOPPED_EXIT_CODE
            stderr += "\n" + _STOPPED_BECAUSE[tree.stopped_because]
        return _result(exit_code, stdout, stderr)

    def _wait(self, tree: _ProcessTree, deadline: float, streams: Tuple[Any, ...]) -> None:
        while True:
            try:
                tree.process.wait(timeout=_POLL_SECONDS)
                return
            except subprocess.TimeoutExpired:
                pass
            if self._stop.is_set():
                tree.kill("closed")
            elif time.monotonic() >= deadline:
                tree.kill("timeout")
            elif any(_stream_bytes(stream) > MAX_STREAM_BYTES for stream in streams):
                tree.kill("output")


def _visible(text: str) -> str:
    """The text with every character a terminal would act on written out, so what is shown is what runs."""
    return "".join(char if char.isprintable() else char.encode("unicode_escape").decode("ascii") for char in text)


def _chat_of(chat_id: object) -> Optional[int]:
    """A lease's chat id, or None for anything but a number (a bool is an int)."""
    return chat_id if isinstance(chat_id, int) and not isinstance(chat_id, bool) else None


def _in_chat(chat_id: object) -> str:
    chat = _chat_of(chat_id)
    return f"  (chat #{chat})" if chat is not None else ""


def _command_label(command: object, chat_id: object) -> str:
    """One line naming a leased command and the chat it came from."""
    text = str(command or "").strip()
    label = _visible(text[:_SHOWN_COMMAND_CHARS])
    if len(text) > _SHOWN_COMMAND_CHARS:
        label += f" … (+{len(text) - _SHOWN_COMMAND_CHARS} more characters)"
    return label + _in_chat(chat_id)


def _whole_command(command: object) -> str:
    """All of a command, for its y/N question: written out as in a label, with each long run of spaces counted."""
    text = _visible(str(command or "").strip())
    return _SPACE_RUN.sub(lambda run: f"⟨{len(run.group())} spaces⟩", text)


def _seconds_to_answer(report_deadline: Optional[float], timeout: float) -> float:
    """How long the terminal has to say yes: a yes must leave the command its whole timeout before report_by."""
    # The poller's deadline, on time.monotonic and read against Skyportal's clock: this machine's can't move it.
    left = timeout if report_deadline is None else report_deadline - time.monotonic() - timeout
    return max(0.0, left - ANSWER_MARGIN_SECONDS)


def _not_run(reason: str) -> Dict[str, Any]:
    """The result of a leased command that never ran; a no at the terminal is the user's, so Skyportal records it so."""
    if reason in ("declined", "no_answer"):
        return {**_result(-1), "refused_by_user": reason}
    # Withdrawn: Skyportal no longer takes this result, so posting it changes nothing.
    return _result(-1, stderr=f"Not run on this machine: {_NOT_RUN_BECAUSE[reason]}.")


def _nobody_answers(_details: str, _prompt: str, _seconds: float) -> Optional[bool]:
    return None


def _session_without_redirects() -> requests.Session:
    # The shell's own client refuses redirects so the key is never sent on; this one does the same.
    session = requests.Session()
    session.max_redirects = 0
    return session


@dataclass
class LocalTarget:
    """This machine, connected as a command target for as long as this shell is open."""

    client: SkyportalClient
    folder: Path
    instance: str = field(default_factory=lambda: str(uuid.uuid4()))
    server_id: Optional[int] = None
    hostname: str = ""
    repo: Optional[RepoContext] = None
    poller_factory: Optional[Callable[..., Any]] = None
    # Where each command this machine runs is shown, as it starts and as it ends: a chat this CLI started
    # can be continued from the owner's browser, so nothing may run here without a trace in the terminal.
    show: Callable[[str], None] = logger.info
    # Asks the terminal y/N about a command from a turn this CLI didn't type: True or False as answered, None when
    # nobody answered in the seconds given. With no terminal to ask, nobody answers, so no such command runs.
    ask: Callable[[str, str, float], Optional[bool]] = _nobody_answers
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: List[threading.Thread] = field(default_factory=list)
    _runner: Optional[ShellRunner] = None
    # Skyportal removed this machine or refused this CLI's key: nothing more runs here.
    _ended: bool = False

    @property
    def connected(self) -> bool:
        return self.server_id is not None and not self._stop.is_set() and not self._ended

    @property
    def session(self) -> Dict[str, Any]:
        """What a chat is bound with: only this process may run its commands on this machine."""
        return {"server_id": self.server_id, "instance": self.instance}

    def describe(self) -> str:
        where = self.repo.label() if self.repo else str(self.folder)
        return f"{self.hostname} · {where}"

    def connect(self) -> None:
        """Register the machine and start running what Skyportal queues for this session."""
        # Deferred: importing the agent package (and wandb, if installed) would slow every start of the CLI.
        from skyportalai.agent.kubernetes.commands import CommandPoller

        self.repo = repo_context(self.folder)
        try:
            self._open()
            self._start(self.poller_factory or CommandPoller)
        except BaseException:  # a Ctrl-C too: a session opened here must not outlive the connect
            self.close()
            raise

    def _start(self, poller_factory: Callable[..., Any]) -> None:
        token = self.client.bearer_token()
        self._runner = ShellRunner(self.folder, self._stop)
        poller = poller_factory(
            self.client.base_url, token,
            session=_session_without_redirects(),
            session_factory=_session_without_redirects,
            run=self._run_leased,
            stop_event=self._stop,
            lease_path=LEASE_PATH.format(server_id=self.server_id),
            result_path=RESULT_PATH.format(server_id=self.server_id),
            instance=self.instance,
            extra_headers={"User-Agent": f"skyportalai-cli/{__version__}"},
            command_field=None,
            thread_prefix="local-command",
            on_gone=self._machine_removed,
            on_refused=self._key_refused,
            max_backoff_seconds=LEASE_BACKOFF_MAX_SECONDS,
            hand_report_deadline=True,
        )
        for name, target in (("local-command-poller", poller.run_forever), ("local-heartbeat", self._beat)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _run_leased(self, lease: object, timeout: float, report_deadline: Optional[float] = None) -> Dict[str, Any]:
        """Run one leased command, showing it as it starts and its exit code as it ends."""
        lease = lease if isinstance(lease, dict) else {}
        label = _command_label(lease.get("command"), lease.get("chat_id"))
        asked = lease.get("turn_origin") != self.instance
        refused = self._refusal_at_terminal(lease, timeout, report_deadline) or self._refusal_to_start(lease, asked)
        if refused is not None:
            self._tell(f"▸ not run ({_NOT_RUN_BECAUSE[refused]}): {label}")
            return _not_run(refused)
        self._tell(f"▸ running: {label}")
        try:
            result = self._runner(lease.get("command"), timeout, _chat_of(lease.get("chat_id")))
        except Exception as error:  # the chat waits for a result whatever went wrong here
            logger.exception("Could not run a leased command")
            result = _result(127, stderr=f"The CLI could not run this command: {error}")
        self._tell(f"▸ exit {result['exit_code']}: {label}")
        return result

    def _refusal_at_terminal(
        self, lease: Dict[str, Any], timeout: float, report_deadline: Optional[float],
    ) -> Optional[str]:
        """None to run it; why not when its turn wasn't typed here and nobody said yes."""
        origin = lease.get("turn_origin")
        if origin == self.instance:
            return None
        # The whole command, not the label's first line: a yes runs all of it.
        command = _whole_command(lease.get("command"))
        if len(command) > _ASKED_COMMAND_CHARS:
            return "too_long"
        where = "another skyportalai CLI session" if origin else "the browser, Slack or the API"
        details = command + _in_chat(lease.get("chat_id"))
        details += f"\nIts turn wasn't typed in this terminal: it came from {where}."
        seconds = _seconds_to_answer(report_deadline, timeout)
        answer = self.ask(details, f"Run this on {self.hostname}? [y/N] ", seconds) if seconds > 0 else None
        if answer is None:
            return "no_answer"
        return None if answer else "declined"

    def _refusal_to_start(self, lease: Dict[str, Any], asked: bool) -> Optional[str]:
        """None once Skyportal says the command may start; else why not. Its turn may have been stopped meanwhile."""
        try:
            self.client.start_local_command(self.server_id, self.instance, lease.get("id"))
        except PortalError as error:
            if error.code == "too_late":
                # After a question, a yes this late couldn't finish in time; nobody was asked about a turn typed here.
                return "no_answer" if asked else "too_late"
            return "withdrawn" if error.code == "withdrawn" else "unconfirmed"
        except Exception as error:  # noqa: BLE001 — what Skyportal didn't confirm never runs
            logger.warning("Could not confirm a leased command: %s", error)
            return "unconfirmed"
        return None

    def _tell(self, line: str) -> None:
        try:
            self.show(line)
        except Exception:  # noqa: BLE001 — a terminal that can't print must not cost the chat its result
            logger.warning("Could not show: %s", line, exc_info=True)

    def _open(self) -> None:
        connected = self.client.connect_local_machine(
            self.instance,
            machine_id=machine_id(),
            name=machine_name(),
            platform=platform_name(),
            shell=shell_name(),
            repo=self.repo.as_payload() if self.repo else None,
        )
        try:
            self.server_id = int(connected["server_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise PortalError("Skyportal did not return this machine's id") from error
        self.hostname = str(connected.get("hostname") or machine_name())

    def _machine_removed(self) -> None:
        self._end("▸ This machine was removed from Skyportal; no commands will run here. Reconnect with /local on.")

    def _key_refused(self) -> None:
        self._end("▸ Skyportal refused this CLI's key, so no commands will run here. Run /login, then /local on.")

    def _end(self, line: str) -> None:
        """Stop for good: out of the prompt, nothing more fetched, and one line saying why and what to do."""
        if self._ended:
            return
        self._ended = True
        self._tell(line)

    def _beat(self) -> None:
        """Tell Skyportal this CLI is alive while it runs a long command; open the session again if it was lost."""
        next_at = time.monotonic() + HEARTBEAT_SECONDS
        while not self._stop.wait(max(0.0, next_at - time.monotonic())) and not self._ended:
            next_at = time.monotonic() + HEARTBEAT_SECONDS
            try:
                self.client.local_machine_heartbeat(self.server_id, self.instance, timeout=HEARTBEAT_TIMEOUT_SECONDS)
            except PortalError as error:
                self._beat_failed(error)
            except Exception as error:  # noqa: BLE001 — a dropped connection must not end the heartbeat
                logger.warning("Heartbeat failed: %s", error)

    def _beat_failed(self, error: PortalError) -> None:
        if error.code == "session_closed":
            self._reopen()
        elif error.status_code == 410:
            self._machine_removed()
        elif error.status_code in (401, 403):
            self._key_refused()
        else:
            logger.warning("Heartbeat failed: %s", error)

    def _reopen(self) -> None:
        if self._stop.is_set():
            return
        try:
            self._open()
        except PortalError as error:
            logger.warning("Could not reconnect this machine: %s", error)

    def interrupt(self, chat_id: Optional[int] = None) -> int:
        """Ctrl-C: stop what runs here now, only that chat's commands if one is named; how many that was."""
        return self._runner.stop("interrupted", chat_id) if self._runner is not None else 0

    def close(self) -> None:
        """Stop running commands here and tell Skyportal, which withdraws whatever this session still held."""
        if self._stop.is_set():
            return
        self._stop.set()
        # Here, not on the workers' threads: the CLI may exit as soon as this returns, and they die with it.
        if self._runner is not None:
            self._runner.stop("closed")
        if self.server_id is None or self._ended:
            return
        try:
            self.client.disconnect_local_machine(self.server_id, self.instance)
        except Exception as error:  # noqa: BLE001 — whatever the network did, the CLI still exits
            # Skyportal withdraws them anyway once this session stops answering.
            logger.warning("Could not disconnect this machine: %s", error)
