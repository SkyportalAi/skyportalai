"""Commands ported from the standalone ``skyportal`` Click CLI.

These were a separate Click application until 0.2.0. Typer vendors its own
Click (:mod:`typer._click`), so a ``TyperGroup`` and a ``click.Group`` do not
share a class hierarchy and their command objects cannot be mixed; the commands
are therefore defined natively against Typer rather than adapted at runtime.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from skyportalai import _env
from skyportalai._client import DEFAULT_BASE_URL
from skyportalai.shell import (
    ConfigManager,
    CredentialStore,
    InteractiveShell,
    PortalConfig,
    PortalError,
    SkyportalClient,
    SkyportalConfig,
    show_startup_animation,
)

console = Console()
# Errors go to stderr, as click.ClickException did before 0.2.0. Only visible to anyone
# separating the streams, which is exactly who would be broken by errors on stdout.
err_console = Console(stderr=True)


def _portal_client() -> SkyportalClient:
    portal = ConfigManager.load_config().portal
    return SkyportalClient(portal.base_url, portal.request_timeout)


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "servers"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def run_shell() -> None:
    """Launch the persistent Skyportal command center."""
    show_startup_animation(console)
    InteractiveShell(console=console, client_factory=_portal_client).run()


def _fail(error: PortalError) -> typer.Exit:
    """Report an expected portal error the way Click's ClickException did."""
    err_console.print(f"[red]Error:[/red] {error}")
    return typer.Exit(1)


def configure(
    portal_url: Annotated[
        str | None,
        typer.Option(
            "--portal-url",
            help=f"Skyportal application URL  [env: SKYPORTALAI_URL]  [default: {DEFAULT_BASE_URL}]",
        ),
    ] = None,
    request_timeout: Annotated[
        int,
        typer.Option("--request-timeout", min=1, help="HTTP request timeout in seconds"),
    ] = 30,
) -> None:
    """Save Skyportal connection settings."""
    # Resolved here rather than by typer's envvar=, which Click reads straight out of
    # os.environ: _env.lookup never runs, so the legacy SKYPORTAL_URL fallback and its
    # deprecation warning are skipped and a self-hosted user is silently pointed at the
    # SaaS host. --base-url on the root callback carries the same envvar= shape but
    # survives it because resolve_settings() re-resolves through _env.
    resolved_url = portal_url or _env.get("SKYPORTALAI_URL") or DEFAULT_BASE_URL
    config = SkyportalConfig(portal=PortalConfig(base_url=resolved_url, request_timeout=request_timeout))
    ConfigManager.save_config(config)
    console.print(f"[green]✓[/green] Skyportal configuration saved to {ConfigManager.get_config_path()}")


def login(
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the API-key URL without opening it")
    ] = False,
    enter_token: Annotated[bool, typer.Option("--token", help="Paste an existing API key")] = False,
) -> None:
    """Create or paste an account API key and connect the CLI."""
    client = _portal_client()
    try:
        if not enter_token:
            result = client.login(open_browser=not no_browser)
            console.print(
                "[bold]Create or copy a Skyportal account API key:[/bold] {}".format(result["verification_url"])
            )
            console.print(
                "Create a key named [bold]Skyportal CLI[/bold] and copy the [bold]sk_[/bold] value.\n"
                "[dim]Do not use an agt_ observability-agent token.[/dim]"
            )
            if not result.get("browser_opened") and not no_browser:
                console.print("[yellow]Browser did not open; use the URL above.[/yellow]")
        access_token = typer.prompt("Skyportal API key", hide_input=True)
        client.set_access_token(access_token)
    except PortalError as error:
        raise _fail(error) from None
    console.print("[green]✓[/green] Credential validated and saved securely")


def logout() -> None:
    """Remove locally stored Skyportal credentials."""
    CredentialStore.clear()
    console.print("[green]✓[/green] Logged out")


