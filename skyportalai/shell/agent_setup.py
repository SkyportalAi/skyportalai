"""Create the in-cluster agent's Secret, and optionally install its chart, for a token minted in chat.

The token only ever travels on a child process's stdin: never argv, where it
would show in the process list and in a failed command's error text.
"""

from __future__ import annotations

import subprocess
from typing import List, Optional, Union

NAMESPACE = "skyportal"
SECRET_NAME = "skyportalai-agent-token"
SECRET_KEY = "SKYPORTALAI_AGENT_TOKEN"
CHART = "oci://ghcr.io/skyportalai/charts/skyportalai-agent"
# Where this pin should live is still open (skyportal-website#3575, out of scope);
# keep it on a chart version that is published on GHCR.
CHART_VERSION = "0.3.2"
_TIMEOUT_SECONDS = 120

MANUAL_COMMANDS = "\n".join([
    "kubectl create namespace skyportal --dry-run=client -o yaml | kubectl apply -f -",
    "kubectl label namespace skyportal pod-security.kubernetes.io/enforce=privileged --overwrite",
    "read -rs AGT   # paste the token; it is not echoed or kept in history",
    (
        "printf '%s' \"$AGT\" | kubectl -n skyportal create secret generic skyportalai-agent-token "
        "--from-file=SKYPORTALAI_AGENT_TOKEN=/dev/stdin"
    ),
    "unset AGT",
])

_Result = Union["subprocess.CompletedProcess[str]", str]


def current_context() -> Optional[str]:
    """The kubectl context the Secret would land in, or None when kubectl can't say."""
    result = _run(["kubectl", "config", "current-context"])
    if isinstance(result, str) or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def create_secret(context: str, token: str) -> Optional[str]:
    """Create the namespace, its Pod Security label and the Secret; None on success, else the error."""
    kubectl = ["kubectl", "--context", context]
    # The `create namespace --dry-run | kubectl apply` pipe, as two processes and no shell.
    manifest = _run(kubectl + ["create", "namespace", NAMESPACE, "--dry-run=client", "-o", "yaml"])
    if isinstance(manifest, str) or manifest.returncode != 0:
        return _error(manifest)
    steps = (
        (kubectl + ["apply", "-f", "-"], manifest.stdout),
        (kubectl + ["label", "namespace", NAMESPACE, "pod-security.kubernetes.io/enforce=privileged", "--overwrite"],
         None),
        (kubectl + ["-n", NAMESPACE, "create", "secret", "generic", SECRET_NAME, f"--from-file={SECRET_KEY}=/dev/stdin"],
         token),
    )
    for argv, stdin in steps:
        result = _run(argv, stdin)
        if isinstance(result, str) or result.returncode != 0:
            return _error(result)
    return None


def helm_install(context: str, cluster_name: str) -> Optional[str]:
    """Install the agent chart against the Secret created above; the token is never passed with --set."""
    result = _run([
        "helm", "upgrade", "--install", "skyportalai-agent", CHART, "--version", CHART_VERSION,
        "--kube-context", context, "-n", NAMESPACE,
        "--set", f"token.existingSecret={SECRET_NAME}",
        "--set", "kubernetes.enabled=true",
        # A cluster name can hold commas or look like a number; --set would split or retype it.
        "--set-literal", f"config.clusterName={cluster_name}",
    ])
    if isinstance(result, str) or result.returncode != 0:
        return _error(result)
    return None


def _run(argv: List[str], stdin: Optional[str] = None) -> _Result:
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError:
        return f"{argv[0]} is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return f"{argv[0]} did not finish within {_TIMEOUT_SECONDS}s"


def _error(result: _Result) -> str:
    if isinstance(result, str):
        return result
    return (result.stderr or result.stdout or f"exit code {result.returncode}").strip()[:2000]
