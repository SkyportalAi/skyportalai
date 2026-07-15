"""Human and machine-readable CLI output."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import typer


class Output:
    """Render a stable JSON envelope or concise human-readable text."""

    def __init__(self, *, json_mode: bool, api_target: str):
        self.json_mode = json_mode
        self.api_target = api_target

    def success(self, data: Any, *, human: str) -> None:
        if self.json_mode:
            typer.echo(json.dumps({"ok": True, "api_target": self.api_target, "data": _jsonable(data)}))
            return
        typer.echo(human)
        typer.echo(f"API target: {self.api_target}", err=True)

    def failure(self, message: str) -> None:
        if self.json_mode:
            typer.echo(
                json.dumps({"ok": False, "api_target": self.api_target, "error": message}),
                err=True,
            )
            return
        typer.echo(f"Error: {message}", err=True)
        typer.echo(f"API target: {self.api_target}", err=True)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, set):
        return [_jsonable(item) for item in sorted(value, key=lambda item: (type(item).__name__, repr(item)))]
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value