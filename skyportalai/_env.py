"""Environment and path resolution for the ``skyportalai`` surface.

Every setting is a ``SKYPORTALAI_*`` environment variable and configuration lives
under ``~/.skyportalai``. The pre-0.2.0 ``SKYPORTAL_*`` names were removed in 0.3.0
and are no longer read. One that is still set draws a warning naming its
replacement, except for the settings that choose which host receives credentials:
there a leftover ``SKYPORTAL_BASE_URL`` with no ``SKYPORTALAI_BASE_URL`` is an error,
because falling back to the default would send a self-hosted install's key to the
SaaS host. A leftover ``~/.skyportal``
directory is still moved to ``~/.skyportalai`` the first time it is needed, so a
late upgrade keeps its credentials and history.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from pathlib import Path

from ._exceptions import SkyportalError

PREFIX = "SKYPORTALAI_"
REMOVED_PREFIX = "SKYPORTAL_"
# Settings that decide which host receives the credentials. For these a removed name
# is refused rather than ignored: the default is somebody else's host.
DESTINATION_SETTINGS = frozenset({"SKYPORTALAI_BASE_URL", "SKYPORTALAI_URL"})

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
    _check_removed_name(os.environ, name)
    return default, None


def get(name: str, default: str | None = None) -> str | None:
    """Resolve ``name`` from the environment."""
    return lookup(name, default)[0]


def get_from(environ: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    """Resolve ``name`` from an explicit mapping.

    The agent is configured from an injected environment mapping rather than
    :data:`os.environ`, so it cannot use :func:`get`.
    """
    value = environ.get(name)
    if value is not None:
        return value
    _check_removed_name(environ, name)
    return default


def _check_removed_name(environ: Mapping[str, str], name: str) -> None:
    """Called when ``name`` is unset: flag its removed spelling if that is set instead."""
    removed = REMOVED_PREFIX + name[len(PREFIX) :] if name.startswith(PREFIX) else None
    if not removed or removed not in environ:
        return
    message = f"{removed} was removed in 0.3.0 and is ignored; set {name} instead."
    if name in DESTINATION_SETTINGS:
        raise SkyportalError(
            f"{message} Refusing to fall back to the default host, which would then receive your credentials."
        )
    warnings.warn(message, UserWarning, stacklevel=4)


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
