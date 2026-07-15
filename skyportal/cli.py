"""Skyportal command-line interface."""

from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from skyportal import __version__
from skyportal.animation import show_startup_animation
from skyportal.config import ConfigManager, PortalConfig, SkyportalConfig
from skyportal.portal import CredentialStore, PortalError, SkyportalClient
from skyportal.shell import InteractiveShell

console = Console()


def _portal_client() -> SkyportalClient:
    portal = ConfigManager.load_config().portal
    return SkyportalClient(portal.base_url, portal.request_timeout)


def _items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "servers"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _run_shell() -> None:
    show_startup_animation(console)
    InteractiveShell(console=console, client_factory=_portal_client).run()


@click.group(invoke_without_command=True)
@click.version_option(version=__version__)
@click.pass_context
def main(context: click.Context) -> None:
    """Talk to the Skyportal Agent and manage server context."""
    if context.invoked_subcommand is None:
        _run_shell()


@main.command()
@click.option(
    "--portal-url",
    default="https://app.skyportal.ai",
    envvar="SKYPORTAL_URL",
    show_default=True,
    help="Skyportal application URL",
)
@click.option("--request-timeout", default=30, type=click.IntRange(min=1), show_default=True)
def configure(portal_url: str, request_timeout: int) -> None:
    """Save Skyportal connection settings."""
    config = SkyportalConfig(
        portal=PortalConfig(base_url=portal_url, request_timeout=request_timeout)
    )
    ConfigManager.save_config(config)
    console.print(
        "[green]✓[/green] Skyportal configuration saved to {}".format(
            ConfigManager.get_config_path()
        )
    )


@main.command()
@click.option("--no-browser", is_flag=True, help="Print the API-key URL without opening it")
@click.option("--token", "enter_token", is_flag=True, help="Paste an existing API key")
def login(no_browser: bool, enter_token: bool) -> None:
    """Create or paste an account API key and connect the CLI."""
    client = _portal_client()
    try:
        if not enter_token:
            result = client.login(open_browser=not no_browser)
            console.print(
                "[bold]Create or copy a Skyportal account API key:[/bold] {}".format(
                    result["verification_url"]
                )
            )
            console.print(
                "Create a key named [bold]Skyportal CLI[/bold] and copy the [bold]sk_[/bold] value.\n"
                "[dim]Do not use an agt_ observability-agent token.[/dim]"
            )
            if not result.get("browser_opened") and not no_browser:
                console.print("[yellow]Browser did not open; use the URL above.[/yellow]")
        access_token = click.prompt("Skyportal API key", hide_input=True)
        client.set_access_token(access_token)
    except PortalError as error:
        raise click.ClickException(str(error)) from error
    console.print("[green]✓[/green] Credential validated and saved securely")


@main.command()
def logout() -> None:
    """Remove locally stored Skyportal credentials."""
    CredentialStore.clear()
    console.print("[green]✓[/green] Logged out")


@main.group()
def github_token() -> None:
    """Manage the GitHub Personal Access Token used for git clone."""


@github_token.command("status")
def github_token_status() -> None:
    """Show whether a GitHub PAT is saved (token value is always masked)."""
    try:
        result = _portal_client().get_github_token_status()
    except PortalError as error:
        raise click.ClickException(str(error))
    if result.get("has_token"):
        console.print(
            "[green]✓[/green] GitHub PAT is set: [bold]{}[/bold]".format(
                result.get("masked_token", "****")
            )
        )
    else:
        console.print("[yellow]No GitHub PAT saved.[/yellow]")


@github_token.command("set")
@click.option(
    "--repo",
    metavar="OWNER/NAME",
    default=None,
    help="Validate the token against a specific repository",
)
def github_token_set(repo: Optional[str]) -> None:
    """Save a GitHub PAT (prompts for the token without echoing it)."""
    try:
        pat = click.prompt("GitHub Personal Access Token", hide_input=True)
        result = _portal_client().save_github_token(pat.strip(), repo=repo)
    except PortalError as error:
        raise click.ClickException(str(error))
    console.print(
        "[green]✓[/green] GitHub PAT saved for [bold]{}[/bold] (masked: [bold]{}[/bold])".format(
            result.get("login", "unknown"),
            result.get("masked_token", "****"),
        )
    )


@github_token.command("remove")
def github_token_remove() -> None:
    """Delete the saved GitHub PAT from Skyportal."""
    try:
        _portal_client().delete_github_token()
    except PortalError as error:
        raise click.ClickException(str(error))
    console.print("[green]✓[/green] GitHub PAT removed")


@main.command()
@click.argument("message", required=False)
@click.option("--server", "server_id", type=int, help="Target one owned server ID")
def ask(message: Optional[str], server_id: Optional[int]) -> None:
    """Send one message to the Skyportal Agent."""
    prompt = message or click.prompt("Message")
    client = _portal_client()
    try:
        with console.status("[cyan]Skyportal is thinking…[/cyan]", spinner="dots12"):
            turn = client.run_chat_turn(prompt, server_id=server_id)
    except PortalError as error:
        raise click.ClickException(str(error)) from error
    response = client.assistant_text(turn.messages)
    if response:
        console.print(Markdown(response))
    if turn.status == "awaiting_approval":
        console.print(
            "[yellow]Chat #{} is awaiting approval. Continue it in the interactive shell.[/yellow]".format(
                turn.chat_id
            )
        )


@main.command()
def servers() -> None:
    """List servers owned by the connected account."""
    try:
        entries = _items(_portal_client().servers())
    except PortalError as error:
        raise click.ClickException(str(error)) from error
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
                server.get("vcpu", 0),
                server.get("ram", 0),
                server.get("gpus", 0),
            ),
        )
    console.print(table)


@main.command()
def start() -> None:
    """Launch the persistent Skyportal command center."""
    _run_shell()
