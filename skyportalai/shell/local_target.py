"""Run SARA's commands on this machine, in this folder, the way Claude Code works (skyportal-website#3665).

The model runs on Skyportal's side and nothing there connects into this machine. When the
shell starts in a folder the user trusts, it registers the machine, opens a session for this
process and leases the commands Skyportal queues for that session over outbound HTTPS, the
same loop the in-cluster agent uses. Each one runs here, in this folder, and its output goes
back to the chat.

The CLI only executes. It decides nothing and asks nothing a second time: the approval card
the shell already shows is the prompt, and Skyportal's gate decides what reaches the queue.
What leaves this machine about it: a random machine id, its name, its OS and shell, and the
repository's remote and branch with the folder's path inside it. Never an absolute path.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
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
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

from skyportalai import _env
from skyportalai._version import __version__
from skyportalai.agent.kubernetes.commands import CommandPoller

from .portal import PortalError, SkyportalClient

# The shell configures no logging; without a handler, a failed poll would print over the prompt.
logging.getLogger("skyportalai").addHandler(logging.NullHandler())
logger = logging.getLogger(__name__)

LEASE_PATH = "/api/v1/agent/local/{server_id}/commands/lease/"
RESULT_PATH = "/api/v1/agent/local/{server_id}/commands/{{id}}/result/"
# Skyportal takes a CLI silent for 40 s as closed and withdraws its commands, so this stays well inside it.
HEARTBEAT_SECONDS = 15
# Kept per stream: the head and the tail, where a failing build says why. The model reads less than this.
OUTPUT_HEAD_BYTES = 30_000
OUTPUT_TAIL_BYTES = 70_000
TIMEOUT_EXIT_CODE = 124
STOPPED_EXIT_CODE = 130
_STOPPED_BECAUSE = {
    "closed": "Stopped: the skyportalai CLI was closed.",
    "interrupted": "Stopped: interrupted with Ctrl-C in the skyportalai CLI.",
}
_GIT_SECONDS = 5
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


class TrustStore:
    """The folders the user said SARA may run commands in. A trusted folder covers everything below it."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _env.config_path("trusted_folders.json", "SKYPORTALAI_TRUSTED_FOLDERS_PATH")

    def _folders(self) -> List[str]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        folders = data.get("folders") if isinstance(data, dict) else None
        return [str(folder) for folder in folders] if isinstance(folders, list) else []

    def is_trusted(self, folder: Path) -> bool:
        folder = folder.resolve()
        for trusted in self._folders():
            try:
                folder.relative_to(Path(trusted))
            except ValueError:
                continue
            return True
        return False

    def trust(self, folder: Path) -> None:
        folders = self._folders()
        resolved = str(folder.resolve())
        if resolved not in folders:
            folders.append(resolved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"folders": folders}, indent=2))
        self.path.chmod(0o600)


def _git(folder: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(folder), *args],
            capture_output=True, text=True, timeout=_GIT_SECONDS, check=False, stdin=subprocess.DEVNULL,
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
        parsed = urlparse(url)
        if parsed.scheme == "file" or not parsed.hostname:
            return ""
        host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
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


def _windows_bash(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Git for Windows' bash, which is not on PATH in PowerShell or Windows Terminal; None without Git."""
    bash = which("bash")
    # System32's bash is WSL: another filesystem, where this folder isn't.
    if bash and "system32" not in bash.lower():
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
    """A command's process and everything it starts, so stopping it stops them all.

    POSIX: a session of its own, killed as a process group. Windows: a job object, because Git
    Bash's children aren't Windows children of bash.exe and taskkill /T leaves them running.
    """

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
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)], capture_output=True, check=False,
                    )
                else:
                    os.killpg(self.process.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                self.process.kill()
        self.process.wait()

    def close(self) -> None:
        with self._lock:
            if self._job is not None:
                # What it left running in the background outlives it, as it does on POSIX.
                _kill_on_close(self._job, False)
                _kernel32.CloseHandle(self._job)
                self._job = None


class ShellRunner:
    """Run one leased command in this folder, for its timeout at most, and report what it printed."""

    def __init__(self, folder: Path, stop: threading.Event, *, shell: Optional[List[str]] = None):
        self.folder = folder
        self._stop = stop
        self._shell = shell or _shell_argv()
        self._running: set = set()
        self._lock = threading.Lock()

    def stop_all(self, because: str) -> int:
        """Stop every command running now, on the caller's thread; how many there were."""
        with self._lock:
            running = list(self._running)
        for tree in running:
            tree.kill(because)
        return len(running)

    def __call__(self, command: object, timeout: float) -> Dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return {"exit_code": 126, "stdout": "", "stderr": "The CLI was sent no command to run."}
        deadline = time.monotonic() + timeout
        # Files, not pipes: a chatty command can't fill a pipe and hang, or fill this process's memory.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                tree = _ProcessTree(
                    [*self._shell, command], cwd=self.folder, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                )
            except OSError as error:
                return {"exit_code": 127, "stdout": "", "stderr": f"Could not start {self._shell[0]}: {error}"}
            with self._lock:
                self._running.add(tree)
            try:
                self._wait(tree, deadline)
            finally:
                with self._lock:
                    self._running.discard(tree)
                tree.close()
            stdout = _read_bounded(out)
            stderr = _read_bounded(err)
        exit_code = tree.process.returncode
        if tree.stopped_because == "timeout":
            exit_code = TIMEOUT_EXIT_CODE
            stderr += f"\nStopped after its {int(timeout)}s timeout."
        elif tree.stopped_because in _STOPPED_BECAUSE:
            exit_code = STOPPED_EXIT_CODE
            stderr += "\n" + _STOPPED_BECAUSE[tree.stopped_because]
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}

    def _wait(self, tree: _ProcessTree, deadline: float) -> None:
        while True:
            try:
                tree.process.wait(timeout=0.5)
                return
            except subprocess.TimeoutExpired:
                pass
            if self._stop.is_set():
                tree.kill("closed")
            elif time.monotonic() >= deadline:
                tree.kill("timeout")


