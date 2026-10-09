"""Public command-line interface for the Skyportal SDK."""

import importlib.util
import sys

# What the CLI imports beyond the SDK: the `cli` extra in pyproject.toml.
CLI_PACKAGES = ("click", "prompt_toolkit", "pydantic", "rich", "typer", "yaml")
INSTALL_HINT = 'The CLI needs extra packages: pip install "skyportalai[cli]"'


def missing_cli_packages() -> list[str]:
    return [name for name in CLI_PACKAGES if importlib.util.find_spec(name) is None]


def require_cli_packages() -> None:
    """Exit with the install hint, not a traceback, when the `cli` extra is missing.

    The console script is installed whatever extras were chosen, so an SDK-only or
    agent-only install reaches here without the CLI's packages. Checked up front:
    `start` imports the shell lazily, so an ImportError there would come mid-run."""
    if missing := missing_cli_packages():
        print(f"{INSTALL_HINT}\n(missing: {', '.join(missing)})", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    """Load the Typer application only when the console script runs."""
    require_cli_packages()
    from .main import main as run

    run()


__all__ = ["main"]
