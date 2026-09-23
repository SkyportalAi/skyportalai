"""Public command-line interface for the Skyportal SDK."""


def main() -> None:
    """Load the Typer application only when the console script runs."""
    from .main import main as run

    run()


__all__ = ["main"]