# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.2.1

### Fixed

- **`./run.sh` works again from a fresh clone.** It previously failed with
  `Directory cannot be installed in editable mode`, then
  `.venv/bin/skyportalai: No such file or directory`. `python3 -m venv` seeds
  whatever pip ships in the host interpreter's `ensurepip` wheel, and a pip
  older than 21.3 predates [PEP 660](https://peps.python.org/pep-0660/), so it
  cannot install a `poetry-core` project in editable mode. The launcher now
  delegates to [uv](https://docs.astral.sh/uv/), which provisions the
  interpreter pinned in `.python-version` and needs no pip bootstrap at all.
  `SKYPORTAL_VENV` is still honoured.

### Documentation

- `docs/deployment.md` described the removed pip recovery ladder and said the
  launcher starts `skyportal`, a command that no longer exists as of 0.2.0.
- The documented manual install could not work either: it used `python3`, which
  may be older than the required 3.11, and did not upgrade pip before an
  editable install. Both failure modes are now called out with their exact
  error messages, and `uv sync` is given as the simpler path.
- `CONTRIBUTING.md` notes that the launcher and Poetry share `.venv`, so
  running the launcher after `poetry install` prunes the dev tools from that
  environment.

## 0.2.0

Everything user-facing is now named `skyportalai`.

### Breaking

- **The `skyportal` command is removed.** Its seven commands (`configure`,
  `login`, `logout`, `ask`, `servers`, `start`, `github-token`) are now
  subcommands of `skyportalai`, which exposes all eleven. Running `skyportalai`
  with no arguments enters the interactive shell, as bare `skyportal` did.
- **`skyportal-agent` is renamed to `skyportalai-agent`.**
- The `skyportal.cli` module is removed along with the command.

### Deprecated

These keep working in 0.2.x and will be removed in 0.3.0. Each emits a
`DeprecationWarning` naming its replacement. Both console scripts enable that
warning explicitly, because Python hides `DeprecationWarning` by default and
the notices would otherwise never reach the people who need to migrate.

- `SKYPORTAL_*` environment variables — use `SKYPORTALAI_*`. All 24 are
  supported, and the canonical name wins when both are set.
- The `skyportal` import package — use `skyportalai`. The shim re-exports
  `__version__` only and warns on import; the submodules are NOT shimmed, so
  `skyportal.shell`, `skyportal.portal`, `skyportal.animation` and
  `skyportal.config` raise `ModuleNotFoundError`. They now live at
  `skyportalai.shell.{interactive,portal,animation,config}`.

### Changed

- The config directory moved from `~/.skyportal` to `~/.skyportalai`. An
  existing `~/.skyportal` is renamed the first time a path under it is
  resolved, so credentials, history and configuration are preserved. If the
  rename fails the old directory keeps being used rather than silently
  starting empty.

### Unchanged on purpose

- The agent state directory stays `/var/lib/skyportal-agent`. Renaming it would
  orphan the on-disk delivery spool of every deployed agent, abandoning queued
  telemetry. `SKYPORTALAI_AGENT_STATE_DIR` still overrides it.

### Internal

- The two CLIs were merged by porting the Click commands to Typer. Typer
  vendors its own Click (`typer._click`), so `TyperGroup` is not a
  `click.Group` and their command objects cannot be mixed at runtime; porting
  was the only approach that could work.
- Added `tests/test_cli_entrypoint.py`, which drives the real console-script
  entry point and asserts exit codes. Every pre-existing CLI test imported the
  Typer app directly, so the compiled root command had no coverage.

## 0.1.1

First release published to PyPI.

## 0.1.0

Never published; the release workflow failed before uploading.
