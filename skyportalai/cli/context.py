"""Shared runtime state for public CLI command modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

import typer

from skyportalai import Skyportal, SkyportalError

from .config import CLISettings
from .output import Output

T = TypeVar("T")


@dataclass
class CLIContext:
    """Per-invocation dependencies shared by command modules."""

    settings: CLISettings
    output: Output

    def client(self) -> Skyportal:
        if not self.settings.api_key:
            raise SkyportalError(
                "No API key configured. Set SKYPORTAL_API_KEY or run the existing "
                "'skyportal login' flow."
            )
        return Skyportal(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
        )


def run_command(context: typer.Context, operation: Callable[[CLIContext], T]) -> T:
    """Run a command operation with consistent expected-error handling."""
    state = get_state(context)
    try:
        return operation(state)
    except SkyportalError as exc:
        state.output.failure(str(exc))
        raise typer.Exit(1) from None


def get_state(context: typer.Context) -> CLIContext:
    """Return the initialized state for a command invocation."""
    state = context.find_root().obj
    if not isinstance(state, CLIContext):
        raise RuntimeError("CLI context was not initialized")
    return state