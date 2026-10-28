"""Run SARA's commands on this machine, starting in the folder the user trusts (skyportal-website#3665)."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union
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
HEARTBEAT_PATH = "/api/v1/agent/local/{server_id}/heartbeat/"
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
# Outside ASCII, separators, format characters, marks and symbols are written out too, emoji with them: Python calls
# U+2800 (a symbol) and combining marks printable, yet they draw blank or over the character before, so padding of
# them could push the head of a command out of sight.
_DRAWN_BLANK_CATEGORIES = frozenset({"Zs", "Cf", "Mn", "Me", "So"})
# The Hangul fillers draw blank too but are letters, so they are named: every other letter is shown as typed.
_FILLERS = frozenset("\u115f\u1160\u3164\uffa0")
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


# The terminal's third answer: run it, and save the rules the lease offers in settings.local.json.
ALWAYS: Literal["always"] = "always"
# True or False as answered, ALWAYS, or None when nobody answered: `answer is True` tells a y from an `a`.
Answer = Optional[Union[bool, Literal["always"]]]

# Where skyportalai looks for a repository's rules; the server parses them, never this CLI.
REPO_SETTINGS_FILE = ".skyportalai/settings.json"
# Beside it and gitignored, as Claude Code's .claude/settings.local.json: this machine's own rules, where `a` saves,
# and the one allow list skyportalai lets skip the y/N. A repository could commit one; skyportalai accepts that.
LOCAL_SETTINGS_FILE = ".skyportalai/settings.local.json"
# The server refuses more: 50 rules of 500 characters per list fit many times over.
MAX_REPO_SETTINGS_BYTES = 64 * 1024
# The server reads a list longer than this as an unreadable file, which asks for everything.
MAX_RULES_PER_LIST = 50
# A Windows junction, which lstat doesn't call a link, carries this attribute as a symbolic link does.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def repo_settings(root: Path) -> Optional[Dict[str, str]]:
    """The repository's settings file as read, raw, or why it couldn't be; None when there is none."""
    return _settings_file(root, REPO_SETTINGS_FILE)


def local_settings(root: Path) -> Optional[Dict[str, str]]:
    """This machine's own rules beside it, read the same way."""
    return _settings_file(root, LOCAL_SETTINGS_FILE)


