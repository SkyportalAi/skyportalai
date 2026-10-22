"""Tests for the agent's Kubernetes roles (#3566).

The cluster role runs the server's plan of read-only kubectl and uploads the raw
output; the node role reads the host. Both go through the same spool and retrying
shipper as the experiment scanners. The agent enforces read-only itself.
"""

from __future__ import annotations

import errno
import gzip
import json
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from skyportalai import SkyportalError
from skyportalai.agent import __main__ as agent_main
from skyportalai.agent.config import AgentConfig
from skyportalai.agent.kubernetes import (
    ClusterRole,
    CommandPoller,
    KubernetesRunner,
    KubernetesShipper,
    NodeRole,
    commands,
    kubectl,
)
from skyportalai.agent.queue import SpoolQueue

BASE_URL = "https://skyportal.test"
TOKEN = "agt_test"
GET_PODS = ["kubectl", "get", "pods", "-A", "-o", "json"]


@dataclass
class FakeResponse:
    status_code: int
    body: dict | None = None

    def json(self):
        if self.body is None:
            raise ValueError("no body")
        return self.body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self) -> None:
        pass


@dataclass
class FakeSession:
    responses: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    closed: int = 0

    def post(self, url, data=None, json=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "data": data, "json": json, "headers": headers})
        reply = self.responses.pop(0) if self.responses else FakeResponse(202, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self) -> None:
        self.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class RecordingStop(threading.Event):
    """A stop event whose wait() records the delay and adds it to `now` instead of sleeping.

    It sets itself on wait number `stop_on`. Hand `clock` to the poller as its clock.
    """

    def __init__(self, stop_on: int | None = None):
        super().__init__()
        self.waits: list[float] = []
        self.now = 1000.0
        self._stop_on = stop_on

    def clock(self) -> float:
        return self.now

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.now += timeout or 0
        if len(self.waits) == self._stop_on:
            self.set()
        return self.is_set()


class CountingStop(threading.Event):
    """A stop event that counts how often it is looked at, so a test can wait for the poller's loop to go round."""

    def __init__(self):
        super().__init__()
        self.checks = 0
        self._checked = threading.Condition()

    def is_set(self) -> bool:
        with self._checked:
            self.checks += 1
            self._checked.notify_all()
        return super().is_set()

    def wait_for_checks(self, count: int) -> bool:
        with self._checked:
            return self._checked.wait_for(lambda: self.checks >= count, timeout=5)


class FakeCommandServer:
    """SkyPortal's command endpoints for several threads: `leases` in order, then 204; every result is taken.

    busy_at_lease holds, for each lease request, how many leased commands had no result yet.
    """

    def __init__(self, leases=()):
        self.leases = list(leases)
        self.busy_at_lease: list[int] = []
        self.results: dict[int, dict] = {}
        self._leased = 0
        self._changed = threading.Condition()

    @property
    def lease_calls(self) -> int:
        return len(self.busy_at_lease)

    def post(self, url, json=None, headers=None, timeout=None):
        with self._changed:
            if url == BASE_URL + commands.LEASE_PATH:
                self.busy_at_lease.append(self._leased - len(self.results))
                if not self.leases:
                    return FakeResponse(204)
                self._leased += 1
                return FakeResponse(200, self.leases.pop(0))
            self.results[int(url.split("/")[-3])] = json
            self._changed.notify_all()
            return FakeResponse(200, {"recorded": True})

    def wait_for_results(self, count: int) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: len(self.results) >= count, timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