def _session_without_redirects() -> requests.Session:
    # The shell's own client refuses redirects so the key is never sent on; this one does the same.
    session = requests.Session()
    session.max_redirects = 0
    return session


@dataclass
class LocalTarget:
    """This machine, connected as a command target for as long as the shell runs in this folder."""

    client: SkyportalClient
    folder: Path
    instance: str = field(default_factory=lambda: str(uuid.uuid4()))
    server_id: Optional[int] = None
    hostname: str = ""
    repo: Optional[RepoContext] = None
    poller_factory: Callable[..., Any] = CommandPoller
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: List[threading.Thread] = field(default_factory=list)
    _runner: Optional[ShellRunner] = None

    @property
    def connected(self) -> bool:
        return self.server_id is not None and not self._stop.is_set()

    @property
    def session(self) -> Dict[str, Any]:
        """What a chat is bound with: only this process may run its commands on this machine."""
        return {"server_id": self.server_id, "instance": self.instance}

    def describe(self) -> str:
        where = self.repo.label() if self.repo else str(self.folder)
        return f"{self.hostname} · {where}"

    def connect(self) -> None:
        """Register the machine and start running what Skyportal queues for this session; PortalError if it can't."""
        self.repo = repo_context(self.folder)
        self._open()
        token = self.client.bearer_token()
        self._runner = ShellRunner(self.folder, self._stop)
        poller = self.poller_factory(
            self.client.base_url, token,
            session=_session_without_redirects(),
            session_factory=_session_without_redirects,
            run=self._runner,
            stop_event=self._stop,
            lease_path=LEASE_PATH.format(server_id=self.server_id),
            result_path=RESULT_PATH.format(server_id=self.server_id),
            extra_headers={"User-Agent": f"skyportalai-cli/{__version__}", "X-Agent-Instance": self.instance},
            command_field="command",
            thread_prefix="local-command",
        )
        for name, target in (("local-command-poller", poller.run_forever), ("local-heartbeat", self._beat)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

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

    def _beat(self) -> None:
        """Tell Skyportal this CLI is alive while it runs a long command; open the session again if it was lost."""
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                self.client.local_machine_heartbeat(self.server_id, self.instance)
            except PortalError as error:
                if error.code == "session_closed":
                    self._reopen()
                else:
                    logger.warning("Heartbeat failed: %s", error)

    def _reopen(self) -> None:
        try:
            self._open()
        except PortalError as error:
            logger.warning("Could not reconnect this machine: %s", error)

    def interrupt(self) -> int:
        """Ctrl-C: stop what is running here now; how many commands that was."""
        return self._runner.stop_all("interrupted") if self._runner is not None else 0

    def close(self) -> None:
        """Stop running commands here and tell Skyportal, which withdraws whatever this session still held."""
        if self._stop.is_set():
            return
        self._stop.set()
        # Here, not on the workers' threads: the CLI may exit as soon as this returns, and they die with it.
        if self._runner is not None:
            self._runner.stop_all("closed")
        if self.server_id is None:
            return
        try:
            self.client.disconnect_local_machine(self.server_id, self.instance)
        except PortalError as error:
            # Skyportal withdraws them anyway once this session stops answering.
            logger.warning("Could not disconnect this machine: %s", error)
