"""End-to-end coverage of the console script users actually run.

Every other CLI test imports ``skyportalai.cli.main.app`` and invokes a
sub-application directly, so none of them exercise the compiled root command.
A previous unification attempt shipped a CLI where every Typer subcommand
raised ``RuntimeError: CLI context was not initialized`` and all tests still
passed. These tests drive the real entry point and assert exit codes.

Note that ``--help`` renders even when a command is broken, because Click
prints help before running the callback. Asserting on output alone is not
enough; the exit code is the part that catches it.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tomllib

import pytest
from typer.testing import CliRunner

from skyportalai.cli.main import app

runner = CliRunner()

# Every command the unified `skyportalai` is expected to expose.
PORTED_FROM_CLICK = ["configure", "login", "logout", "ask", "servers", "start", "github-token"]
NATIVE_TYPER = ["config", "chat", "ansible", "kubernetes"]


def invoke(*args: str):
    return runner.invoke(app, list(args))


@pytest.mark.parametrize("command", NATIVE_TYPER + PORTED_FROM_CLICK)
def test_command_help_exits_zero(command: str) -> None:
    """Each command must resolve and exit 0 rather than raising."""
    result = invoke(command, "--help")
    assert result.exit_code == 0, f"{command} --help exited {result.exit_code}: {result.output}"
    assert "Traceback" not in result.output


@pytest.mark.parametrize("command", NATIVE_TYPER + PORTED_FROM_CLICK)
def test_command_is_listed_in_root_help(command: str) -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert command in result.output


def test_root_help_exits_zero() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert "Traceback" not in result.output


def test_version_flag() -> None:
    from skyportalai import __version__

    result = invoke("--version")
    assert result.exit_code == 0
    assert __version__ in result.output


def test_context_is_initialized_for_typer_commands(tmp_path, monkeypatch) -> None:
    """Regression guard for the load-bearing Typer root callback.

    ``get_state`` reads ``context.find_root().obj``, which only the
    ``@app.callback()`` sets. If the callback is dropped while composing the
    root command, this is the test that fails.
    """
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    result = invoke("config", "show")
    assert result.exit_code == 0, result.output
    assert "CLI context was not initialized" not in result.output


def test_global_json_flag_still_applies(tmp_path, monkeypatch) -> None:
    """``--json`` is declared on the root callback; losing it breaks JSON mode."""
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    result = invoke("--json", "config", "show")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert "base_url" in payload["data"]


def test_global_base_url_flag_still_applies(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    result = invoke("--json", "--base-url", "https://example.invalid", "config", "show")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["base_url"] == "https://example.invalid"


def test_bare_invocation_starts_the_shell(monkeypatch, tmp_path) -> None:
    """Bare `skyportalai` drops into the shell, as bare `skyportal` used to."""
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    started = []
    monkeypatch.setattr("skyportalai.cli.main.run_shell", lambda: started.append(True))
    result = invoke()
    assert result.exit_code == 0, result.output
    assert started == [True]


def test_unknown_command_exits_nonzero() -> None:
    result = invoke("definitely-not-a-command")
    assert result.exit_code != 0


def test_console_script_entry_point_resolves() -> None:
    """`skyportalai = "skyportalai.cli:main"` must resolve to a callable.

    Resolved in a fresh interpreter on purpose: importing the
    ``skyportalai.cli.main`` submodule binds it as an attribute of the
    ``skyportalai.cli`` package, shadowing the ``main`` function in an
    already-warm process and hiding a broken entry point.
    """
    source = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    target = source["project"]["scripts"]["skyportalai"]
    module, _, attribute = target.partition(":")

    probe = (
        f"import importlib;"
        f"fn = getattr(importlib.import_module({module!r}), {attribute!r});"
        f"assert callable(fn), {target!r}"
    )
    assert subprocess.run([sys.executable, "-c", probe], capture_output=True).returncode == 0


@pytest.mark.parametrize("command", NATIVE_TYPER + PORTED_FROM_CLICK)
def test_command_works_through_the_real_console_script(command: str) -> None:
    """Run the console script in a subprocess, not the imported ``app``.

    The previous unification attempt built a *separate* merged root command for
    the entry point while the tests kept importing ``app``, so the object under
    test and the object users ran had diverged and the suite stayed green. This
    goes through ``skyportalai.cli:main`` exactly as the installed script does,
    which is the only way to catch that class of divergence.
    """
    probe = "from skyportalai.cli import main; main()"
    result = subprocess.run(
        [sys.executable, "-c", probe, command, "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{command} --help exited {result.returncode}: {result.stderr}"
    assert "Traceback" not in result.stderr


def test_console_script_bare_invocation_is_not_an_error(monkeypatch) -> None:
    """Bare invocation must reach the shell rather than exiting non-zero."""
    probe = (
        "import skyportalai.cli.main as m;"
        "m.run_shell = lambda: print('SHELL STARTED');"
        "m.main()"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "SHELL STARTED" in result.stdout


def test_declared_console_scripts_are_the_skyportalai_pair() -> None:
    """The standalone `skyportal` script is gone as of 0.2.0."""
    scripts = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["scripts"]
    assert set(scripts) == {"skyportalai", "skyportalai-agent"}
