# Skyportal CLI architecture

The distribution exposes two frontends. `skyportal` is the persistent Click /
prompt-toolkit terminal described below. `skyportalai` is a Typer interface for
automation and calls the public `skyportalai.Skyportal` SDK resources, with
optional stable JSON output.

## Components

- `skyportal.cli`: Click entry points for configuration, login, one-shot questions, server listing, and the interactive shell.
- `skyportal.shell`: persistent prompt-toolkit command center with history, completion, Markdown output, chat cursors, server context, and approvals.
- `skyportal.portal`: standard-library HTTP client for credential validation and the headless agent REST API.
- `skyportal.animation`: responsive Rich ANSI branding and startup animation.
- `skyportal.config`: application URL and timeout configuration under `~/.skyportal`.
- `skyportalai.cli`: script-friendly configuration and chat subcommands backed
  by the public SDK.

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
2. The CLI polls `GET /api/v1/agent/chat/{id}/status/` with a bounded timeout.
3. It fetches new messages from `GET /api/v1/agent/chat/{id}/messages/` using the last sequence as a cursor.
4. Follow-up messages call `POST /api/v1/agent/chat/{id}/message/`.
5. If status is `awaiting_approval`, the CLI shows the requested action and submits the user's decision to the approval endpoint.
6. `/new` clears only local chat context and starts a new chat on the next message.

## Server context

`GET /api/v1/experiments/my-servers/` provides the authenticated account's owned servers. A selected server is included when creating a chat or sent to `/select-server/` for an existing chat.

## Local state

- Configuration: `~/.skyportal/config.yaml`
- Credentials: `~/.skyportal/credentials.json` (mode `0600`)
- Prompt history: `~/.skyportal/history` (mode `0600`)

Environment variables can override each path for tests and managed installations.
