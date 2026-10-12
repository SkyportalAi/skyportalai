"""The agent stands alone: no CLI code, and nothing that can change a cluster (#3657).

`pip install "skyportalai[agent]"` is meant to be the agent and nothing else. These
tests keep the code honest about that; CI's agent-only job proves it against an
install that really has no CLI packages.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import subprocess
import sys

import pytest

AGENT = pathlib.Path(__file__).resolve().parents[1] / "skyportalai" / "agent"
CLI_ONLY = ("typer", "rich", "click", "prompt_toolkit", "pydantic", "skyportalai.cli", "skyportalai.shell")
# wandb (the agent extra) itself requires click and pydantic, so a running agent may
# load those; the agent's own code still never imports them (checked statically).
NOT_LOADED_BY_AGENT = ("typer", "rich", "prompt_toolkit", "skyportalai.cli", "skyportalai.shell")


def _modules():
    for path in sorted(AGENT.rglob("*.py")):
        package = ".".join(("skyportalai", *path.relative_to(AGENT.parent).with_suffix("").parts))
        if path.name != "__init__.py":
            package = package.rpartition(".")[0]
        yield path, package, ast.parse(path.read_text(encoding="utf-8"))


def _imports(package: str, tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            else:
                base = node.module or ""
            yield base


def _is_cli(name: str) -> bool:
    return any(name == banned or name.startswith(banned + ".") for banned in CLI_ONLY)


@pytest.mark.parametrize("path,package,tree", list(_modules()), ids=lambda v: getattr(v, "name", ""))
def test_agent_code_imports_nothing_from_the_cli(path, package, tree):
    assert [name for name in _imports(package, tree) if _is_cli(name)] == []


def test_running_the_agent_loads_no_cli_module():
    probe = (
        "import sys, skyportalai.agent.__main__\n"
        f"loaded = [m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in {NOT_LOADED_BY_AGENT!r})]\n"
        "print(loaded); sys.exit(1 if loaded else 0)"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr


def test_agent_help_exits_zero_without_config():
    done = subprocess.run(
        [sys.executable, "-m", "skyportalai.agent", "--help"], capture_output=True, text=True, check=False,
        env={"PATH": ""},
    )
    assert done.returncode == 0, done.stderr
    assert "SKYPORTALAI_AGENT_TOKEN" in done.stdout


# Every place the agent starts a process, and what it may run. A new one has to be
# added here on purpose, with a reason it can't change the cluster.
PROCESS_SPAWNERS = {
    "kubernetes/kubectl.py",  # run_kubectl's read-only allowlist; run_process for fixed argv
    "scrapers/mlflow_scanner.py",  # find, to locate mlruns directories
    "scrapers/wandb_scanner.py",  # find, to locate wandb directories
}


def test_only_known_modules_start_processes():
    spawners = set()
    for path, _package, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (
                (node.value.id == "subprocess" and node.attr in {"run", "Popen", "call", "check_call", "check_output"})
                or (node.value.id == "os" and node.attr in {"system", "popen"} | {a for a in dir(__import__("os")) if a.startswith(("exec", "spawn"))})
            ):
                spawners.add(path.relative_to(AGENT).as_posix())
    assert spawners == PROCESS_SPAWNERS


def test_unchecked_process_runner_only_runs_nvidia_smi():
    """run_process skips the kubectl allowlist, so its only caller must pass a constant."""
    from skyportalai.agent.kubernetes import node

    users = {
        path.relative_to(AGENT).as_posix()
        for path, _package, tree in _modules()
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "run_process"
    }
    assert users == {"kubernetes/kubectl.py", "kubernetes/node.py"}
    assert node.NVIDIA_SMI == ["nvidia-smi", "-q", "-x"]
