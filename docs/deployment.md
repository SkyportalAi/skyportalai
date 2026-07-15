# Skyportal CLI installation

## Launcher

From a source checkout:

```bash
./run.sh
```

The launcher creates `.venv`, installs missing Debian/Ubuntu virtual-environment or pip support when possible, installs the package in editable mode, and starts `skyportal`.

## Manual installation

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
skyportal
```

Python 3.11 or newer is required.

## Connect to production

The default application URL is `https://app.skyportal.ai`. Run `skyportal login`, create an account API key on the browser page, and paste the `sk_` value into the hidden prompt.

To use an existing key non-persistently:

```bash
export SKYPORTAL_ACCESS_TOKEN='sk_...'
skyportal
```

Avoid putting credentials directly in shell history. Do not use `agt_` observability-agent tokens with the CLI.

## Self-hosted deployment

```bash
skyportal configure --portal-url https://skyportal.example
skyportal login
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

Use the interactive `skyportal` command when a chat may require an approval
prompt.

## Noninteractive terminals

Disable animation for logs or automation:

```bash
SKYPORTAL_NO_ANIMATION=1 skyportal servers
```

Use `skyportal ask` for a one-shot agent request. Interactive approvals require the persistent shell.

## Troubleshooting

### Virtual environment has no pip

Rerun `./run.sh`; it tries `ensurepip`, Debian/Ubuntu Python packages, and the official pip bootstrap in sequence.

### Access denied

- Confirm the URL is the application host, not a marketing site.
- Run `skyportal login` and create a fresh `sk_` API key.
- Ensure the key is active and not expired or revoked.
- An `agt_` token cannot authorize account or chat operations.

### Browser login lands on another page

Return to the `/keys/` URL printed in the terminal. The product website also preserves the key-page return path across its production login exchange.

### Cloudflare blocks the request

Current releases send an explicit `Skyportal-CLI` user agent. Reinstall the editable package with `./run.sh` if an older process still identifies itself as Python's default URL client.
