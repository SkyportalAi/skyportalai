"""Environment and path resolution for the ``skyportalai`` surface.

Every setting is a ``SKYPORTALAI_*`` environment variable and configuration lives
under ``~/.skyportalai``. The pre-0.2.0 ``SKYPORTAL_*`` names were removed in 0.3.0
and are no longer read. A leftover ``~/.skyportal`` directory is still moved to
``~/.skyportalai`` the first time it is needed, so a late upgrade keeps its
credentials and history.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from pathlib import Path

PREFIX = "SKYPORTALAI_"

CONFIG_DIR_NAME = ".skyportalai"
LEGACY_CONFIG_DIR_NAME = ".skyportal"


def lookup(name: str, default: str | None = None) -> tuple[str | None, str | None]:
    """Resolve ``name`` from the environment.

    Returns the value and the variable it came from (None when the default was
    used), so callers can report the source.
    """
    value = os.environ.get(name)
    if value is not None:
        return value, name
    return default, None


def get(name: str, default: str | None = None) -> str | None:
    """Resolve ``name`` from the environment."""
    return lookup(name, default)[0]


def get_from(environ: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    """Resolve ``name`` from an explicit mapping.

    The agent is configured from an injected environment mapping rather than
    :data:`os.environ`, so it cannot use :func:`get`.
    """
    return environ.get(name, default)


def config_dir() -> Path:
    """Return the CLI config directory, moving a leftover ``~/.skyportal`` into place.

    The move is a rename, so it runs once and is a no-op afterwards. If it cannot
    be performed the old directory is used as-is rather than silently starting from
    an empty configuration.
    """
    home = Path.home()
    current = home / CONFIG_DIR_NAME
    legacy = home / LEGACY_CONFIG_DIR_NAME

    if current.exists():
        return current
    if not legacy.exists():
        return current

    try:
        legacy.rename(current)
    except OSError:
        warnings.warn(
            f"Could not move {legacy} to {current}; continuing to use {legacy}. Move it manually.",
            UserWarning,
            stacklevel=2,
        )
        return legacy
    return current


def config_path(name: str, env_var: str) -> Path:
    """Return ``<config dir>/<name>``, honouring an explicit path override."""
    override = get(env_var)
    return Path(override).expanduser() if override else config_dir() / name
