"""The node role reads its own kubelet's /stats/summary for per-pod usage (#3588)."""

from __future__ import annotations

import io
import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from skyportalai.agent import __main__ as agent_main
from skyportalai.agent.config import AgentConfig
from skyportalai.agent.kubernetes import KubeletClient, NodeRole
from skyportalai.agent.kubernetes import kubelet as kubelet_mod

TOKEN = "agt_" + "x" * 40
SUMMARY = json.dumps({"node": {"nodeName": "n1"}, "pods": [{"podRef": {"name": "api-0", "namespace": "prod"}}]})


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _Response(self.outcome)


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("sa-token-1\n")
    return path


def _client(monkeypatch, outcome, token_file, **kwargs):
    opener = _Opener(outcome)
    monkeypatch.setattr(kubelet_mod.urllib.request, "build_opener", lambda *handlers: opener)
    return KubeletClient("10.0.0.7", 10250, token_file=token_file, **kwargs), opener


class TestKubeletClient:
    def test_returns_the_summary_body_raw(self, monkeypatch, token_file):
        client, _ = _client(monkeypatch, SUMMARY.encode(), token_file)
        assert client.stats_summary(max_chars=1_000_000) == (SUMMARY, None)

    def test_calls_its_own_node_with_the_service_account_token(self, monkeypatch, token_file):
        client, opener = _client(monkeypatch, b"{}", token_file)
        client.stats_summary(max_chars=100)
        (request,) = opener.requests
        assert request.full_url == "https://10.0.0.7:10250/stats/summary"
        assert request.get_header("Authorization") == "Bearer sa-token-1"

    def test_rereads_the_token_every_call_because_it_rotates(self, monkeypatch, token_file):
        client, opener = _client(monkeypatch, b"{}", token_file)
        client.stats_summary(max_chars=100)
        token_file.write_text("sa-token-2")
        client.stats_summary(max_chars=100)
        assert opener.requests[-1].get_header("Authorization") == "Bearer sa-token-2"

    def test_brackets_an_ipv6_node_address(self):
        assert KubeletClient("fd00::7", 10250).url == "https://[fd00::7]:10250/stats/summary"

    def test_an_http_error_is_a_reason_not_a_raise(self, monkeypatch, token_file):
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        client, _ = _client(monkeypatch, err, token_file)
        assert client.stats_summary(max_chars=100) == (None, "HTTP 403")

    def test_a_tls_failure_names_itself(self, monkeypatch, token_file):
        err = urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))
        client, _ = _client(monkeypatch, err, token_file)
        body, reason = client.stats_summary(max_chars=100)
        assert body is None and reason.startswith("TLS")

    def test_an_oversized_summary_is_refused_not_truncated(self, monkeypatch, token_file):
        client, _ = _client(monkeypatch, b"x" * 101, token_file)
        assert client.stats_summary(max_chars=100) == (None, "EFBIG")

    def test_no_token_means_no_request(self, monkeypatch, tmp_path):
        client, opener = _client(monkeypatch, b"{}", tmp_path / "missing")
        body, reason = client.stats_summary(max_chars=100)
        assert body is None and reason.startswith("token unreadable")
        assert opener.requests == []

    def test_a_malformed_ca_file_is_a_reason_not_a_raise(self, tmp_path, token_file):
        # Review (CodeRabbit): the TLS context was built before the try, so a CA file holding
        # garbage raised out of stats_summary and the node lost that whole cycle's upload.
        bad_ca = tmp_path / "ca.crt"
        bad_ca.write_text("-----BEGIN CERTIFICATE-----\nnot base64 at all\n-----END CERTIFICATE-----\n")
        client = KubeletClient("10.0.0.7", 10250, ca_file=bad_ca, token_file=token_file)
        body, reason = client.stats_summary(max_chars=100)
        assert body is None and reason.startswith("TLS")

    def test_never_follows_a_redirect(self):
        handler = kubelet_mod._NoRedirect()
        assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere/") is None

    def test_verifies_tls_by_default(self):
        context = KubeletClient("10.0.0.7", 10250)._tls_context()
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

    def test_skips_verification_only_when_asked(self):
        context = KubeletClient("10.0.0.7", 10250, insecure_skip_verify=True)._tls_context()
        assert context.verify_mode == ssl.CERT_NONE


class _StubKubelet:
    def __init__(self, body=None, reason=None):
        self.body, self.reason = body, reason

    def stats_summary(self, max_chars):
        return self.body, self.reason


class TestNodeRoleKubelet:
    def _role(self, tmp_path, kubelet):
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "stat").write_text("cpu 1 2 3 4\n")
        smi = {"argv": ["nvidia-smi"], "exit_code": 127, "stdout": "", "stderr": ""}
        return NodeRole("n1", host_proc=proc, run=lambda argv: smi, kubelet=kubelet)

    def test_uploads_the_summary_raw_keyed_by_its_path(self, tmp_path):
        node = self._role(tmp_path, _StubKubelet(body=SUMMARY)).collect()["node"]
        assert node["kubelet"] == {"/stats/summary": SUMMARY}

    def test_a_kubelet_failure_is_named_and_the_proc_upload_still_happens(self, tmp_path):
        node = self._role(tmp_path, _StubKubelet(reason="HTTP 403")).collect()["node"]
        assert node["kubelet"] == {}
        assert node["unreadable"]["kubelet:/stats/summary"] == "HTTP 403"
        assert node["files"]["/proc/stat"].startswith("cpu ")

    def test_without_a_kubelet_client_nothing_is_attempted(self, tmp_path):
        node = self._role(tmp_path, None).collect()["node"]
        assert node["kubelet"] == {}
        assert not any(k.startswith("kubelet:") for k in node["unreadable"])


class TestConfigAndWiring:
    def _config(self, **env):
        return AgentConfig.from_env({
            "SKYPORTALAI_AGENT_TOKEN": TOKEN, "SKYPORTALAI_AGENT_ROLE": "node",
            "SKYPORTALAI_AGENT_NODE_NAME": "n1", **env,
        })

    def test_reads_the_kubelet_settings(self):
        config = self._config(
            SKYPORTALAI_AGENT_HOST_IP="10.0.0.7", SKYPORTALAI_AGENT_KUBELET_PORT="10255",
            SKYPORTALAI_AGENT_KUBELET_CA_FILE="/etc/kubelet-ca/ca.crt",
            SKYPORTALAI_AGENT_KUBELET_INSECURE_SKIP_VERIFY="true",
        )
        assert (config.host_ip, config.kubelet_port) == ("10.0.0.7", 10255)
        assert config.kubelet_ca_file == Path("/etc/kubelet-ca/ca.crt")
        assert config.kubelet_insecure_skip_verify is True

    def test_defaults_are_the_secure_port_and_verification(self):
        config = self._config()
        assert (config.host_ip, config.kubelet_port, config.kubelet_insecure_skip_verify) == (None, 10250, False)

    def test_the_node_runner_reads_the_kubelet_only_when_it_knows_the_node_ip(self):
        with_ip = agent_main.build_kubernetes_runner(self._config(SKYPORTALAI_AGENT_HOST_IP="10.0.0.7"))
        without = agent_main.build_kubernetes_runner(self._config())
        assert with_ip.role._kubelet.url == "https://10.0.0.7:10250/stats/summary"
        assert without.role._kubelet is None
