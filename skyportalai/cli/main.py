"""Typer application for the public ``skyportalai`` command."""

from __future__ import annotations

from typing import Annotated

import typer

from skyportalai import SkyportalError, __version__

from .config import resolve_settings, save_connection_config
from .context import CLIContext
from .context import get_state as _state
from .output import Output

app = typer.Typer(
    name="skyportalai",
    help="Drive the SkyPortal API and ops agent.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Inspect or update CLI connection settings.")
app.add_typer(config_app, name="config")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"skyportalai {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    context: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable JSON instead of human-readable output."),
    ] = False,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", envvar="SKYPORTAL_BASE_URL", help="Override the API target."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit."),
    ] = False,
) -> None:
    """Resolve shared configuration before dispatching a command."""
    del version
    settings = resolve_settings(base_url=base_url)
    context.obj = CLIContext(
        settings=settings,
        output=Output(json_mode=json_output, api_target=settings.base_url),
    )


@config_app.command("show")
def show_config(context: typer.Context) -> None:
    """Show effective connection settings without printing the API key."""
    state = _state(context)
    data = {
        "base_url": state.settings.base_url,
        "timeout": state.settings.timeout,
        "authenticated": state.settings.api_key is not None,
        "api_key_source": state.settings.api_key_source,
        "config_path": state.settings.config_path,
    }
    state.output.success(
        data,
        human=(
            f"Base URL: {state.settings.base_url}\n"
            f"Timeout: {state.settings.timeout:g}s\n"
            f"Credential: {state.settings.api_key_source or 'not configured'}\n"
            f"Config: {state.settings.config_path}"
        ),
    )


@config_app.command("set")
def set_config(
    context: typer.Context,
    base_url: Annotated[str | None, typer.Option("--base-url", help="API base URL to save.")] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", min=0.001, help="Per-request timeout in seconds."),
    ] = None,
) -> None:
    """Save non-secret connection settings."""
    state = _state(context)
    if base_url is None and timeout is None:
        state.output.failure("Provide --base-url, --timeout, or both.")
        raise typer.Exit(2)
    try:
        path = save_connection_config(base_url=base_url, timeout=timeout)
        updated = resolve_settings()
    except SkyportalError as exc:
        state.output.failure(str(exc))
        raise typer.Exit(1) from None
    state.output = Output(json_mode=state.output.json_mode, api_target=updated.base_url)
    state.settings = updated
    state.output.success(
        {"config_path": path, "base_url": updated.base_url, "timeout": updated.timeout},
        human=f"Saved SkyPortal CLI configuration to {path}",
    )


from .chat import chat_app  # noqa: E402
from .kubernetes import kubernetes_app  # noqa: E402

app.add_typer(chat_app, name="chat")
app.add_typer(kubernetes_app, name="kubernetes")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
