"""Environment and path resolution for the ``skyportalai`` surface.

Every knob was named ``SKYPORTAL_*`` before 0.2.0 and lives under
``~/.skyportal``. The canonical names are now ``SKYPORTALAI_*`` and
``~/.skyportalai``. The old names keep working for one release: reads fall back
to them and emit a :class:`DeprecationWarning`, and the config directory is
migrated in place the first time it is needed.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from pathlib import Path

PREFIX = "SKYPORTALAI_"
LEGACY_PREFIX = "SKYPORTAL_"

CONFIG_DIR_NAME = ".skyportalai"
LEGACY_CONFIG_DIR_NAME = ".skyportal"


def legacy_name(name: str) -> str:
    """Return the pre-0.2.0 spelling of a ``SKYPORTALAI_*`` variable."""
    if not name.startswith(PREFIX):
        raise ValueError(f"{name!r} is not a {PREFIX}* variable")
    return LEGACY_PREFIX + name[len(PREFIX) :]


def lookup(name: str, default: str | None = None) -> tuple[str | None, str | None]:
    """Resolve ``name``, falling back to its legacy spelling.

    Returns the value and the variable it actually came from, so callers can
    report the source without re-implementing the fallback.
    """
    value = os.environ.get(name)
    if value is not None:
        return value, name

    legacy = legacy_name(name)
    value = os.environ.get(legacy)
    if value is not None:
        warnings.warn(
            f"{legacy} is deprecated and will be removed in 0.3.0; use {name} instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        return value, legacy

    return default, None


def get(name: str, default: str | None = None) -> str | None:
    """Resolve ``name``, falling back to its legacy spelling."""
    return lookup(name, default)[0]


def get_from(environ: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    """Resolve ``name`` from an explicit mapping, falling back to its legacy spelling.

    The agent is configured from an injected environment mapping rather than
    :data:`os.environ`, so it cannot use :func:`get`.
    """
    value = environ.get(name)
    if value is not None:
        return value

    legacy = legacy_name(name)
    value = environ.get(legacy)
    if value is not None:
        warnings.warn(
            f"{legacy} is deprecated and will be removed in 0.3.0; use {name} instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        return value

    return default


def config_dir() -> Path:
    """Return the CLI config directory, migrating ``~/.skyportal`` if needed.

    The migration is a rename, so it runs once and is a no-op afterwards. If it
    cannot be performed the legacy directory is used as-is rather than silently
    starting from an empty configuration.
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
            f"Could not migrate {legacy} to {current}; continuing to use {legacy}. "
            "Move it manually before 0.3.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return legacy
    return current


def config_path(name: str, env_var: str) -> Path:
    """Return ``<config dir>/<name>``, honouring an explicit path override."""
    override = get(env_var)
    return Path(override).expanduser() if override else config_dir() / name