def _settings_file(root: Path, name: str) -> Optional[Dict[str, str]]:
    try:
        raw = _read_plain_file(root / name, MAX_REPO_SETTINGS_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        return {"file": name, "error": error.strerror or type(error).__name__}
    if len(raw) > MAX_REPO_SETTINGS_BYTES:
        return {"file": name, "error": f"larger than {MAX_REPO_SETTINGS_BYTES // 1024} KiB"}
    try:
        return {"file": name, "content": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"file": name, "error": "not UTF-8 text"}


def _refuse_a_link(path: Path, why: str) -> None:
    """Raise unless the path is no link: a repository could ship one to any file here, which connect would upload."""
    found = os.lstat(path)
    if stat.S_ISLNK(found.st_mode) or getattr(found, "st_file_attributes", 0) & _REPARSE_POINT:
        raise OSError(errno.ELOOP, why)


def _read_plain_file(path: Path, limit: int) -> bytes:
    """Up to `limit` bytes of a regular file reached through no link, in its own name or its folder's."""
    _refuse_a_link(path.parent, f"{path.parent.name} is a link, which isn't followed")
    _refuse_a_link(path, "a link, which isn't followed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags)
    except OSError as error:
        # Made a link since the check: O_NOFOLLOW refuses it.
        if error.errno == errno.ELOOP:
            raise OSError(errno.ELOOP, "a link, which isn't followed") from error
        raise
    with os.fdopen(handle, "rb") as file:
        # A FIFO or a device would hang the read or never end it; O_NONBLOCK opened it without waiting.
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        return file.read(limit)


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
    """The text with each character a terminal acts on or draws blank written out: what is shown is what runs."""
    return "".join(char.encode("unicode_escape").decode("ascii") if _written_out(char) else char for char in text)


def _written_out(char: str) -> bool:
    if not char.isprintable():
        return True
    return not char.isascii() and (unicodedata.category(char) in _DRAWN_BLANK_CATEGORIES or char in _FILLERS)


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
    # Not an exit code, which Skyportal takes only for a command it let start: it records why this one never ran.
    # Withdrawn: Skyportal no longer takes this result, so posting it changes nothing.
    return {**_result(-1, stderr=f"Not run on this machine: {_NOT_RUN_BECAUSE[reason]}."), "not_run": reason}


def add_allow_rules(root: Path, rules: List[str]) -> Optional[str]:
    """Add the rules to settings.local.json as it is on disk now, so no edit is lost; why not, if it can't."""
    name = LOCAL_SETTINGS_FILE
    read = local_settings(root)
    if read is not None and read.get("error"):
        return f"{name} couldn't be read ({read['error']})"
    data: Any = {}
    if read is not None and read["content"].strip():
        try:
            data = json.loads(read["content"])
        except ValueError:
            data = None
    # Never rewrite a file this CLI can't read as rules: skyportalai reports what is wrong with it.
    permissions = data.setdefault("permissions", {}) if isinstance(data, dict) else None
    allow = permissions.setdefault("allow", []) if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        return f"{name} isn't a settings file this CLI can add to"
    new = [rule for rule in rules if rule not in allow]
    if len(allow) + len(new) > MAX_RULES_PER_LIST:
        return f"{name} would hold more than {MAX_RULES_PER_LIST} allow rules, the most skyportalai reads"
    allow.extend(new)
    try:
        _replace_file(root / name, json.dumps(data, indent=2) + "\n")
    except OSError as error:
        return f"{name} couldn't be written ({error.strerror or type(error).__name__})"
    return None


def _replace_file(path: Path, text: str) -> None:
    """Write a temporary file beside it and rename that over it: never half a file, and never through a link."""
    path.parent.mkdir(exist_ok=True)
    _refuse_a_link(path.parent, f"{path.parent.name} is a link, which isn't followed")
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _local_allow_rules(root: Path) -> List[str]:
    """The allow rules in this machine's own file for the folder as it reads now; none if it can't be read as rules."""
    read = local_settings(root) or {}
    try:
        data = json.loads(read.get("content") or "null")
    except ValueError:
        return []
    permissions = data.get("permissions") if isinstance(data, dict) else None
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    return [rule for rule in allow if isinstance(rule, str)] if isinstance(allow, list) else []


@dataclass
class FolderRules:
    """The rules files the CLI sends for its folder: where they live, what was last sent, and what `a` adds."""

    # The repository's root, as for Claude Code's .claude/: a subfolder shares its repository's files. Outside a
    # repository, the folder itself.
    root: Path
    in_repository: bool = False
    # As last sent: skyportalai applies them to every command here, and the heartbeat sends a change.
    sent: Tuple[Optional[Dict[str, str]], Optional[Dict[str, str]]] = (None, None)

    @classmethod
    def of(cls, folder: Path) -> FolderRules:
        top = _git(folder, "rev-parse", "--show-toplevel")
        return cls(Path(top), in_repository=True) if top else cls(folder)

    @property
    def where(self) -> str:
        return "this repository" if self.in_repository else "this folder"

    def read(self) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, str]]]:
        return repo_settings(self.root), local_settings(self.root)

    def changed(self) -> bool:
        return self.read() != self.sent

    def allows(self, command: object) -> bool:
        """Whether this machine's own file, read now, holds this very command: `a` saves exact commands only."""
        return f"run_command({str(command or '').strip()})" in _local_allow_rules(self.root)

    def add(self, rules: List[str]) -> Optional[str]:
        """Save the rules for an `a`; why not, if they can't be."""
        return add_allow_rules(self.root, rules)

    def kept_out_of_git(self) -> bool:
        """False for a repository whose git doesn't ignore settings.local.json, so a commit would take it along."""
        return not self.in_repository or bool(_git(self.root, "check-ignore", "--", LOCAL_SETTINGS_FILE))


