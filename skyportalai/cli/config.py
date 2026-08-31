"""Configuration resolution shared by public CLI commands."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from skyportalai import _env
from skyportalai._client import DEFAULT_BASE_URL
from skyportalai._exceptions import SkyportalError


@dataclass(frozen=True)
class CLISettings:
    """Effective, non-secret CLI connection settings."""

    api_key: str | None
    api_key_source: str | None
    base_url: str
    timeout: float
    config_path: Path
    credentials_path: Path


def get_config_path() -> Path:
    return _env.config_path("config.yaml", "SKYPORTALAI_CONFIG_PATH")


def get_credentials_path() -> Path:
    return _env.config_path("credentials.json", "SKYPORTALAI_CREDENTIALS_PATH")


def resolve_settings(*, base_url: str | None = None) -> CLISettings:
    """Resolve CLI settings without exposing the credential value."""
    config_path = get_config_path()
    credentials_path = get_credentials_path()
    config = _read_mapping(config_path, "configuration", yaml.safe_load)
    credentials = _read_mapping(credentials_path, "credentials", json.load)
    portal = config.get("portal", {})
    if not isinstance(portal, dict):
        raise SkyportalError(f"Invalid SkyPortal configuration in {config_path}: 'portal' must be a mapping.")

    configured_url = portal.get("base_url")
    stored_url = credentials.get("base_url")
    effective_url = (
        base_url
        or _env.get("SKYPORTALAI_BASE_URL")
        or _env.get("SKYPORTALAI_URL")
        or (str(configured_url) if configured_url else None)
        or (str(stored_url) if stored_url else None)
        or DEFAULT_BASE_URL
    ).rstrip("/")

    timeout_value = portal.get("request_timeout", 30.0)
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise SkyportalError(f"Invalid request timeout in {config_path}: {timeout_value!r}.") from exc
    if timeout <= 0:
        raise SkyportalError(f"Invalid request timeout in {config_path}: it must be greater than zero.")

    api_key, source = _env.lookup("SKYPORTALAI_API_KEY")
    if not api_key:
        api_key, source = _env.lookup("SKYPORTALAI_ACCESS_TOKEN")
    if not api_key and credentials.get("access_token"):
        if stored_url and str(stored_url).rstrip("/") != effective_url:
            raise SkyportalError(
                "Stored credentials belong to another SkyPortal deployment. "
                "Set SKYPORTALAI_API_KEY or update the selected base URL."
            )
        api_key = str(credentials["access_token"])
        source = str(credentials_path)

    return CLISettings(
        api_key=api_key,
        api_key_source=source,
        base_url=effective_url,
        timeout=timeout,
        config_path=config_path,
        credentials_path=credentials_path,
    )


def save_connection_config(*, base_url: str | None, timeout: float | None) -> Path:
    """Persist non-secret connection settings in the legacy-compatible YAML shape."""
    path = get_config_path()
    config = _read_mapping(path, "configuration", yaml.safe_load)
    portal = config.setdefault("portal", {})
    if not isinstance(portal, dict):
        raise SkyportalError(f"Invalid SkyPortal configuration in {path}: 'portal' must be a mapping.")
    if base_url is not None:
        portal["base_url"] = base_url.rstrip("/")
    if timeout is not None:
        if timeout <= 0:
            raise SkyportalError("Request timeout must be greater than zero.")
        portal["request_timeout"] = timeout

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as config_file:
        yaml.safe_dump(config, config_file, default_flow_style=False, sort_keys=True)
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
    return path


def _read_mapping(path: Path, label: str, loader: Any) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open() as source:
            value = loader(source) or {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise SkyportalError(f"Could not read SkyPortal {label} from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SkyportalError(f"Invalid SkyPortal {label} in {path}: expected a mapping.")
    return value