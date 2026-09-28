"""The shell's kubectl/helm steps for an agent token minted in chat (#3575)."""

import subprocess

from skyportalai.shell import agent_setup


def _recorder(monkeypatch, returncodes=None):
    calls = []
    codes = iter(returncodes or [])

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("input"), kwargs.get("shell", False)))
        code = next(codes, 0)
        stdout = "apiVersion: v1\nkind: Namespace\n" if "--dry-run=client" in argv else ""
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="boom" if code else "")

    monkeypatch.setattr(agent_setup.subprocess, "run", fake_run)
    return calls


def test_current_context(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="kind-x\n", stderr="")

    monkeypatch.setattr(agent_setup.subprocess, "run", fake_run)
    assert agent_setup.current_context() == "kind-x"


def test_create_secret_runs_the_issue_commands_with_the_token_on_stdin(monkeypatch):
    calls = _recorder(monkeypatch)
    assert agent_setup.create_secret("kind-x", "agt_SECRET") is None

    argvs = [argv for argv, _, _ in calls]
    assert argvs[0] == [
        "kubectl", "--context", "kind-x", "create", "namespace", "skyportal", "--dry-run=client", "-o", "yaml",
    ]
    assert argvs[1] == ["kubectl", "--context", "kind-x", "apply", "-f", "-"]
    assert calls[1][1].startswith("apiVersion")
    assert argvs[2] == [
        "kubectl", "--context", "kind-x", "label", "namespace", "skyportal",
        "pod-security.kubernetes.io/enforce=privileged", "--overwrite",
    ]
    assert argvs[3] == [
        "kubectl", "--context", "kind-x", "-n", "skyportal", "create", "secret", "generic",
        "skyportalai-agent-token", "--from-file=SKYPORTALAI_AGENT_TOKEN=/dev/stdin",
    ]
    assert calls[3][1] == "agt_SECRET"
    assert all("agt_SECRET" not in " ".join(argv) for argv in argvs)
    assert not any(shell for _, _, shell in calls)


def test_a_failing_step_reports_stderr_without_the_token(monkeypatch):
    _recorder(monkeypatch, returncodes=[0, 0, 0, 1])
    error = agent_setup.create_secret("kind-x", "agt_SECRET")
    assert error and "boom" in error and "agt_SECRET" not in error


def test_missing_kubectl(monkeypatch):
    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(agent_setup.subprocess, "run", missing)
    assert agent_setup.current_context() is None
    assert "kubectl" in agent_setup.create_secret("kind-x", "agt_SECRET")


def test_helm_install_uses_the_existing_secret_never_the_token(monkeypatch):
    calls = _recorder(monkeypatch)
    assert agent_setup.helm_install("kind-x", "prod-gpu-1") is None
    argv = calls[0][0]
    assert argv[:5] == ["helm", "upgrade", "--install", "skyportalai-agent", agent_setup.CHART]
    assert ["--version", agent_setup.CHART_VERSION] == argv[5:7]
    assert "token.existingSecret=skyportalai-agent-token" in argv
    assert "kubernetes.enabled=true" in argv
    assert argv[argv.index("config.clusterName=prod-gpu-1") - 1] == "--set-literal"
    assert calls[0][1] is None


def test_helm_install_passes_the_cluster_name_literally(monkeypatch):
    calls = _recorder(monkeypatch)
    agent_setup.helm_install("kind-x", "gpu,east=1")
    argv = calls[0][0]
    assert argv[argv.index("config.clusterName=gpu,east=1") - 1] == "--set-literal"


def test_manual_commands_use_the_chart_key_and_stdin():
    assert "--from-file=SKYPORTALAI_AGENT_TOKEN=/dev/stdin" in agent_setup.MANUAL_COMMANDS
    assert "--from-literal" not in agent_setup.MANUAL_COMMANDS
