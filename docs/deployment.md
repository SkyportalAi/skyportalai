# Skyportal CLI installation

## Launcher

From a source checkout:

```bash
./run.sh
```

The launcher installs [uv](https://docs.astral.sh/uv/) if it is missing, provisions the Python version pinned in `.python-version`, installs the project and its dependencies, and starts `skyportalai`.

Set `SKYPORTAL_VENV` to place the environment somewhere other than `.venv`.
Contributors who also run `poetry install` should do so: both default to
`.venv`, and the launcher installs runtime dependencies only, so running it
afterwards prunes `pytest`, `ruff` and the `agent` extra from that environment.

## Manual installation

```bash
uv sync --no-dev
uv run skyportalai
```

Without uv, note two requirements that the launcher otherwise handles. The
interpreter must be **3.11 or newer**, and pip must be new enough to support
[PEP 660](https://peps.python.org/pep-0660/) (pip 21.3+), because this project
builds with `poetry-core` rather than setuptools:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
skyportalai
```

An older pip fails with `Directory cannot be installed in editable mode`, and a
`python3` that is older than 3.11 fails with `requires a different Python`.

## Connect to production

The default application URL is `https://app.skyportal.ai`. Run `skyportalai login`, create an account API key on the browser page, and paste the `sk_` value into the hidden prompt.

`login` prints the instance it is connecting to before prompting, so check that
line if you are not sure which deployment the key will belong to. A saved
`base_url` outranks the default, and `login` warns when it is a loopback address.

To use an existing key non-persistently:

```bash
export SKYPORTALAI_API_KEY='sk_...'
skyportalai
```

`skyportalai` also reads `SKYPORTALAI_ACCESS_TOKEN`, which takes precedence over
`SKYPORTALAI_API_KEY`. The pre-0.2.0 `SKYPORTAL_API_KEY` and
`SKYPORTAL_ACCESS_TOKEN` still work in 0.2.x and warn on use; they are removed
in 0.3.0.

Avoid putting credentials directly in shell history. Do not use `agt_` observability-agent tokens with the CLI.

## Self-hosted deployment

```bash
skyportalai configure --portal-url https://skyportal.example
skyportalai login
```

The configured deployment and credential deployment must match.
Use HTTPS for every remote deployment. Plain HTTP is accepted only for
loopback development because API credentials are sent on every request.

## Automation CLI

The `skyportalai` command uses the same saved connection settings and
credentials, but is designed for scripts and structured output:

```bash
skyportalai config show
skyportalai --json chat send --server 42 "Show disk usage"
```

Use the interactive `skyportalai` command when a chat may require an approval
prompt.

## Noninteractive terminals

Disable animation for logs or automation:

```bash
SKYPORTALAI_NO_ANIMATION=1 skyportalai servers
```

Use `skyportalai ask` for a one-shot agent request. Interactive approvals require the persistent shell.

## Troubleshooting

### Virtual environment is broken or has no pip

Delete `.venv` and rerun `./run.sh`. uv rebuilds the environment from scratch
and does not depend on the interpreter's bundled pip.

### `Directory cannot be installed in editable mode`

Raised by a pip older than 21.3, which predates PEP 660 and cannot install a
`poetry-core` project in editable mode. Use `./run.sh`, or upgrade pip first
with `python -m pip install --upgrade pip`.

### `requires a different Python`

The interpreter is older than 3.11. `./run.sh` provisions a supported one; for
a manual install, create the venv with `python3.11` or newer explicitly.

### Access denied

- Confirm the URL is the application host, not a marketing site.
- Run `skyportalai login` and create a fresh `sk_` API key.
- Ensure the key is active and not expired or revoked.
- An `agt_` token cannot authorize account or chat operations.

### Login points at localhost

`login` echoes `Connecting to ...` and warns when the target is a local address:

```
Connecting to http://localhost:8000
Warning: base_url is a local address (http://localhost:8000) from
/home/you/.skyportalai/config.yaml — run skyportalai configure to change it.
```

That URL came from `config.yaml`, usually written during earlier local
development. A key created there belongs to the local instance, and the key page
will not load at all with no local server running. Point the CLI back at the
deployment you want before pasting a key:

```bash
skyportalai configure --portal-url https://app.skyportal.ai
```

### Stored credentials belong to another deployment

Repointing the CLI at a different instance leaves the saved key behind, and a
key is only valid on the instance that issued it:

```
Error: Stored credentials belong to another SkyPortal deployment
(http://localhost:8000), but the selected base URL is https://app.skyportal.ai.
Run 'skyportalai logout' to clear them (/home/you/.skyportalai/credentials.json),
or point the CLI back with 'skyportalai config set --base-url'.
```

Either clear the key with `skyportalai logout` and run `skyportalai login`
against the instance you want, or put the base URL back. `skyportalai config
show` reports the same conflict without needing a working credential.

### Browser login lands on another page

Return to the `/keys/` URL printed in the terminal. The product website also preserves the key-page return path across its production login exchange.

### Cloudflare blocks the request

Current releases send an explicit `Skyportal-CLI` user agent. Reinstall with `./run.sh` if an older process still identifies itself as Python's default URL client.
