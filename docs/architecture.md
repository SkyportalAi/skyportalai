# Skyportal CLI architecture

The distribution exposes a single frontend. `skyportalai` is a Typer interface
covering both the persistent prompt-toolkit terminal described below and the
script-friendly subcommands that call the public `skyportalai.Skyportal` SDK
resources, with optional stable JSON output. Run it with no arguments to enter
the interactive shell.

Before 0.2.0 there were two commands: a Click app named `skyportal` and a Typer
app named `skyportalai`, with disjoint command sets. They were merged by
porting the Click commands to Typer; `skyportal` no longer exists.

## Components

- `skyportalai.cli.main`: the Typer root. Its `@app.callback()` declares the
  global `--json` / `--base-url` options and is the only place `context.obj`
  is set, so every subcommand depends on it running.
- `skyportalai.cli.shell_commands`: configuration, login, one-shot questions,
  server listing, and the interactive shell, ported from the former Click app.
- `skyportalai.cli.{config,chat,ansible,kubernetes}`: script-friendly
  subcommands backed by the public SDK.
- `skyportalai.shell.interactive`: persistent prompt-toolkit command center with history, completion, Markdown output, chat cursors, server context, and approvals.
- `skyportalai.shell.portal`: standard-library HTTP client for credential validation and the headless agent REST API.
- `skyportalai.shell.animation`: responsive Rich ANSI branding and startup animation.
- `skyportalai.shell.config`: application URL and timeout configuration under `~/.skyportalai`.

## Authentication

The website browser session is not copied into the terminal. The CLI opens `/keys/`, accepts the one-time raw account API key through hidden input, validates it, and only then stores it.

Supported credentials:

- `sk_`: named account API key
- `skt_`: short-lived access token issued from an account API key

An `agt_` token is deliberately rejected. It is a host-bound observability upload credential and does not carry account/chat authority.

Production traffic targets `https://app.skyportal.ai`. `https://skyportal.ai` is the marketing host and is normalized to the application host.

Authenticated API requests do not follow redirects, and remote base URLs must
use HTTPS. These constraints prevent Bearer credentials from being forwarded to
an unexpected origin or sent in cleartext.

## Conversation flow

1. The first message calls `POST /api/v1/agent/chat/` and stores the returned chat ID.
2. The CLI polls `GET /api/v1/agent/chat/{id}/status/` until the turn settles;
   a finite timeout is opt-in for automation.
3. While it polls, it fetches and renders new messages from
   `GET /api/v1/agent/chat/{id}/messages/` using the last sequence as a cursor.
4. Follow-up messages call `POST /api/v1/agent/chat/{id}/message/`.
5. If status is `awaiting_approval`, the CLI shows the requested action and submits the user's decision to the approval endpoint.
6. `/new` clears only local chat context and starts a new chat on the next message.

`/permission ask|autoapprove` reads or replaces the account-wide approval
preference shared with the website and public SDK. Autoapproval still sends one
typed decision at a time and waits for the durable checkpoint to advance before
submitting another. Backend read-only, target scope, repository, and environment
policy are always rechecked.

## Server context

`GET /api/v1/experiments/my-servers/` provides the authenticated account's
owned servers. Chat creation accepts either the backward-compatible
`server_id` field or an atomic first-turn scope with `selected_server_ids`,
`active_server_id`, `active_host_id`, and `selected_namespaces`. The singular
and plural forms are mutually exclusive. A single selection can also be sent
to `/select-server/` for an existing chat.

For an existing chat that is not actively processing, `POST
/api/v1/agent/chat/{id}/select-servers/` replaces the complete execution
allowlist. It can also choose the default execution server, preserve or update
the terminal/Jupyter binding, and set per-server Kubernetes namespace scope.
Omitted namespace data preserves choices for retained servers, `{}` clears all
namespace choices, and `__all__` represents cluster-wide namespace access.
Multi-server scope does not make every command a broadcast: the prompt must
explicitly target all selected hosts.

The plural creation fields are persisted before first-turn processing starts,
so every tool sees the complete allowlist from the beginning. Scope changes on
an existing chat still happen only while it is not actively processing. The
website serializes REST and browser turns and scope mutations with the same
token-owned Redis lease, rejects changes while approvals are pending, and
renews the lease during long turns; losing ownership cancels the local worker
instead of allowing two clients to execute against different scopes.
The interactive `skyportalai` shell accepts multiple IDs with `/server 12 18`,
and the one-shot `skyportalai ask` command accepts repeated `--server` options.

## Local state

- Configuration: `~/.skyportalai/config.yaml`
- Credentials: `~/.skyportalai/credentials.json` (mode `0600`)
- Prompt history: `~/.skyportalai/history` (mode `0600`)

A pre-0.2.0 `~/.skyportal` directory is renamed to `~/.skyportalai` the first
time a path under it is resolved.

Environment variables can override each path for tests and managed installations.
