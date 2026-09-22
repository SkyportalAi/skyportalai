"""Tests for the agent's Kubernetes roles (#3566).

The cluster role runs the server's plan of read-only kubectl and uploads the raw
output; the node role reads the host. Both go through the same spool and retrying
shipper as the experiment scanners. The agent enforces read-only itself.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import threading
from dataclasses import dataclass, field

import pytest

from skyportalai import SkyportalError
from skyportalai.agent import __main__ as agent_main
from skyportalai.agent.config import AgentConfig
from skyportalai.agent.kubernetes import (
    ClusterRole,
    CommandPoller,
    KubernetesRunner,
    KubernetesShipper,
    NodeRole,
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

    def post(self, url, data=None, json=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "data": data, "json": json, "headers": headers})
        return self.responses.pop(0) if self.responses else FakeResponse(202, {})


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

    def test_timeout_is_reported_not_raised(self, monkeypatch):
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(subprocess, "run", slow)
        assert kubectl.run_kubectl(GET_PODS, timeout=1)["exit_code"] == kubectl.EXIT_TIMEOUT


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


class TestNodeRole:
    def test_reports_host_metrics_and_gpus(self, monkeypatch, tmp_path):
        import psutil

        monkeypatch.setattr(psutil, "cpu_percent", lambda interval: 41.5)
        role = NodeRole("gpu-node-1", disk_path=tmp_path,
                        read_gpus=lambda: [{"index": 0, "name": "NVIDIA H100", "utilization_pct": 93}])
        node = role.collect()["node"]
        assert node["name"] == "gpu-node-1"
        assert node["cpu_usage_percent"] == 41.5
        assert node["memory_total_bytes"] > 0 and node["disk_total_bytes"] > 0
        assert node["gpus"][0]["name"] == "NVIDIA H100"

    def test_a_cpu_node_reports_no_gpus(self, monkeypatch):
        import psutil

        monkeypatch.setattr(psutil, "cpu_percent", lambda interval: 5.0)
        node = NodeRole("cpu-node", read_gpus=lambda: []).collect()["node"]
        assert node["gpus"] == [] and node["disk_total_bytes"] is None

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
    def _poller(self, session, run):
        return CommandPoller(BASE_URL, TOKEN, session=session, run=run, stop_event=threading.Event())

    def test_nothing_queued(self):
        session = FakeSession([FakeResponse(204)])
        assert self._poller(session, run=lambda argv: pytest.fail("ran")).poll_once() is False

    def test_runs_a_leased_command_and_posts_its_result(self):
        session = FakeSession([FakeResponse(200, {"id": 7, "argv": GET_PODS}), FakeResponse(200, {"recorded": True})])
        run = lambda argv: {"argv": argv, "exit_code": 0, "stdout": "trainer-0", "stderr": ""}  # noqa: E731
        assert self._poller(session, run).poll_once() is True
        result_call = session.calls[1]
        assert result_call["url"] == BASE_URL + "/agent/api/kubernetes/commands/7/result/"
        assert result_call["json"] == {"exit_code": 0, "stdout": "trainer-0", "stderr": ""}
        assert result_call["headers"]["Authorization"] == f"Bearer {TOKEN}"

    def test_a_mutating_command_from_the_server_is_refused_by_the_agent(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("process started"))
        session = FakeSession([FakeResponse(200, {"id": 8, "argv": ["kubectl", "delete", "ns", "prod"]}),
                               FakeResponse(200, {})])
        self._poller(session, run=kubectl.run_kubectl).poll_once()
        assert session.calls[1]["json"]["exit_code"] == kubectl.EXIT_REFUSED


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
        assert runner.shipper.url == "https://app.skyportal.ai/agent/api/kubernetes/ingest/"
