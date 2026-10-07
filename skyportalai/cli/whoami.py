"""``skyportalai whoami``: the account's role and per-environment permissions."""

from __future__ import annotations

import typer

from skyportalai.types import EffectivePermissions

from .context import get_state as _state
from .context import run_command


def whoami(context: typer.Context) -> None:
    """Show the account, approval mode, and what each environment allows.

    Display only: the server enforces every policy on each command.
    """
    permissions = run_command(context, lambda state: state.client().get_effective_permissions())
    _state(context).output.success(permissions.raw, human=_render(permissions))


def _render(permissions: EffectivePermissions) -> str:
    account = permissions.username + (f" <{permissions.email}>" if permissions.email else "")
    lines = [
        f"Account: {account}",
        f"Approval mode: {permissions.permission_mode}",
        f"Read-only mode: {'on' if permissions.read_only_mode else 'off'}",
        "",
        "Your own hosts:",
        *_environment_lines(permissions.own_environments),
    ]
    if not permissions.teams:
        return "\n".join([*lines, "", "Teams: none"])
    for team in permissions.teams:
        lines += ["", f"Team {team.name} (role: {team.role}), ceiling per environment:"]
        lines += _environment_lines(team.environments)
    lines += ["", "On a teammate's host, the stricter of the team ceiling and your own setting applies."]
    return "\n".join(lines)


def _environment_lines(environments: dict[str, str | None]) -> list[str]:
    if not environments:
        return ["  (no environments)"]
    width = max(len(env) for env in environments)
    return [f"  {env.ljust(width)}  {policy or 'denied'}" for env, policy in environments.items()]
