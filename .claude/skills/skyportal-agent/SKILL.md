---
name: skyportal-agent
description: Drive the skyportalai CLI to diagnose infrastructure regressions — GPU utilization drops, latency spikes, deployment/config changes, Kubernetes incidents. Use whenever the user asks to diagnose, investigate, or ask "what changed" about servers, clusters, or deployments that skyportalai has visibility into, or asks to connect a Kubernetes cluster, manage chat approvals, or script skyportalai in CI. Covers `skyportalai chat`/`kubernetes`/`config` subcommands, multi-host scope, and the exit-code contract.
---

# skyportal-agent — drive the skyportalai CLI

`skyportalai` is a CLI + Python SDK that talks to the SkyPortal ops agent: it
observes deployments, Kubernetes events, GPU metrics, and logs, then answers
"what changed before X broke." This skill teaches the concrete command
surface so you don't have to re-derive flags or guess at the approval flow.

Two console scripts exist — prefer `skyportalai` (stable JSON, scriptable
Typer CLI). `skyportal` is a separate interactive shell package; do not
conflate the two when reading source or writing commands.

## Before running anything

Check auth is resolvable, since every command needs it:

```bash
skyportalai config show
```

Key resolution order: `SKYPORTAL_API_KEY` env var → `SKYPORTAL_ACCESS_TOKEN`
env var → stored `~/.skyportal/credentials.json` token (only if its saved
`base_url` matches the effective one — otherwise it errors rather than
silently talking to the wrong deployment). If `authenticated: false`, tell
the user to set `SKYPORTAL_API_KEY` rather than guessing at a login flow —
there is no `skyportalai login` subcommand in this package.

Add `--json` as the **first** argument (it's a root-level option, not a
per-subcommand one) whenever output will be parsed rather than read by a
human: `skyportalai --json chat send ...`.

## Starting or continuing an investigation

```bash
skyportalai chat send --server 12 --wait "What changed before GPU utilization dropped?"
```

- `--wait` blocks until the turn settles (a real answer, an error, or a
  pending approval) instead of returning `status: processing` immediately.
  Always pass it for one-shot scripted use; omit it only if you intend to
  poll separately with `chat wait <id>`.
- `--timeout` is unset by default, i.e. **waits indefinitely** — a
  multi-host or Kubernetes turn can legitimately run for minutes. Only pass
  `--timeout` when the caller has a hard deadline (e.g. a CI job); otherwise
  leave it unset rather than guessing a number.
- Follow-up in the same chat: `--chat-id <id>` instead of `--server`. Scope
  flags (`--server`, `--active-server`, `--active-host`, `--namespace`) are
  **creation-only** — combining them with `--chat-id` is a hard `Exit 2`, not
  a merge.

### Multi-host and Kubernetes scope

Scope is an allowlist, not a broadcast target — the active server/host
resolves an ambiguous instruction; every other selected host only acts when
the prompt explicitly says "all" / names them.

```bash
skyportalai chat send \
  --server 12 --server 18 --active-server 12 \
  --namespace 18=default --namespace 18=vllm \
  --wait "Compare GPU health on all selected hosts"
```

- `--namespace` is always `SERVER_ID=NAMESPACE`, repeatable, and the
  `SERVER_ID` **must** also appear in a `--server` flag on the same
  invocation — a namespace for a server not in scope is rejected before any
  network call (`Exit 2`).
- Use the literal namespace value `__all__` for cluster-wide scope on one
  server, e.g. `--namespace 18=__all__`.
- To change scope on an existing chat, use `chat select-servers`, never
  re-pass `--server` on `chat send --chat-id`:

```bash
skyportalai chat select-servers 123 \
  --server 12 --server 18 --active-server 12 \
  --namespace 18=default --namespace 18=vllm
```

  `select-servers` refuses to run while the chat is `processing`,
  `uninitialized`, or `awaiting_approval` — finish or resolve the current
  turn first. Omitting `--namespace` entirely **preserves** existing
  namespace selections; pass `--clear-namespaces` to explicitly wipe them, or
  `--clear-scope` to remove every server (mutually exclusive with
  `--server`).

## The exit-code contract (read this before scripting)

`chat send --wait` and `chat wait` do **not** raise on a pending approval —
they exit non-zero on purpose so shell scripts can branch on it:

| Exit code | Meaning |
|---|---|
| `0` | Turn settled cleanly (includes ordinary text answers). |
| `2` | `status == "awaiting_approval"` — the agent wants to run something and is waiting on a human/script decision. **Not a failure.** |
| `1` | `status == "error"`. |

A script that treats any non-zero exit as failure will misreport "the agent
needs your approval" as a crash. Check `status` in the JSON payload (or the
specific exit code) before deciding an investigation failed.

## Approvals

```bash
skyportalai chat status 123        # see pending_approvals count + IDs
skyportalai chat approve 123 <approval_id> --type bash_command
skyportalai chat reject 123 <approval_id> --reason "not on this host"
```

`--type` defaults to `bash_command`; the other value in use is `plan`. Pass
`--command` on `approve` only when the server's stored approval requires an
exact command match — check `chat status` output for what it expects rather
than assuming.

## Kubernetes clusters

```bash
skyportalai kubernetes connect production --kubeconfig ~/.kube/config --environment Production
skyportalai kubernetes list
skyportalai kubernetes disconnect 17 --yes
```

- The kubeconfig is read client-side and capped at 1 MiB; pass `-` to read
  it from stdin instead of a path (useful piping from a secrets manager
  without writing a temp file).
- The returned cluster `id` is used exactly like a server ID in
  `chat send --server <id> --namespace <id>=<ns>` — there's no separate
  "target a cluster" flag.
- `disconnect` requires `--yes` in `--json` mode (there's no TTY to confirm
  against) — always pass it in scripts.

## Common mistakes to avoid

- Don't combine `--server` with `--chat-id` on `chat send` — creation-only
  flags, hard error.
- Don't assume a non-zero exit means failure — check for `2`
  (`awaiting_approval`) first.
- Don't invent a `skyportalai login` command — auth is env-var or stored
  credentials only, from this package's CLI.
- Don't call `chat select-servers` while a turn is mid-flight; check
  `chat status` first if unsure.
- Don't confuse this package's `skyportalai` Typer CLI with the separate
  `skyportal` interactive shell package when citing source files — they live
  in different top-level packages (`skyportalai/cli/` vs `skyportal/`) with
  different capabilities.
