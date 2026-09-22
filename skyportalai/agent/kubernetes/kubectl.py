"""Run read-only kubectl inside the cluster with the pod's ServiceAccount.

kubectl picks up the in-cluster config (the mounted ServiceAccount token and the
KUBERNETES_SERVICE_* environment) when no kubeconfig is present, so no
credential is ever written or read here. Every command is checked against the
read-only allowlist before a process is started, whatever the server asked for.
"""

from __future__ import annotations

import subprocess

# Mirrors _READONLY_SUBCOMMANDS in skyportal-website's readonly_kube.py. The agent
# enforces it itself: a compromised or buggy server must not be able to make this
# pod change the cluster.
READONLY_SUBCOMMANDS = frozenset({
    "get", "logs", "top", "describe",
    "api-resources", "api-versions", "version", "cluster-info", "explain",
})

DEFAULT_TIMEOUT_SECONDS = 30
# One command's output is capped so a runaway log can't blow the upload size limit.
MAX_OUTPUT_CHARS = 8 * 1024 * 1024

EXIT_REFUSED = 126
EXIT_NOT_FOUND = 127
EXIT_TIMEOUT = 124


def is_readonly(argv: list[str]) -> bool:
    """True iff argv is `kubectl <read-only subcommand> ...`."""
    return (
        isinstance(argv, list)
        and len(argv) >= 2
        and all(isinstance(arg, str) for arg in argv)
        and argv[0] == "kubectl"
        and argv[1] in READONLY_SUBCOMMANDS
    )


def run_kubectl(argv: list[str], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run argv and return {argv, exit_code, stdout, stderr}; refuses anything not read-only."""
    if not is_readonly(argv):
        return _result(argv, EXIT_REFUSED, "", "refused: the SkyPortal agent only runs read-only kubectl")
    try:
        # argv, never a shell string: nothing in it is interpreted by a shell.
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return _result(argv, EXIT_NOT_FOUND, "", "kubectl is not installed in the agent image")
    except subprocess.TimeoutExpired:
        return _result(argv, EXIT_TIMEOUT, "", f"kubectl did not finish within {timeout:g}s")
    return _result(argv, done.returncode, done.stdout, done.stderr)


def _result(argv: list[str], exit_code: int, stdout: str, stderr: str) -> dict:
    return {
        "argv": list(argv),
        "exit_code": exit_code,
        "stdout": (stdout or "")[:MAX_OUTPUT_CHARS],
        "stderr": (stderr or "")[:MAX_OUTPUT_CHARS],
    }
