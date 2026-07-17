# Contributing to skyportalai

Thank you for helping improve the SkyPortal Python SDK, command-line clients,
and observability agent.

## Report an issue

Use GitHub issues for reproducible bugs and focused feature requests. Bug
reports should include:

- the command or minimal code needed to reproduce the problem;
- expected and actual behavior;
- the output of `skyportalai --version` and `python --version`;
- the operating system and relevant deployment details.

Remove API keys, access tokens, hostnames, and private run metadata before
posting. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Development setup

Python 3.11 or newer and [Poetry](https://python-poetry.org/) are required.

```console
poetry install --all-extras
poetry run pytest
poetry run ruff check .
poetry check --strict
```

The normal test suite must not contact live services. Use `requests-mock`,
temporary paths, and environment isolation.

## Pull requests

1. Branch from `main` and keep the change focused.
2. Add or update tests for behavior changes.
3. Update public documentation when commands, configuration, or APIs change.
4. Run the checks above before opening the pull request.
5. Use a concise conventional title such as `feat: ...`, `fix: ...`,
   `docs: ...`, or `chore: ...` so the project history stays readable.

All pull requests are reviewed by the Skyportal team before merging.

Public functions should be typed. Preserve existing APIs unless a breaking
change is intentional and documented. Never commit credentials or fixtures
copied from a real account.

## Supported Python versions

CI covers Python 3.11, 3.12, and 3.13.

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