def _report_by(seconds_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat()


def _sent_body(call: dict) -> dict:
    return json.loads(gzip.decompress(call["data"]))


class TestKubectl:
    @pytest.mark.parametrize("argv", [
        ["kubectl", "get", "pods"],
        ["kubectl", "logs", "-n", "ml", "trainer-0", "--previous"],
        ["kubectl", "top", "nodes"],
    ])
    def test_read_only_commands_are_allowed(self, argv):
        assert kubectl.is_readonly(argv)

    @pytest.mark.parametrize("argv", [
        ["kubectl", "delete", "pod", "x"],
        ["kubectl", "exec", "x", "--", "sh"],
        ["kubectl", "apply", "-f", "x.yaml"],
        ["helm", "list"],
        ["sh", "-c", "kubectl get pods"],
        ["kubectl"],
        "kubectl get pods",
    ])
    def test_anything_else_is_refused(self, argv):
        assert not kubectl.is_readonly(argv)

    def test_a_refused_command_never_starts_a_process(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("process started"))
        result = kubectl.run_kubectl(["kubectl", "delete", "pod", "trainer-0"])
        assert result["exit_code"] == kubectl.EXIT_REFUSED
        assert "read-only" in result["stderr"]

    def test_runs_argv_without_a_shell(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(argv=argv, shell=kwargs.get("shell", False))
            return subprocess.CompletedProcess(argv, 0, stdout="pods", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = kubectl.run_kubectl(GET_PODS)
        assert seen == {"argv": GET_PODS, "shell": False}
        assert (result["exit_code"], result["stdout"]) == (0, "pods")

    def test_oversized_output_is_a_failure_not_a_truncated_success(self, monkeypatch):
        monkeypatch.setattr(kubectl, "MAX_OUTPUT_CHARS", 10)
        monkeypatch.setattr(subprocess, "run",
                            lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout='{"items": [1, 2, 3]}', stderr=""))
        result = kubectl.run_kubectl(GET_PODS)
        assert result["exit_code"] == kubectl.EXIT_TOO_LARGE
        assert result["stdout"] == ""

    def test_timeout_is_reported_not_raised(self, monkeypatch):
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(subprocess, "run", slow)
        assert kubectl.run_kubectl(GET_PODS, timeout=1)["exit_code"] == kubectl.EXIT_TIMEOUT

    def test_invalid_utf8_in_output_is_replaced_not_raised(self, monkeypatch):
        # Through the real decoder: a crash log's stray byte must not fail the cycle.
        real_run = subprocess.run
        emit = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok\\n\\xff\\xfe crash\\n')"]
        monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: real_run(emit, **kwargs))
        result = kubectl.run_kubectl(["kubectl", "logs", "-n", "ml", "trainer-0", "--previous"])
        assert result["exit_code"] == 0
        assert result["stdout"].startswith("ok\n") and "\ufffd" in result["stdout"]


class TestClusterRole:
    def test_a_new_agent_uploads_no_commands_and_asks_again_soon(self):
        role = ClusterRole(run=lambda argv: pytest.fail("ran with no plan"))
        assert role.collect()["commands"] == []
        assert role.next_delay(30) == 5

    def test_runs_exactly_the_servers_plan(self):
        ran = []
        role = ClusterRole(run=lambda argv: ran.append(argv) or {"argv": argv, "exit_code": 0, "stdout": "", "stderr": ""})
        role.apply_reply({"plan": [GET_PODS, ["kubectl", "top", "nodes", "--no-headers"]]})
        payload = role.collect()
        assert ran == [GET_PODS, ["kubectl", "top", "nodes", "--no-headers"]]
        assert payload["role"] == "cluster" and payload["schema_version"] == 1
        assert role.next_delay(30) == 30

    def test_drops_a_mutating_command_from_the_servers_plan(self):
        role = ClusterRole()
        role.apply_reply({"plan": [GET_PODS, ["kubectl", "delete", "ns", "prod"]]})
        assert role.plan == [GET_PODS]

    def test_a_reply_without_a_plan_keeps_the_current_one(self):
        role = ClusterRole()
        role.apply_reply({"plan": [GET_PODS]})
        role.apply_reply({"interval_seconds": 30})
        assert role.plan == [GET_PODS]


PROC_STAT = "cpu  2255 34 2290 22625563 6290 127 456 0 0 0\ncpu0 1132 34 1441 11311718 3675 127 438 0 0 0\nctxt 1990473\n"
MEMINFO = "MemTotal:       16303428 kB\nMemFree:          227532 kB\nMemAvailable:    8742056 kB\n"


def _fake_host_proc(root: Path) -> Path:
    (root / "1" / "net").mkdir(parents=True)
    (root / "stat").write_text(PROC_STAT)
    (root / "meminfo").write_text(MEMINFO)
    (root / "loadavg").write_text("3.21 2.87 2.40 5/1234 99887\n")
    (root / "1" / "net" / "dev").write_text("eth0: 8812312 1234 0 0 0 0 0 0 99123 456 0 0 0 0 0 0\n")
    # Per-process data must never be read, whatever else is under /proc.
    (root / "1" / "environ").write_text("DB_PASSWORD=hunter2\0")
    return root


class TestNodeRole:
    def _role(self, tmp_path, **kwargs):
        smi = {"argv": ["nvidia-smi", "-q", "-x"], "exit_code": 0, "stdout": "<nvidia_smi_log/>", "stderr": ""}
        return NodeRole("gpu-node-1", host_proc=_fake_host_proc(tmp_path / "proc"), run=lambda argv: smi, **kwargs)

    def test_sends_the_proc_files_byte_for_byte(self, tmp_path):
        files = self._role(tmp_path).collect()["node"]["files"]
        assert files["/proc/stat"] == PROC_STAT
        assert files["/proc/meminfo"] == MEMINFO
        assert files["/proc/1/net/dev"].startswith("eth0:")

    def test_never_reads_per_process_data(self, tmp_path):
        node = self._role(tmp_path).collect()["node"]
        assert "/proc/1/environ" not in node["files"]
        assert "hunter2" not in json.dumps(node)

    def test_names_what_it_could_not_read(self, tmp_path):
        # Absent here, like /proc/pressure on a kernel older than 4.20.
        unreadable = self._role(tmp_path).collect()["node"]["unreadable"]
        assert unreadable["/proc/pressure/cpu"] == "ENOENT"
        assert "/proc/stat" not in unreadable

    def test_sends_the_whole_statvfs_of_the_disk(self, tmp_path):
        spool = tmp_path / "spool"
        spool.mkdir()
        statvfs = self._role(tmp_path, disk_path=spool).collect()["node"]["statvfs"][str(spool)]
        assert {"f_blocks", "f_bfree", "f_bavail", "f_frsize", "f_files", "f_ffree"} <= statvfs.keys()

    def test_sends_nvidia_smis_full_report(self, tmp_path):
        (command,) = self._role(tmp_path).collect()["node"]["commands"]
        assert command["argv"] == ["nvidia-smi", "-q", "-x"] and command["stdout"] == "<nvidia_smi_log/>"

    def test_a_node_without_nvidia_smi_reports_it_missing(self):
        result = kubectl.run_process(["skyportal-no-such-binary", "-q"])
        assert result["exit_code"] == kubectl.EXIT_NOT_FOUND
        assert "skyportal-no-such-binary is not installed" in result["stderr"]

    def test_derives_nothing(self, tmp_path):
        node = self._role(tmp_path).collect()["node"]
        assert set(node) == {"name", "files", "statvfs", "commands", "unreadable"}

    def test_requires_a_node_name(self):
        with pytest.raises(ValueError):
            NodeRole("")


class TestKubernetesRunner:
    def _runner(self, tmp_path, session, role):
        shipper = KubernetesShipper(BASE_URL, TOKEN, session=session, sleep=lambda s: None)
        return KubernetesRunner(role, SpoolQueue(tmp_path), shipper, interval_seconds=60)

    def test_uploads_one_cycle_as_one_request_and_applies_the_reply(self, tmp_path):
        session = FakeSession([FakeResponse(202, {"plan": [GET_PODS], "interval_seconds": 30})])
        role = ClusterRole()
        runner = self._runner(tmp_path, session, role)
        runner.run_once()

        (call,) = session.calls
        assert call["url"] == BASE_URL + "/agent/api/kubernetes/ingest/"
        body = _sent_body(call)
        assert body["role"] == "cluster" and body["commands"] == [] and body["batch_id"]
        assert role.plan == [GET_PODS]
        assert runner.interval_seconds == 30
        assert SpoolQueue(tmp_path).is_empty()

    def test_an_outage_is_spooled_and_delivered_in_order(self, tmp_path):
        session = FakeSession([FakeResponse(503)] * 3 + [FakeResponse(202, {}), FakeResponse(202, {})])
        runner = self._runner(tmp_path, session, NodeRoleStub())
        runner.run_once()
        assert len(SpoolQueue(tmp_path)) == 1
        runner.run_once()
        assert SpoolQueue(tmp_path).is_empty()
        delivered = [_sent_body(c)["collected_at"] for c in session.calls[-2:]]
        assert delivered == sorted(delivered)


    def test_an_upload_refused_permanently_is_dropped_so_later_ones_still_ship(self, tmp_path):
        session = FakeSession([FakeResponse(413), FakeResponse(202, {})])
        runner = self._runner(tmp_path, session, NodeRoleStub())
        runner.queue.enqueue([{"stuck": True}])  # an earlier upload SkyPortal will refuse
        runner.run_once()
        assert SpoolQueue(tmp_path).is_empty()
        assert len(session.calls) == 2

    def test_a_transient_failure_keeps_the_upload(self, tmp_path):
        session = FakeSession([FakeResponse(503)] * 3)
        runner = self._runner(tmp_path, session, NodeRoleStub())
        runner.run_once()
        assert len(SpoolQueue(tmp_path)) == 1

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refused_token_keeps_every_queued_upload(self, tmp_path, status):
        # 401/403 are about the token (or a WAF in front of the API), not the upload: a
        # fixed token must still be able to deliver the backlog.
        session = FakeSession([FakeResponse(status)] * 5)
        runner = self._runner(tmp_path, session, NodeRoleStub())
        runner.queue.enqueue([{"buffered": True}])
        runner.run_once()
        assert len(SpoolQueue(tmp_path)) == 2
        assert len(session.calls) == 1

    def test_a_failed_collection_still_ships_the_backlog(self, tmp_path):
        session = FakeSession([FakeResponse(202, {})])
        runner = self._runner(tmp_path, session, FailingRole())
        runner.queue.enqueue([{"buffered": True}])
        runner.run_once()
        assert SpoolQueue(tmp_path).is_empty()
        assert _sent_body(session.calls[0]) == {"buffered": True}

    def test_a_full_spool_volume_still_ships_the_backlog(self, tmp_path, monkeypatch):
        session = FakeSession([FakeResponse(202, {})])
        runner = self._runner(tmp_path, session, NodeRoleStub())
        runner.queue.enqueue([{"buffered": True}])

        def disk_full(runs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(runner.queue, "enqueue", disk_full)
        runner.run_once()
        assert SpoolQueue(tmp_path).is_empty()

    def test_a_failing_backlog_is_read_one_upload_at_a_time(self, tmp_path, monkeypatch):
        # A long outage's backlog must not be loaded whole: the pod's memory limit is small.
        runner = self._runner(tmp_path, FakeSession([FakeResponse(503)] * 3), NodeRoleStub())
        for n in range(3):
            runner.queue.enqueue([{"n": n}])
        reads = []
        original = SpoolQueue._read_batch
        monkeypatch.setattr(SpoolQueue, "_read_batch", lambda self, path: reads.append(path) or original(self, path))
        runner.shipper.ship(runner.queue)
        assert len(reads) == 1


class FailingRole:
    kind = "cluster"

    def collect(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    def apply_reply(self, reply):
        pass

    def next_delay(self, interval):
        return interval


class NodeRoleStub:
    kind = "node"
    _n = 0

    def collect(self):
        NodeRoleStub._n += 1
        return {"schema_version": 1, "role": "node", "collected_at": f"2026-09-22T10:00:{NodeRoleStub._n:02d}",
                "node": {"name": "n1", "gpus": []}}

    def apply_reply(self, reply):
        pass

    def next_delay(self, interval):
        return interval


class TestCommandPoller:
    def _poller(self, session, run, stop_event=None):
        """A poller whose leases and result posts all go to `session`, in order."""
        return CommandPoller(
            BASE_URL, TOKEN, session=session, session_factory=lambda: session, run=run,
            stop_event=stop_event or threading.Event(),
        )

    def _handle(self, poller) -> bool:
        """poll_once, then wait for the command it started; True if it started one."""
        worker = poller.poll_once()
        if worker is None:
            return False
        worker.join(5)
        assert not worker.is_alive()
        return True

    def test_nothing_queued(self):
        session = FakeSession([FakeResponse(204)])
        assert self._poller(session, run=lambda argv, timeout: pytest.fail("ran")).poll_once() is None

    def test_every_lease_and_result_says_the_agent_honors_lease_timeouts(self):
        # The server gives a 30 s lease to an agent that doesn't say so (skyportal-website#3709).
        def run(argv, timeout):
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        session = FakeSession([FakeResponse(200, {"id": 7, "argv": GET_PODS}), FakeResponse(200, {"recorded": True})])
        assert self._handle(self._poller(session, run)) is True
        assert [call["headers"]["X-Agent-Capabilities"] for call in session.calls] == ["command-timeouts"] * 2

    def test_runs_a_leased_command_on_its_own_thread_and_posts_its_result(self):
        ran_on = []

        def run(argv, timeout):
            ran_on.append(threading.current_thread().name)
            return {"argv": argv, "exit_code": 0, "stdout": "trainer-0", "stderr": ""}

        session = FakeSession([FakeResponse(200, {"id": 7, "argv": GET_PODS}), FakeResponse(200, {"recorded": True})])
        assert self._handle(self._poller(session, run)) is True
        assert ran_on == ["kube-command-7"]
        result_call = session.calls[1]
        assert result_call["url"] == BASE_URL + "/agent/api/kubernetes/commands/7/result/"
        assert result_call["json"] == {"exit_code": 0, "stdout": "trainer-0", "stderr": ""}
        assert result_call["headers"]["Authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.parametrize(
        "reply", [FakeResponse(200, {"recorded": True}), requests.TooManyRedirects("loop")], ids=["posted", "raised"]
    )
    def test_each_command_posts_its_result_on_a_session_of_its_own(self, reply):
        lease_session = FakeSession([FakeResponse(200, {"id": n, "argv": GET_PODS}) for n in (7, 8)])
        worker_sessions = []

        def new_session():
            worker_sessions.append(FakeSession([reply]))
            return worker_sessions[-1]

        def run(argv, timeout):
            return {"argv": argv, "exit_code": 0, "stdout": "", "stderr": ""}

        poller = CommandPoller(
            BASE_URL, TOKEN, session=lease_session, session_factory=new_session, run=run, stop_event=threading.Event()
        )
        assert self._handle(poller) is True and self._handle(poller) is True
        assert [call["url"] for call in lease_session.calls] == [BASE_URL + commands.LEASE_PATH] * 2
        assert lease_session.closed == 0
        assert len({id(lease_session), *map(id, worker_sessions)}) == 3
        for command_id, session in zip((7, 8), worker_sessions, strict=True):
            (call,) = session.calls
            assert call["url"] == BASE_URL + f"/agent/api/kubernetes/commands/{command_id}/result/"
            assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
            assert session.closed == 1

    def test_a_result_is_posted_with_no_more_output_than_the_server_keeps(self):
        # The server stores the first MiB of each stream; 2 MiB of stdout goes out as that first MiB.
        head = commands.MAX_COMMAND_OUTPUT_CHARS
        assert head == 1024 * 1024

        def run(argv, timeout):
            return {"argv": argv, "exit_code": 3, "stdout": "a" * head + "b" * head, "stderr": "e" * (head + 1)}

        session = FakeSession([FakeResponse(200, {"id": 7, "argv": GET_PODS}), FakeResponse(200, {})])
        assert self._handle(self._poller(session, run)) is True
        posted = session.calls[1]["json"]
        assert posted.keys() == {"exit_code", "stdout", "stderr"} and posted["exit_code"] == 3
        # Compared by length and letters so a failure doesn't print a MiB.
        assert (len(posted["stdout"]), set(posted["stdout"])) == (head, {"a"})
        assert (len(posted["stderr"]), set(posted["stderr"])) == (head, {"e"})

    def test_a_mutating_command_from_the_server_is_refused_by_the_agent(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("process started"))
        session = FakeSession([FakeResponse(200, {"id": 8, "argv": ["kubectl", "delete", "ns", "prod"]}),
                               FakeResponse(200, {})])
        assert self._handle(self._poller(session, run=kubectl.run_kubectl)) is True
        assert session.calls[1]["json"]["exit_code"] == kubectl.EXIT_REFUSED

    def test_a_lease_without_argv_is_answered_refused(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("process started"))
        session = FakeSession([FakeResponse(200, {"id": 9}), FakeResponse(200, {})])
        assert self._handle(self._poller(session, run=kubectl.run_kubectl)) is True
        assert session.calls[1]["url"] == BASE_URL + "/agent/api/kubernetes/commands/9/result/"
        assert session.calls[1]["json"]["exit_code"] == kubectl.EXIT_REFUSED

    @pytest.mark.parametrize("body", [{"argv": GET_PODS}, {"id": "x", "argv": GET_PODS}, {}])
    def test_a_lease_without_a_usable_id_is_ignored(self, body):
        session = FakeSession([FakeResponse(200, body)])
        assert self._poller(session, run=lambda argv, timeout: pytest.fail("ran")).poll_once() is None
        assert len(session.calls) == 1

    def _timeout_used(self, **lease):
        seen = []

        def run(argv, timeout):
            seen.append(timeout)
            return {"argv": argv, "exit_code": 0, "stdout": "", "stderr": ""}

        session = FakeSession([FakeResponse(200, {"id": 7, "argv": GET_PODS, **lease}), FakeResponse(200, {})])
        assert self._handle(self._poller(session, run)) is True
        (timeout,) = seen
        return timeout

    @pytest.mark.parametrize("sent", [600, 90.5, 1800, 3600, 86400], ids=["int", "float", "30 min", "1 h", "1 day"])
    def test_the_leases_timeout_is_what_the_command_runs_under(self, sent):
        # The server owns the limit, so the agent doesn't cut it down.
        assert self._timeout_used(timeout_seconds=sent) == sent

    def test_a_lease_without_a_timeout_keeps_the_default(self):
        # An older server sends none: the command keeps the 30 s it always had.
        assert self._timeout_used() == kubectl.DEFAULT_TIMEOUT_SECONDS == 30

    @pytest.mark.parametrize("sent", [None, 0, 0.0, -5, True, False, "600", [600], float("nan"), float("inf")])
    def test_an_unusable_timeout_keeps_the_default(self, sent):
        assert self._timeout_used(timeout_seconds=sent) == kubectl.DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.parametrize(
        ("lease", "expected"), [({"timeout_seconds": 600}, 600), ({"timeout_seconds": 3600}, 3600), ({}, 30)]
    )
    def test_the_kubectl_process_gets_the_leases_timeout(self, monkeypatch, lease, expected):
        # The bug: run_kubectl killed every chat command at its own 30 s default.
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="trainer-0", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        session = FakeSession([FakeResponse(200, {"id": 7, "argv": GET_PODS, **lease}), FakeResponse(200, {})])
        assert self._handle(self._poller(session, run=kubectl.run_kubectl)) is True
        assert seen["timeout"] == expected
        assert session.calls[1]["json"]["stdout"] == "trainer-0"

    def _posting(self, *replies, stop=None, runs_for=0, **lease):
        """A poller on a fake clock that leases command 7 (plus `lease`) and meets `replies` posting its result."""
        session = FakeSession([FakeResponse(200, {"id": 7, "argv": GET_PODS, **lease}), *replies])
        stop = stop or RecordingStop()

        def run(argv, timeout):
            stop.now += runs_for
            return {"argv": argv, "exit_code": 0, "stdout": "secret-stdout", "stderr": ""}

        poller = CommandPoller(
            BASE_URL, TOKEN, session=session, session_factory=lambda: session, run=run, stop_event=stop,
            clock=stop.clock,
        )
        return poller, session, stop

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_a_result_rejected_for_now_is_sent_again(self, status):
        poller, session, stop = self._posting(FakeResponse(status), FakeResponse(200, {"recorded": True}))
        assert self._handle(poller) is True
        _lease, first, second = session.calls
        assert first["url"] == second["url"] == BASE_URL + "/agent/api/kubernetes/commands/7/result/"
        assert first["json"] == second["json"] == {"exit_code": 0, "stdout": "secret-stdout", "stderr": ""}
        assert stop.waits == [commands.RESULT_RETRY_DELAYS[0]]

    @pytest.mark.parametrize(
        "error", [requests.ConnectionError("reset"), requests.Timeout("slow")], ids=["connection", "timeout"]
    )
    def test_a_result_that_could_not_reach_skyportal_is_sent_again(self, error):
        poller, session, stop = self._posting(error, FakeResponse(200, {"recorded": True}))
        assert self._handle(poller) is True
        assert len(session.calls) == 3
        assert stop.waits == [commands.RESULT_RETRY_DELAYS[0]]

    @pytest.mark.parametrize("with_report_by", [False, True], ids=["no report_by", "time left"])
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
    def test_a_result_the_server_refuses_is_not_sent_again(self, caplog, status, with_report_by):
        lease = {"report_by": _report_by(600)} if with_report_by else {}
        poller, session, stop = self._posting(FakeResponse(status), FakeResponse(200, {}), **lease)
        with caplog.at_level(logging.WARNING, logger=commands.logger.name):
            assert self._handle(poller) is True
        assert len(session.calls) == 2
        assert stop.waits == []
        (message,) = [record.getMessage() for record in caplog.records]
        assert "command 7" in message and f"HTTP {status}" in message
        assert "secret-stdout" not in message and "pods" not in message

    @pytest.mark.parametrize(
        ("last", "reason"),
        [(FakeResponse(503), "HTTP 503"), (requests.ConnectionError("reset"), "ConnectionError")],
        ids=["status", "connection"],
    )
    def test_a_result_that_never_gets_through_is_given_up_on(self, caplog, last, reason):
        # A lease without report_by, as an older server sends it: the fixed schedule, about 77 s.
        attempts = len(commands.RESULT_RETRY_DELAYS) + 1
        failures = [FakeResponse(503)] * (attempts - 1) + [last]
        poller, session, stop = self._posting(*failures, FakeResponse(200, {}))
        with caplog.at_level(logging.WARNING, logger=commands.logger.name):
            assert self._handle(poller) is True
        assert len(session.calls) == 1 + attempts
        assert stop.waits == list(commands.RESULT_RETRY_DELAYS)
        (message,) = [record.getMessage() for record in caplog.records]
        assert "command 7" in message and reason in message
        assert "secret-stdout" not in message and "pods" not in message

    @pytest.mark.parametrize(
        "report_by", [None, "", "soon", "2026-10-14T18:05:00", 1760465100],
        ids=["null", "empty", "garbled", "no utc offset", "a number"],
    )
    def test_an_unusable_report_by_keeps_the_fixed_schedule(self, report_by):
        attempts = len(commands.RESULT_RETRY_DELAYS) + 1
        poller, session, stop = self._posting(*[FakeResponse(503)] * (attempts + 3), report_by=report_by)
        assert self._handle(poller) is True
        assert len(session.calls) == 1 + attempts
        assert stop.waits == list(commands.RESULT_RETRY_DELAYS)

    def test_a_result_is_sent_again_until_the_report_by_deadline(self, caplog):
        # Past the fixed 77 s: a minute apart once the listed waits run out, and the last wait ends on the deadline.
        poller, session, stop = self._posting(*[FakeResponse(503)] * 20, report_by=_report_by(230))
        with caplog.at_level(logging.WARNING, logger=commands.logger.name):
            assert self._handle(poller) is True
        assert stop.waits[:7] == [2, 5, 10, 20, 40, 60, 60]
        assert len(stop.waits) == 8
        assert sum(stop.waits) == pytest.approx(230, abs=1)
        assert len(session.calls) == 1 + 1 + len(stop.waits)
        (message,) = [record.getMessage() for record in caplog.records]
        assert "command 7" in message and "HTTP 503" in message
        assert "secret-stdout" not in message and "pods" not in message

    @pytest.mark.parametrize(
        ("report_in", "runs_for"), [(-5, 0), (60, 100)], ids=["already past", "passed while the command ran"]
    )
    def test_a_result_past_its_report_by_is_posted_once(self, caplog, report_in, runs_for):
        poller, session, stop = self._posting(
            FakeResponse(503), FakeResponse(200, {}), runs_for=runs_for, report_by=_report_by(report_in)
        )
        with caplog.at_level(logging.WARNING, logger=commands.logger.name):
            assert self._handle(poller) is True
        assert len(session.calls) == 2
        assert stop.waits == []
        (message,) = [record.getMessage() for record in caplog.records]
        assert "command 7" in message and "HTTP 503" in message

    @pytest.mark.parametrize("with_report_by", [False, True], ids=["no report_by", "time left"])
    def test_a_shutdown_during_a_retry_wait_stops_the_retries(self, caplog, with_report_by):
        lease = {"report_by": _report_by(600)} if with_report_by else {}
        failures = [FakeResponse(503)] * 5
        poller, session, stop = self._posting(*failures, stop=RecordingStop(stop_on=2), **lease)
        with caplog.at_level(logging.WARNING, logger=commands.logger.name):
            assert self._handle(poller) is True
        assert len(session.calls) == 1 + 2
        assert stop.waits == list(commands.RESULT_RETRY_DELAYS[:2])
        assert any("command 7" in record.getMessage() for record in caplog.records)

    def test_poll_once_asks_for_nothing_while_every_slot_is_busy(self):
        release = threading.Event()

        def run(argv, timeout):
            release.wait(5)
            return {"argv": argv, "exit_code": 0, "stdout": "", "stderr": ""}

        slots = commands.COMMAND_SLOTS
        server = FakeCommandServer([{"id": n, "argv": GET_PODS} for n in range(1, slots + 2)])
        poller = self._poller(server, run)
        try:
            busy = [poller.poll_once() for _ in range(slots)]
            assert poller.poll_once() is None
            assert server.lease_calls == slots
        finally:
            release.set()
        for worker in busy:
            worker.join(5)
        assert self._handle(poller) is True
        assert sorted(server.results) == list(range(1, slots + 2))

    def test_four_commands_run_at_once_and_the_next_waits_for_a_slot(self, monkeypatch):
        monkeypatch.setattr(commands, "POLL_SECONDS", 0.01)
        slots = commands.COMMAND_SLOTS
        # This test is the extra party, so the barrier only opens with a command running in every slot.
        all_running, release = threading.Barrier(slots + 1, timeout=5), threading.Event()

        def run(argv, timeout):
            if argv == GET_PODS:
                all_running.wait()
                release.wait(5)
            return {"argv": argv, "exit_code": 0, "stdout": "ok", "stderr": ""}

        server = FakeCommandServer(
            [{"id": n, "argv": GET_PODS} for n in range(1, slots + 1)] + [{"id": 99, "argv": ["kubectl", "version"]}]
        )
        stop = CountingStop()
        loop = threading.Thread(target=self._poller(server, run, stop).run_forever, daemon=True)
        loop.start()
        try:
            all_running.wait()
            # The loop keeps going round (so it would see a shutdown) but asks for no fifth command.
            assert stop.wait_for_checks(stop.checks + 3)
            assert server.lease_calls == slots
            release.set()
            assert server.wait_for_results(slots + 1)
        finally:
            release.set()
            stop.set()
            loop.join(5)
        assert sorted(server.results) == [*range(1, slots + 1), 99]
        # Nor at any other point while every slot held a command with no result yet.
        assert max(server.busy_at_lease) < slots

    def test_a_command_that_raises_frees_its_slot(self, caplog):
        def boom(argv, timeout):
            raise OSError(errno.ENOMEM, "Cannot allocate memory")

        slots = commands.COMMAND_SLOTS
        server = FakeCommandServer([{"id": n, "argv": GET_PODS} for n in range(1, slots + 2)])
        poller = self._poller(server, boom)
        with caplog.at_level(logging.ERROR, logger=commands.logger.name):
            for _ in range(slots + 1):
                assert self._handle(poller) is True
        assert server.lease_calls == slots + 1 and server.results == {}
        logged = [record.getMessage() for record in caplog.records if record.name == commands.logger.name]
        assert logged == [f"Command {n} failed" for n in range(1, slots + 2)]

    def test_a_shutdown_stops_leasing_without_waiting_for_running_commands(self, monkeypatch):
        monkeypatch.setattr(commands, "POLL_SECONDS", 0.01)
        workers, started, release = [], threading.Event(), threading.Event()

        def run(argv, timeout):
            workers.append(threading.current_thread())
            started.set()
            release.wait(5)
            return {"argv": argv, "exit_code": 0, "stdout": "done", "stderr": ""}

        server = FakeCommandServer([{"id": 7, "argv": GET_PODS}])
        stop = threading.Event()
        poller = self._poller(server, run, stop)
        loop = threading.Thread(target=poller.run_forever, daemon=True)
        loop.start()
        try:
            assert started.wait(5)
            stop.set()
            loop.join(5)
            assert not loop.is_alive()
            (worker,) = workers
            assert worker.is_alive() and worker.daemon
            leased = server.lease_calls
            assert poller.poll_once() is None and server.lease_calls == leased
        finally:
            release.set()
        worker.join(5)
        assert server.results == {7: {"exit_code": 0, "stdout": "done", "stderr": ""}}

    def test_the_poller_survives_a_command_that_raises(self):
        # /healthz can't see this thread; if it died, chat's kubectl would time out for good.
        def boom(argv, timeout):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        session = FakeSession([FakeResponse(200, {"id": 1, "argv": GET_PODS})] + [FakeResponse(204)] * 50)
        poller = self._poller(session, run=boom)
        thread = threading.Thread(target=poller.run_forever, daemon=True)
        thread.start()
        thread.join(0.3)
        alive = thread.is_alive()
        poller._stop.set()
        thread.join(5)
        assert alive

    @pytest.mark.parametrize("status", [404, 401, 403, 410])
    def test_a_refused_or_gone_lease_is_a_failed_poll_the_in_cluster_agent_keeps_retrying(self, status):
        session = FakeSession([FakeResponse(status)] * 3)
        poller = self._poller(session, run=lambda argv, timeout: pytest.fail("ran"))

        assert [poller.poll_once() for _ in range(3)] == [None, None, None]
        assert len(session.calls) == 3

    def _hooked(self, session, monkeypatch, **hooks):
        monkeypatch.setattr("skyportalai.agent.kubernetes.commands.POLL_SECONDS", 0.01)
        return CommandPoller(
            BASE_URL, TOKEN, session=session, session_factory=lambda: session,
            run=lambda argv, timeout: pytest.fail("ran"), stop_event=threading.Event(), **hooks,
        )

    def _runs_to_its_end(self, poller):
        thread = threading.Thread(target=poller.run_forever, daemon=True)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()

    def test_a_410_lease_calls_on_gone_once_and_ends_polling(self, monkeypatch):
        called = []
        session = FakeSession([FakeResponse(410, {"code": "machine_removed"})] * 5)
        poller = self._hooked(
            session, monkeypatch, on_gone=lambda: called.append("gone"), on_refused=lambda: called.append("refused"),
        )

        self._runs_to_its_end(poller)

        assert called == ["gone"]
        assert len(session.calls) == 1
        assert poller.poll_once() is None and len(session.calls) == 1

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refused_lease_calls_on_refused_once_and_ends_polling(self, monkeypatch, status):
        called = []
        session = FakeSession([FakeResponse(status)] * 5)
        poller = self._hooked(
            session, monkeypatch, on_gone=lambda: called.append("gone"), on_refused=lambda: called.append("refused"),
        )

        self._runs_to_its_end(poller)

        assert called == ["refused"]
        assert len(session.calls) == 1

    def test_a_404_lease_never_reads_as_gone_even_with_the_hooks_set(self):
        session = FakeSession([FakeResponse(404)] * 3)
        called = []
        poller = CommandPoller(
            BASE_URL, TOKEN, session=session, session_factory=lambda: session,
            run=lambda argv, timeout: pytest.fail("ran"), stop_event=threading.Event(),
            on_gone=lambda: called.append("gone"), on_refused=lambda: called.append("refused"),
        )

        assert [poller.poll_once() for _ in range(3)] == [None, None, None]
        assert called == [] and len(session.calls) == 3

    @staticmethod
    def _waits_between_polls(responses, **options):
        """The delays the poller asks for between polls that find nothing, one per response."""
        session = FakeSession(list(responses))
        stop = RecordingStop(stop_on=len(responses))
        poller = CommandPoller(
            BASE_URL, TOKEN, session=session, session_factory=lambda: session,
            run=lambda argv, timeout: pytest.fail("ran"), stop_event=stop, **options,
        )
        poller.run_forever()
        return stop.waits

    def test_failed_polls_back_off_up_to_the_cap_and_a_good_one_resets_it(self):
        responses = [FakeResponse(503)] * 6 + [FakeResponse(204), FakeResponse(503)]
        connection_lost = requests.ConnectionError("reset")

        waits = self._waits_between_polls(responses + [connection_lost], max_backoff_seconds=30)

        assert waits == [2, 4, 8, 16, 30, 30, 2, 2, 4]

    def test_without_a_backoff_the_wait_between_polls_is_always_the_same(self):
        waits = self._waits_between_polls([FakeResponse(503)] * 4 + [FakeResponse(204)])

        assert waits == [2] * 5


class TestConfigAndEntrypoint:
    def test_role_defaults_to_the_experiment_scanners(self):
        assert AgentConfig.from_env({"SKYPORTALAI_AGENT_TOKEN": TOKEN}).role == "experiments"

    def test_unknown_role_is_refused(self):
        with pytest.raises(SkyportalError):
            AgentConfig.from_env({"SKYPORTALAI_AGENT_TOKEN": TOKEN, "SKYPORTALAI_AGENT_ROLE": "everything"})

    def test_node_role_needs_the_node_name(self):
        with pytest.raises(SkyportalError):
            AgentConfig.from_env({"SKYPORTALAI_AGENT_TOKEN": TOKEN, "SKYPORTALAI_AGENT_ROLE": "node"})

    @pytest.mark.parametrize(("role", "expected"), [("cluster", ClusterRole), ("node", NodeRole)])
    def test_builds_the_runner_for_the_role(self, tmp_path, role, expected):
        config = AgentConfig.from_env({
            "SKYPORTALAI_AGENT_TOKEN": TOKEN,
            "SKYPORTALAI_AGENT_ROLE": role,
            "SKYPORTALAI_AGENT_NODE_NAME": "n1",
            "SKYPORTALAI_AGENT_STATE_DIR": str(tmp_path),
        })
        runner = agent_main.build_kubernetes_runner(config)
        assert isinstance(runner.role, expected)
        assert runner.queue.max_bytes == 512 * 1024 * 1024
        assert runner.shipper.url == "https://app.skyportal.ai/agent/api/kubernetes/ingest/"

    @pytest.mark.parametrize("role", ["cluster", "node"])
    def test_refuses_a_plain_http_base_url(self, tmp_path, role):
        config = self._config(tmp_path, role, "http://skyportal.example")
        with pytest.raises(SkyportalError, match="non-HTTPS"):
            agent_main.build_kubernetes_runner(config)

    def test_allows_plain_http_to_loopback(self, tmp_path):
        config = self._config(tmp_path, "cluster", "http://127.0.0.1:8000")
        assert agent_main.build_kubernetes_runner(config).shipper.url.startswith("http://127.0.0.1:8000/")

    def test_allows_plain_http_when_explicitly_insecure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKYPORTALAI_ALLOW_INSECURE", "1")
        config = self._config(tmp_path, "cluster", "http://skyportal.internal:8000")
        with pytest.warns(UserWarning, match="ALLOW_INSECURE"):
            agent_main.build_kubernetes_runner(config)

    def _config(self, tmp_path, role, base_url):
        return AgentConfig.from_env({
            "SKYPORTALAI_AGENT_TOKEN": TOKEN,
            "SKYPORTALAI_AGENT_ROLE": role,
            "SKYPORTALAI_AGENT_NODE_NAME": "n1",
            "SKYPORTALAI_AGENT_STATE_DIR": str(tmp_path),
            "SKYPORTALAI_BASE_URL": base_url,
        })
