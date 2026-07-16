# skyportalai

[![CI](https://github.com/SkyportalAi/skyportalai/actions/workflows/ci.yml/badge.svg)](https://github.com/SkyportalAi/skyportalai/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/skyportalai.svg)](https://pypi.org/project/skyportalai/)
[![Python](https://img.shields.io/pypi/pyversions/skyportalai.svg)](https://pypi.org/project/skyportalai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

The official Python SDK and persistent terminal client for the
[SkyPortal](https://skyportal.ai) API.

> **Project status:** Alpha. APIs may evolve before 1.0; changes are documented
> in GitHub releases.

```
User app  →  skyportalai (SDK)  →  SkyPortal HTTP API
```

## Install

```bash
pip install skyportalai
# or
poetry add skyportalai
```

Requires Python 3.11+.

The local observability agent has one additional dependency set:

```bash
pip install "skyportalai[agent]"
```

## Quickstart

```python
from skyportalai import Skyportal

client = Skyportal(api_key="sk-...")   # or set SKYPORTAL_API_KEY
user = client.me()
print(user.name)
```

`Skyportal` can be used as a context manager when the SDK owns the HTTP
session:

```python
with Skyportal(api_key="sk-...") as client:
    print(client.me().name)
```

## Drive the ops agent

`client.chat` wraps the headless agent API: create a chat, poll it, resolve
approvals, and read the run's messages — the agent itself runs server-side.

```python
chat = client.chat.create_chat("check disk usage on the training node", server_id=12)

# Poll until the agent settles; approve any command it asks to run.
status = chat.wait(on_approval=lambda a: True)          # True approves, False rejects
print(status.status)                                    # "idle"

for m in chat.messages().messages:
    print(f"{m.role}: {m.content}")
```

Without an `on_approval` callback, `wait()` returns as soon as the agent needs
a decision, and you resolve it yourself:

```python
status = chat.wait()
if status.status == "awaiting_approval":
    for approval in status.pending_approvals:
        print("agent wants to run:", approval.command)
        chat.approve(approval.approval_id, command=approval.command)
    status = chat.wait()
```

Follow-ups go through `chat.send("...")`; `chat.cancel()` stops an active run.
Read-only introspection mirrors the observability endpoints: `chat.events()`,
`chat.tool_calls()`, `chat.reasoning()`, `chat.plan()`, `chat.evaluations()`,
`chat.environment()`.

`wait()` raises `WaitTimeoutError` if the agent is still busy past `timeout`
(default 300s).

## Configuration

| Argument | Env var | Default | Notes |
|---|---|---|---|
| `api_key` | `SKYPORTAL_API_KEY` | — | required; sent as `Authorization: Bearer <key>` |
| `base_url` | `SKYPORTAL_BASE_URL` | `https://app.skyportal.ai` | API root; trailing slash optional |
| `timeout` | — | `30.0` | per-request seconds |
| `max_retries` | — | `2` | retries for GET on network error / 5xx |

```python
client = Skyportal(api_key="sk-...", base_url="http://localhost:8000", timeout=10)
```

Remote targets must use HTTPS because every request carries a Bearer key.
Plain HTTP is accepted for loopback development only. Valid self-hosted HTTPS
deployments are fully supported.

## Errors

Every failure is a `skyportalai` exception — a raw `requests` error never escapes:

```python
from skyportalai import Skyportal, AuthenticationError, APIConnectionError, APIError

try:
    Skyportal(api_key="bad").me()
except AuthenticationError:
    ...   # 401/403, or the key was rejected
except APIConnectionError:
    ...   # network failure / timeout
except APIError as e:
    ...   # other non-2xx; e.status_code, e.body
```

Hierarchy: `SkyportalError` ▸ `APIConnectionError`, `APIStatusError` ▸ `AuthenticationError`, `APIError`.

## Command-line clients

The package installs two complementary commands:

- `skyportal` is the interactive command center, with login, server selection,
    persistent chat state, and approval prompts.
- `skyportalai` is the script-friendly Typer CLI, with stable `--json` output
    for chat and configuration operations.

### Interactive terminal

```bash
skyportal                    # start the interactive command center
skyportal start              # same as above
skyportal login              # browser-guided API-key setup
skyportal login --token      # paste an existing credential
skyportal ask "List my hosts"
skyportal ask --server 42 "Show disk usage"
skyportal servers
skyportal logout
skyportal configure --portal-url https://your-skyportal.example
```

For a self-installing local launcher, clone the repository and run `./run.sh`.
It provisions a virtual environment, installs this package, displays the
animated Skyportal astronaut, and opens the command center.

At the interactive prompt, `/login` opens the API-key page and guides you
through connecting the terminal. Credentials are validated before being stored
in `~/.skyportal/credentials.json` with user-only permissions.

Interactive commands:

```text
/help                 Show commands
/login                Open API-key setup and connect
/token                Reopen API-key setup and paste a credential
/logout               Remove the saved credential
/status               Show API, chat, and server context
/new                  Start a new agent chat
/servers              List owned servers
/server <id>          Select a server for agent execution
/server auto          Let the agent choose a server
/clear                Clear the terminal
/exit                 Exit
```

CLI configuration is stored in `~/.skyportal/config.yaml`. Environment
overrides include `SKYPORTAL_URL`, `SKYPORTAL_ACCESS_TOKEN`,
`SKYPORTAL_CONFIG_PATH`, `SKYPORTAL_CREDENTIALS_PATH`,
`SKYPORTAL_HISTORY_PATH`, `SKYPORTAL_NO_ANIMATION`, and
`SKYPORTAL_ANIMATION_SPEED`.

See [CLI architecture](docs/architecture.md) and
[CLI deployment](docs/deployment.md) for more detail.

### Automation CLI

```bash
skyportalai config show
skyportalai chat send --server 42 --wait "Show disk usage"
skyportalai chat status 123
skyportalai --json chat messages 123
```

Run `skyportalai --help` or `skyportalai chat --help` for the full command
reference. It uses `SKYPORTAL_API_KEY` first and can also read credentials saved
by `skyportal login`.

## Observability agent

`skyportal-agent` discovers W&B and MLflow experiment metadata, buffers it in a
disk-backed queue, and uploads it with bounded retries. Its config/tag redaction
removes credential-like values before persistence, but operators must still
restrict scan roots and protect the state directory.

See [observability agent deployment and data handling](docs/agent.md) before
running it on experiment volumes.

## Development

```bash
poetry install --all-extras
poetry run pytest
poetry run ruff check .
poetry check --strict
```

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for
development setup, tests, and the pull request process, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations. Supported
Python versions are 3.11, 3.12, and 3.13.

## Security

Do not open a public issue for a vulnerability. Follow the private reporting
instructions in [SECURITY.md](SECURITY.md).

## License

Released under the [MIT License](LICENSE).
