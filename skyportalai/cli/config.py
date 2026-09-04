"""Configuration resolution shared by public CLI commands."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from skyportalai import _env
from skyportalai._client import DEFAULT_BASE_URL, normalize_base_url
from skyportalai._exceptions import SkyportalError

_FLAG_ORIGIN = "--base-url"


@dataclass(frozen=True)
class CLISettings:
    """Effective, non-secret CLI connection settings."""

    api_key: str | None
    api_key_source: str | None
    base_url: str
    timeout: float
    config_path: Path
    credentials_path: Path
    #: Why a stored credential could not be used, if there was one.
    credential_conflict: str | None = None


def get_config_path() -> Path:
    return _env.config_path("config.yaml", "SKYPORTALAI_CONFIG_PATH")


def get_credentials_path() -> Path:
    return _env.config_path("credentials.json", "SKYPORTALAI_CREDENTIALS_PATH")


def resolve_settings(*, base_url: str | None = None) -> CLISettings:
    """Resolve CLI settings without exposing the credential value."""
    config_path = get_config_path()
    credentials_path = get_credentials_path()
    config = _read_mapping(config_path, "configuration", yaml.safe_load)
    # Resolution must not die on the credential file: `skyportalai logout`
    # exists to remove exactly the file that cannot be used, and it runs
    # through this same resolution. Record the reason instead of raising.
    credentials, credential_conflict = _read_credentials(credentials_path)
    portal = config.get("portal", {})
    if not isinstance(portal, dict):
        raise SkyportalError(f"Invalid Skyportal configuration in {config_path}: 'portal' must be a mapping.")

    stored_url = credentials.get("base_url")
    effective_url, url_origin = _select_base_url(
        flag=base_url,
        configured=portal.get("base_url"),
        stored=stored_url,
    )

    timeout_value = portal.get("request_timeout", 30.0)
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise SkyportalError(f"Invalid request timeout in {config_path}: {timeout_value!r}.") from exc
    if timeout <= 0:
        raise SkyportalError(f"Invalid request timeout in {config_path}: it must be greater than zero.")

    # ACCESS_TOKEN first, matching shell/portal.py._env_access_token and what
    # docs/deployment.md states. This path preferred API_KEY, so with both set the CLI
    # could authenticate as a different identity than the shell did.
    api_key, source = _env.lookup("SKYPORTALAI_ACCESS_TOKEN")
    if not api_key:
        api_key, source = _env.lookup("SKYPORTALAI_API_KEY")
    if not api_key and credentials.get("access_token"):
        # Normalized on both sides: the shell client rewrites the marketing host
        # to the app host before saving, so a raw comparison reports a conflict
        # between two spellings of one deployment that logout cannot resolve.
        stored_normalized = normalize_base_url(str(stored_url)) if stored_url else None
        if stored_normalized and stored_normalized != effective_url:
            credential_conflict = (
                f"Stored credentials belong to another Skyportal deployment "
                f"({stored_normalized}), but the selected base URL is {effective_url}. "
                f"Run 'skyportalai logout' to clear them ({credentials_path}), "
                f"or keep them by {_keep_credentials_advice(url_origin, stored_normalized)}."
            )
        else:
            api_key = str(credentials["access_token"])
            source = str(credentials_path)

    return CLISettings(
        api_key=api_key,
        api_key_source=source,
        base_url=effective_url,
        timeout=timeout,
        config_path=config_path,
        credentials_path=credentials_path,
        credential_conflict=credential_conflict,
    )


def _select_base_url(*, flag: str | None, configured: Any, stored: Any) -> tuple[str, str | None]:
    """The effective base URL, and the flag or variable that selected it.

    The origin is not decoration: ``--base-url`` and the environment both
    outrank ``config.yaml``, so advice to run ``config set --base-url`` is a
    dead end when one of them is what chose the URL.
    """
    if flag:
        return normalize_base_url(flag), _FLAG_ORIGIN
    env_url, env_name = _env.lookup("SKYPORTALAI_BASE_URL")
    if not env_url:
        env_url, env_name = _env.lookup("SKYPORTALAI_URL")
    if env_url:
        return normalize_base_url(env_url), env_name
    if configured:
        return normalize_base_url(str(configured)), None
    if stored:
        return normalize_base_url(str(stored)), None
    return DEFAULT_BASE_URL, None


def _keep_credentials_advice(url_origin: str | None, stored_url: str) -> str:
    """How to make the selected URL match the stored credential."""
    if url_origin == _FLAG_ORIGIN:
        return f"dropping {_FLAG_ORIGIN}"
    if url_origin:
        return f"unsetting {url_origin}"
    return f"running 'skyportalai config set --base-url {stored_url}'"


def save_connection_config(*, base_url: str | None, timeout: float | None) -> Path:
    """Persist non-secret connection settings in the legacy-compatible YAML shape."""
    path = get_config_path()
    config = _read_mapping(path, "configuration", yaml.safe_load)
    portal = config.setdefault("portal", {})
    if not isinstance(portal, dict):
        raise SkyportalError(f"Invalid Skyportal configuration in {path}: 'portal' must be a mapping.")
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


def _read_credentials(path: Path) -> tuple[dict[str, Any], str | None]:
    """Read the credential file, reporting rather than raising when it cannot be used.

    Content failures and access failures get different advice on purpose: an
    unparseable file is worth deleting, but a permission error or a transient
    read failure on a perfectly good credential is not.
    """
    if not path.exists():
        return {}, None
    try:
        with path.open() as source:
            value = json.load(source) or {}
    except OSError as exc:
        return {}, f"Could not read Skyportal credentials from {path}: {exc}. Check the file's permissions."
    except ValueError as exc:
        return {}, f"Invalid Skyportal credentials in {path}: {exc}. Run 'skyportalai logout' to remove the file."
    if not isinstance(value, dict):
        return {}, (
            f"Invalid Skyportal credentials in {path}: expected a mapping. "
            "Run 'skyportalai logout' to remove the file."
        )
    return value, None


def _read_mapping(path: Path, label: str, loader: Any) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open() as source:
            value = loader(source) or {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise SkyportalError(f"Could not read Skyportal {label} from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SkyportalError(f"Invalid Skyportal {label} in {path}: expected a mapping.")
    return value