def ask(
    message: Annotated[str | None, typer.Argument(help="Message to send")] = None,
    server_ids: Annotated[
        list[int] | None,
        typer.Option(
            "--server",
            help="Target a server ID; repeat to scope the first turn to multiple hosts",
        ),
    ] = None,
) -> None:
    """Send one message to the Skyportal Agent."""
    prompt = message or typer.prompt("Message")
    client = _portal_client()
    try:
        with console.status("[cyan]Skyportal is thinking…[/cyan]", spinner="dots12"):
            selected = list(dict.fromkeys(server_ids or []))
            if len(selected) == 1:
                # Preserve the established singular request for deployments
                # that predate the plural first-turn scope contract.
                turn = client.run_chat_turn(prompt, server_id=selected[0])
            elif selected:
                turn = client.run_chat_turn(prompt, server_ids=selected, active_server_id=selected[0])
            else:
                turn = client.run_chat_turn(prompt)
    except PortalError as error:
        raise _fail(error) from None
    response = client.assistant_text(turn.messages)
    if response:
        console.print(Markdown(response))
    if turn.status == "awaiting_approval":
        console.print(
            "[yellow]Chat #{} is awaiting approval. Continue it in the interactive shell.[/yellow]".format(
                turn.chat_id
            )
        )


def servers() -> None:
    """List servers owned by the connected account."""
    try:
        entries = _items(_portal_client().servers())
    except PortalError as error:
        raise _fail(error) from None
    if not entries:
        console.print("[yellow]No servers found.[/yellow]")
        return
    table = Table(title="Skyportal Servers")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Status", style="green")
    table.add_column("Environment")
    table.add_column("Resources")
    for server in entries:
        table.add_row(
            str(server.get("id", "-")),
            str(server.get("name") or server.get("hostname") or "-"),
            str(server.get("status", "-")),
            str(server.get("host_type") or server.get("location") or "Custom"),
            "{} vCPU / {} GB RAM / {} GPU".format(
                server.get("vcpu", 0), server.get("ram", 0), server.get("gpus", 0)
            ),
        )
    console.print(table)


def start() -> None:
    """Launch the persistent Skyportal command center."""
    run_shell()


github_token_app = typer.Typer(help="Manage the GitHub Personal Access Token used for git clone.")


@github_token_app.command("status")
def github_token_status() -> None:
    """Show whether a GitHub PAT is saved (token value is always masked)."""
    try:
        result = _portal_client().get_github_token_status()
    except PortalError as error:
        raise _fail(error) from None
    if result.get("has_token"):
        console.print("[green]✓[/green] GitHub PAT is set: [bold]{}[/bold]".format(result.get("masked_token", "****")))
    else:
        console.print("[yellow]No GitHub PAT saved.[/yellow]")


@github_token_app.command("set")
def github_token_set(
    repo: Annotated[
        str | None,
        typer.Option("--repo", metavar="OWNER/NAME", help="Validate the token against a specific repository"),
    ] = None,
) -> None:
    """Save a GitHub PAT (prompts for the token without echoing it)."""
    try:
        pat = typer.prompt("GitHub Personal Access Token", hide_input=True)
        result = _portal_client().save_github_token(pat.strip(), repo=repo)
    except PortalError as error:
        raise _fail(error) from None
    console.print(
        "[green]✓[/green] GitHub PAT saved for [bold]{}[/bold] (masked: [bold]{}[/bold])".format(
            result.get("login", "unknown"), result.get("masked_token", "****")
        )
    )


@github_token_app.command("remove")
def github_token_remove() -> None:
    """Delete the saved GitHub PAT from Skyportal."""
    try:
        _portal_client().delete_github_token()
    except PortalError as error:
        raise _fail(error) from None
    console.print("[green]✓[/green] GitHub PAT removed")


def register(app: typer.Typer) -> None:
    """Attach the ported commands to the root ``skyportalai`` application."""
    app.command("configure")(configure)
    app.command("login")(login)
    app.command("logout")(logout)
    app.command("ask")(ask)
    app.command("servers")(servers)
    app.command("start")(start)
    app.add_typer(github_token_app, name="github-token")
