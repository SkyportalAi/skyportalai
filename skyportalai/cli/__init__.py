"""Public command-line interface for the Skyportal SDK."""


def main() -> None:
    """Load the Typer application only when the console script runs."""
    from skyportalai import _env

    # Python hides DeprecationWarning by default, which would silence every
    # legacy SKYPORTAL_* notice for the users who need to act on it.
    _env.enable_deprecation_warnings()

    from .main import main as run

    run()


__all__ = ["main"]