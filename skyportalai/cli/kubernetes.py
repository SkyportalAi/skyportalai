"""Kubernetes lifecycle commands for the public CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from .context import get_state as _state
from .context import run_command

kubernetes_app = typer.Typer(
    help="Connect Kubernetes clusters and make them available to agent chats."
)

_MAX_KUBECONFIG_BYTES = 1024 * 1024


def _read_kubeconfig(path: str) -> str:
    if path == "-":
        raw_bytes = sys.stdin.buffer.read(_MAX_KUBECONFIG_BYTES + 1)
        if len(raw_bytes) > _MAX_KUBECONFIG_BYTES:
            raise typer.BadParameter("Kubeconfig exceeds the 1 MiB safety limit.")
        try:
            contents = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise typer.BadParameter(f"Cannot decode kubeconfig from stdin: {exc}") from None
        source = "stdin"
    else:
        resolved = Path(path).expanduser()
        try:
            with resolved.open("rb") as fh:
                raw_bytes = fh.read(_MAX_KUBECONFIG_BYTES + 1)
        except OSError as exc:
            raise typer.BadParameter(f"Cannot read kubeconfig {resolved}: {exc}") from None
        if len(raw_bytes) > _MAX_KUBECONFIG_BYTES:
            raise typer.BadParameter("Kubeconfig exceeds the 1 MiB safety limit.")
        try:
            contents = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise typer.BadParameter(f"Cannot decode kubeconfig {resolved}: {exc}") from None
        source = str(resolved)
    if not contents.strip():
        raise typer.BadParameter(f"Kubeconfig from {source} is empty.")
    return contents


@kubernetes_app.command("connect")
def connect(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Display name for the cluster.")],
    kubeconfig: Annotated[
        str,
        typer.Option(
            "--kubeconfig",
            "-k",
            help="Kubeconfig path, or '-' to read it from standard input.",
        ),
    ] = "~/.kube/config",
    environment: Annotated[
        str,
        typer.Option("--environment", "-e", help="Environment policy label."),
    ] = "Custom",
) -> None:
    """Validate, encrypt, and connect a Kubernetes cluster."""
    name = name.strip()
    if not name:
        raise typer.BadParameter("Cluster name cannot be empty.", param_hint="'NAME'")
    contents = _read_kubeconfig(kubeconfig)
    cluster = run_command(
        context,
        lambda state: state.client().kubernetes.connect(
            name,
            contents,
            environment=environment,
        ),
    )
    if cluster.connection_verified is True:
        verification = "verified"
    elif cluster.connection_verified is False:
        verification = "saved; reachability not verified"
    else:
        verification = "saved; reachability unknown"
    _state(context).output.success(
        cluster,
        human=(
            f"Connected Kubernetes cluster #{cluster.id}: {cluster.name} "
            f"({cluster.environment}, {verification})\n"
            f"Control it with: skyportalai chat send --server {cluster.id} "
            f"--namespace {cluster.id}=default --wait \"get the pods\""
        ),
    )


@kubernetes_app.command("list")
def list_clusters(context: typer.Context) -> None:
    """List Kubernetes clusters connected to the account."""
    clusters = run_command(context, lambda state: state.client().kubernetes.list())
    if not clusters:
        human = "No Kubernetes clusters connected."
    else:
        lines = ["ID  NAME  STATUS  ENVIRONMENT  NAMESPACES"]
        for cluster in clusters:
            namespaces = ",".join(cluster.namespaces) or "-"
            lines.append(
                f"{cluster.id}  {cluster.name}  {cluster.status or '-'}  "
                f"{cluster.environment}  {namespaces}"
            )
        human = "\n".join(lines)
    _state(context).output.success(clusters, human=human)


@kubernetes_app.command("disconnect")
def disconnect(
    context: typer.Context,
    cluster_id: Annotated[int, typer.Argument(min=1, help="Cluster ID to remove.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Remove a Kubernetes cluster and its encrypted kubeconfig from SkyPortal."""
    if not yes:
        if _state(context).output.json_mode:
            _state(context).output.failure("--yes is required when using --json mode")
            raise typer.Exit(1)
        if not typer.confirm(f"Disconnect Kubernetes cluster #{cluster_id}?"):
            raise typer.Abort()
    result = run_command(
        context,
        lambda state: state.client().kubernetes.disconnect(cluster_id),
    )
    _state(context).output.success(
        result,
        human=f"Disconnected Kubernetes cluster #{cluster_id}.",
    )