def _nobody_answers(_details: str, _prompt: str, _seconds: float) -> Answer:
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
    # Asks the terminal y/N about a command from a turn this CLI didn't type: True, False or ALWAYS as answered, None
    # when nobody answered in the seconds given. With no terminal to ask, nobody answers, so no such command runs.
    ask: Callable[[str, str, float], Answer] = _nobody_answers
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: List[threading.Thread] = field(default_factory=list)
    _runner: Optional[ShellRunner] = None
    # Skyportal removed this machine or refused this CLI's key: nothing more runs here.
    _ended: bool = False
    # The rules files skyportalai applies to every command here; connect finds the repository's root.
    _rules: FolderRules = field(init=False)

    def __post_init__(self) -> None:
        self._rules = FolderRules(self.folder)

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
        self._rules = FolderRules.of(self.folder)
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
            # The poller's beat while every slot is busy. _beat stays: only it opens a session Skyportal closed again,
            # notices a removed machine or a refused key while every slot is busy, and keeps a fixed cadence.
            heartbeat_path=HEARTBEAT_PATH.format(server_id=self.server_id),
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
        if origin == self.instance or self._allowed_here(lease):
            return None
        # The whole command, not the label's first line: a yes runs all of it.
        command = _whole_command(lease.get("command"))
        if len(command) > _ASKED_COMMAND_CHARS:
            return "too_long"
        where = "another skyportalai CLI session" if origin else "the browser, Slack or the API"
        details = command + _in_chat(lease.get("chat_id"))
        details += f"\nIts turn wasn't typed in this terminal: it came from {where}."
        rules = [rule for rule in lease.get("always_allow") or [] if isinstance(rule, str)]
        if rules:
            details += (
                f"\na = always: run it, and from now on run {', '.join(rules)} without asking in {self._rules.where},"
                f" saved in its {LOCAL_SETTINGS_FILE}."
            )
        choices = "[y/N/a=always]" if rules else "[y/N]"
        seconds = _seconds_to_answer(report_deadline, timeout)
        answer = self.ask(details, f"Run this on {self.hostname}? {choices} ", seconds) if seconds > 0 else None
        if answer is None:
            return "no_answer"
        if answer == ALWAYS and rules:
            self._allow_always(rules)
            return None
        return None if answer is True else "declined"

    def _allowed_here(self, lease: Dict[str, Any]) -> bool:
        """skyportalai let it through on this machine's own list, and the file `a` writes holds this very command."""
        if lease.get("asks_at_terminal") is not False:
            return False
        # The flag alone is the server's word, which a bug or a key holder sending its own list could clear for any
        # command.
        return self._rules.allows(lease.get("command"))

    def _allow_always(self, rules: List[str]) -> None:
        """Write the rules to this machine's allow list and send it at once: the next command mustn't ask again."""
        refused = self._rules.add(rules)
        if refused is not None:
            self._tell(f"▸ Couldn't add {', '.join(rules)}: {refused}. Running it this once.")
            return
        # Committed, the file would skip the question for whoever clones the repository.
        tracked = "" if self._rules.kept_out_of_git() else " git doesn't ignore it: add it to .gitignore."
        self._tell(f"▸ Added {', '.join(rules)} to {LOCAL_SETTINGS_FILE}.{tracked}")
        try:
            self._reopen()
        except Exception as error:  # noqa: BLE001 — the heartbeat sends it on its next beat
            logger.warning("Could not send the changed settings file: %s", error)

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
        settings, local = sent = self._rules.read()
        connected = self.client.connect_local_machine(
            self.instance,
            machine_id=machine_id(),
            name=machine_name(),
            platform=platform_name(),
            shell=shell_name(),
            repo=self.repo.as_payload() if self.repo else None,
            repo_settings=settings,
            local_settings=local,
        )
        try:
            self.server_id = int(connected["server_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise PortalError("Skyportal did not return this machine's id") from error
        self.hostname = str(connected.get("hostname") or machine_name())
        self._rules.sent = sent

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
            else:
                self._send_changed_settings()

    def _send_changed_settings(self) -> None:
        """An edited settings file reaches skyportalai within a heartbeat: connect again with it."""
        try:
            if self._rules.changed():
                self._reopen()
        except Exception as error:  # noqa: BLE001 — unsent, it is tried again on the next heartbeat
            logger.warning("Could not send the changed settings file: %s", error)

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
