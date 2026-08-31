"""Public command-line interface for the SkyPortal SDK."""


def build_cli():
    """Return the single Click group exposing every `skyportalai` command.

    The interactive shell is a Click group and the API client is a Typer app.
    Typer compiles to Click, so the two command sets are merged here rather
    than either one being rewritten. Imports stay inside the function because
    Typer and Rich are slow to import and this runs on every invocation.
    """
    import typer

    from skyportal.cli import main as shell_group

    from .main import app

    for name, command in typer.main.get_command(app).commands.items():
        shell_group.add_command(command, name)

    return shell_group


def main() -> None:
    """Console-script entry point."""
    build_cli()()


__all__ = ["build_cli", "main"]
