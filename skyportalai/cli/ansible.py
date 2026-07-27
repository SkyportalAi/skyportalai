"""Ansible playbook lifecycle commands for the public CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from .context import get_state as _state
from .context import run_command

ansible_app = typer.Typer(
    help="Create, manage, list, and deploy Ansible playbooks through the ops agent."
)

_MAX_PLAYBOOK_BYTES = 256 * 1024


def _read_playbook(path: str) -> str:
    if path == "-":
        raw = sys.stdin.buffer.read(_MAX_PLAYBOOK_BYTES + 1)
        source = "stdin"
    else:
        resolved = Path(path).expanduser()
        try:
            with resolved.open("rb") as handle:
                raw = handle.read(_MAX_PLAYBOOK_BYTES + 1)
        except OSError as exc:
            raise typer.BadParameter(f"Cannot read playbook {resolved}: {exc}") from None
        source = str(resolved)
    if len(raw) > _MAX_PLAYBOOK_BYTES:
        raise typer.BadParameter("Playbook exceeds the 256 KiB safety limit.")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise typer.BadParameter(f"Cannot decode playbook from {source}: {exc}") from None
    if not content.strip():
        raise typer.BadParameter(f"Playbook from {source} is empty.")
    return content


@ansible_app.command("create")
def create(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Display name for the playbook.")],
    file: Annotated[
        str,
        typer.Option("--file", "-f", help="Playbook YAML path, or '-' for standard input."),
    ],
    description: Annotated[
        str,
        typer.Option("--description", "-d", help="Optional playbook description."),
    ] = "",
) -> None:
    """Validate and store a new Ansible playbook."""
    content = _read_playbook(file)
    playbook = run_command(
        context,
        lambda state: state.client().ansible.create(
            name,
            content,
            description=description,
        ),
    )
    _state(context).output.success(
        playbook,
        human=f'Created Ansible playbook #{playbook.id}: {playbook.name}',
    )


@ansible_app.command("list")
def list_playbooks(context: typer.Context) -> None:
    """List stored Ansible playbooks."""
    playbooks = run_command(context, lambda state: state.client().ansible.list())
    if not playbooks:
        human = "No Ansible playbooks stored."
    else:
        lines = ["ID  NAME  UPDATED  DESCRIPTION"]
        for playbook in playbooks:
            lines.append(
                f"{playbook.id}  {playbook.name}  {playbook.updated_at or '-'}  "
                f"{playbook.description or '-'}"
            )
        human = "\n".join(lines)
    _state(context).output.success(playbooks, human=human)


@ansible_app.command("show")
def show(
    context: typer.Context,
    playbook_id: Annotated[int, typer.Argument(min=1, help="Playbook ID.")],
) -> None:
    """Show one playbook, including its YAML."""
    playbook = run_command(
        context,
        lambda state: state.client().ansible.get(playbook_id),
    )
    _state(context).output.success(
        playbook,
        human=(
            f"Ansible playbook #{playbook.id}: {playbook.name}\n"
            f"{playbook.description}\n\n{playbook.content}"
        ).rstrip(),
    )


@ansible_app.command("update")
def update(
    context: typer.Context,
    playbook_id: Annotated[int, typer.Argument(min=1, help="Playbook ID.")],
    name: Annotated[str | None, typer.Option("--name", help="New display name.")] = None,
    file: Annotated[
        str | None,
        typer.Option("--file", "-f", help="Replacement YAML path, or '-' for standard input."),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", "-d", help="Replacement description."),
    ] = None,
) -> None:
    """Update a playbook's name, description, YAML, or any combination."""
    if name is None and file is None and description is None:
        _state(context).output.failure("Provide --name, --file, --description, or a combination.")
        raise typer.Exit(2)
    content = _read_playbook(file) if file is not None else None
    playbook = run_command(
        context,
        lambda state: state.client().ansible.update(
            playbook_id,
            name=name,
            content=content,
            description=description,
        ),
    )
    _state(context).output.success(
        playbook,
        human=f'Updated Ansible playbook #{playbook.id}: {playbook.name}',
    )


@ansible_app.command("delete")
def delete(
    context: typer.Context,
    playbook_id: Annotated[int, typer.Argument(min=1, help="Playbook ID.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Delete a stored Ansible playbook."""
    if not yes:
        if _state(context).output.json_mode:
            _state(context).output.failure("--yes is required when using --json mode")
            raise typer.Exit(1)
        if not typer.confirm(f"Delete Ansible playbook #{playbook_id}?"):
            raise typer.Abort()
    result = run_command(
        context,
        lambda state: state.client().ansible.delete(playbook_id),
    )
    _state(context).output.success(
        result,
        human=f"Deleted Ansible playbook #{playbook_id}.",
    )


@ansible_app.command("deploy")
def deploy(
    context: typer.Context,
    playbook_id: Annotated[int, typer.Argument(min=1, help="Playbook ID.")],
    server_id: Annotated[
        int,
        typer.Option("--server", "-s", min=1, help="Owned SSH target ID."),
    ],
) -> None:
    """Start an audited agent deployment of a stored playbook."""
    deployment = run_command(
        context,
        lambda state: state.client().ansible.deploy(
            playbook_id,
            server_id=server_id,
        ),
    )
    _state(context).output.success(
        deployment,
        human=(
            f"Ansible deployment started in chat #{deployment.chat_id} "
            f"(playbook #{deployment.playbook_id} → server #{deployment.server_id}).\n"
            f"Monitor it with: skyportalai chat wait {deployment.chat_id}"
        ),
    )
