# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Helm chart

- **Chart 0.3.2 accepts the agent token inline as `token.value`** (#89). The chart
  then creates the Secret itself, so a quick install is one `helm install`.
  `token.existingSecret` is unchanged, still recommended for production, and wins
  when both are set. A `checksum/token` pod annotation restarts the agent pods
  when an inline token changes. The chart still installs agent 0.3.0.

## 0.3.0

### Breaking

- **The pre-0.2.0 `SKYPORTAL_*` environment variables are removed.** They were
  deprecated in 0.2.0 and are now ignored: set the `SKYPORTALAI_*` name instead
  (for example `SKYPORTALAI_API_KEY`, `SKYPORTALAI_BASE_URL`,
  `SKYPORTALAI_AGENT_TOKEN`). An old name that is still set draws a warning naming
  its replacement, and without the new name the setting uses its default. Two
  exceptions:
  - **A leftover `SKYPORTAL_BASE_URL` or `SKYPORTAL_URL` is an error** (SDK, CLI and
    agent) until you set `SKYPORTALAI_BASE_URL` / `SKYPORTALAI_URL` or unset it. The
    default would be `https://app.skyportal.ai`, and a self-hosted install would
    send its credentials there.
  - **Credentials have no default:** the agent won't start without
    `SKYPORTALAI_AGENT_TOKEN`, and the SDK and CLI are unauthenticated without
    `SKYPORTALAI_API_KEY`.
- **The `skyportal` import package is removed.** Import from `skyportalai`. This
  also ends the import-name collision with the unrelated `skyportal` astronomy
  package on PyPI.

### Added

- **The agent monitors Kubernetes from inside the cluster.** Two new agent roles,
  selected by `SKYPORTALAI_AGENT_ROLE` (default `experiments`, the W&B/MLflow
  scanners, unchanged):
  - `cluster` runs the read-only `kubectl` reads SkyPortal asks for, using a
    read-only ServiceAccount, and uploads the raw output. It also runs the
    read-only `kubectl` you ask for in SkyPortal chat. It refuses any other
    command itself, whatever the server sends.
  - `node` (a DaemonSet pod per node) reports the node's CPU, memory, disk, load
    and GPUs from the host's `/proc` and NVML, with no Kubernetes API access.

  Both only call out to SkyPortal over HTTPS, and buffer on disk while it is
  unreachable. The `agent` extra adds `psutil` and `nvidia-ml-py`. Deploy with
  the Helm chart's `kubernetes.enabled`, which needs the 0.3.0 agent image
  (chart 0.3.1 and later default to it); see `deploy/agent/README.md`.

- **`/upload <path>` attaches a file to the chat.** Logs, CSV, JSON, YAML and
  images, up to 10 MB each. Ask about the file by name afterwards; the agent
  reads what you attach. Secrets in a log are redacted before it leaves the
  server's front door, so a stray API key is never stored. Requires a deployment
  with the `/api/v1/agent/chat/<id>/upload/` route.
- **Dropping a file on the terminal attaches it too.** A terminal types the
  path when you drag a file onto it, so a line that is nothing but existing
  absolute paths is treated as an upload rather than a message. Handles the
  quoting and backslash-escaping different terminals apply, `file://` URIs, and
  several files at once. A relative path stays a message, so asking about
  `vllm.log` from a directory containing one still asks.

### Changed

- **The observability agent identifies itself as `skyportalai-agent/<version>`.**
  The `User-Agent` on ingest requests still carried the `skyportal-agent` name
  the console script dropped in 0.2.0. SkyPortal reads the agent version off
  that header and has to know the new name (servers from September 2026 on
  accept both), so update the server before rolling this agent version out.

## 0.2.2

### Added

- **`skyportalai login` connects the terminal by itself.** It starts an
  authorization, prints a short code with the page to confirm it on, and waits;
  approving in the browser hands the key back and the terminal finishes on its
  own. No copying a key across windows, and nothing to paste. `/login` in the
  interactive shell does the same. Falls back to the old key-page paste when the
  deployment has no handshake endpoint, and `--token` (or `/token`) still pastes
  a key deliberately.

### Fixed

- **`skyportalai logout` is reachable again when the stored credential points
  at another deployment** — the state logout exists to clean up. Credential
  resolution runs above every command and used to abort with a traceback on a
  deployment mismatch, so the CLI stayed wedged until
  `~/.skyportalai/credentials.json` was deleted by hand. Resolution now records
  the problem instead of raising: `logout` works, commands that need a
  credential fail with a normal `Error:` line, and `skyportalai config show`
  reports it. An unparseable credential file is treated the same way — deleting
  a file should not require the file to be valid.
- Both deployment-mismatch messages (the public CLI and the interactive shell)
  now name the two URLs that disagree, the credential file, and the way to keep
  the credential instead — which depends on whether `--base-url`, an
  environment variable or `config.yaml` selected the target, since the first
  two outrank the file and advice to edit it would be a dead end.
- **`https://skyportal.ai` and `https://app.skyportal.ai` are no longer treated
  as two deployments.** The shell client normalizes the marketing host to the
  app host before saving a credential, so configuring the marketing spelling
  produced a permanent conflict that `logout` and `login` only recreated. The
  normalization now lives in one place (`_client.normalize_base_url`) and both
  comparisons use it.
- `skyportalai login` and `skyportalai github-token set` now name the instance
  they are connecting to before prompting for a secret, and warn when that
  target is a loopback address, naming both `config.yaml` and
  `skyportalai configure` in the warning. A `base_url` saved during local
  development outranked the shipped default with nothing on screen to show
  it, so a key could be created against the wrong instance (or against a page
  that never loaded).

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
