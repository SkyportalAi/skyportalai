# Skyportal Agent

[![CI](https://github.com/SkyportalAi/skyportalai/actions/workflows/ci.yml/badge.svg)](https://github.com/SkyportalAi/skyportalai/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/skyportalai.svg)](https://pypi.org/project/skyportalai/)
[![Python versions](https://img.shields.io/pypi/pyversions/skyportalai.svg)](https://pypi.org/project/skyportalai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/SkyportalAi/skyportalai/blob/main/LICENSE)

An open-source AI infrastructure engineer that explains what changed before
production breaks.

![Skyportal diagnosing an infrastructure regression](https://raw.githubusercontent.com/SkyportalAi/skyportalai/main/docs/assets/skyportal-diagnose.gif)

Skyportal continuously builds a timeline of your AI infrastructure by observing
deployments, Kubernetes events, GPU metrics, configuration changes, logs, and
infrastructure updates. It correlates those events across your stack and
explains likely root causes.

Instead of searching through dozens of dashboards, ask:

- Why did GPU utilization suddenly drop?
- What changed before latency doubled?
- Which deployment caused this regression?
- Why is this model suddenly slower?
- Have we seen this incident before?

## How it works

```text
Observe infrastructure → Build a change timeline → Correlate regressions → Explain the likely cause
```

Skyportal connects a symptom to the changes that preceded it. A diagnosis can
compare a deployment with its previous release, measure the impact, identify
the most likely change, and report its confidence.

## Works with

![Kubernetes](https://img.shields.io/badge/Kubernetes-326CE5?logo=kubernetes&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?logo=prometheus&logoColor=white)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-000000?logo=opentelemetry&logoColor=white)
![vLLM](https://img.shields.io/badge/vLLM-4B32C3?logo=vllm&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![Bare metal](https://img.shields.io/badge/Bare_metal-FCC624?logo=linux&logoColor=black)
![NVIDIA GPUs](https://img.shields.io/badge/NVIDIA_GPUs-76B900?logo=nvidia&logoColor=white)

## Get started

Requires Python 3.11 or newer.

```bash
pip install "skyportalai[cli]"
skyportalai
```

`[cli]` installs the command-line client. For the Python SDK alone,
`pip install skyportalai` is enough from the next release on; today it still
installs the CLI's packages too.

Or run from a checkout:

```bash
git clone https://github.com/SkyportalAi/skyportalai.git
cd skyportalai
./run.sh
```

Inside the terminal, run `/login` once, list infrastructure with `/servers`,
select one or more hosts with `/server`, and ask what changed. For a Kubernetes
cluster, also choose the namespaces the agent may use with `/namespace` — every
command is refused until you do, as in the web app's Scope pill, so the terminal
won't send a question until each selected cluster has one.
`/namespace <name> ...` sets the list, `/namespace add` / `remove` edit it, and
with several clusters selected, `--cluster <name>` says which cluster a list is
for (`all` and `clear` without it apply to every selected cluster):

```text
skyportalai [connected] > diagnose the latest deployment
```

Useful commands:

```text
/login          Connect your Skyportal account
/servers        List available infrastructure
/server <id> [id ...]  Select one or more servers; the first is the default
/namespace <name> [name ...] | add|remove <name> ... | all | clear [--cluster <name>]
                Choose namespaces on a selected Kubernetes cluster
/permission [ask|autoapprove]  Show or change the shared approval setting
/status         Show the active context
/new            Start a new investigation
/resume         Continue the previous investigation
/help           Show every command
```

## Python SDK

Use the SDK when you want to start or automate an investigation from Python:

```python
from skyportalai import Skyportal

with Skyportal(api_key="sk-...") as client:
    client.set_permission_mode("autoapprove")
    chat = client.chat.create_chat(
        "What changed before GPU utilization dropped?",
        server_id=12,
    )
    result = chat.wait()
    print(result.status)
```

`ask` is the default. `autoapprove` submits each concrete approval through the
normal audited approval endpoint; it does not bypass read-only environments,
server scope, repository denials, or other backend safety policy. An explicit
`on_approval` callback takes precedence over the stored account setting. Waits
are indefinite by default so long-running single-host, multi-host, and
Kubernetes turns can finish; pass `timeout=` when an automation job needs a
finite deadline.

A command that targets a host or namespace outside the chat's scope waits on a
`scope` approval: approving adds the target to the chat and runs the command.
It is never autoapproved; only an `on_approval` callback answers it, and
`approval.scope` says what it adds. One whose `approval.scope["namespace"]` is
`None` asks which namespace to add: answer it with
`client.chat.approve(chat_id, approval_id, approval_type="scope", namespace="...")`.

To make the full multi-host scope available to the first turn, create the chat
with repeatable server scope and an active default:

```python
with Skyportal(api_key="sk-...") as client:
    chat = client.chat.create_chat(
        "Compare GPU health on all selected hosts",
        server_ids=[12, 18],
        active_server_id=12,
        selected_namespaces={18: ["default", "vllm"]},
    )
    result = chat.wait(on_approval=lambda approval: True)
```

The scope is an allowlist: the active server handles an ambiguous command, and
the agent broadcasts only when the prompt explicitly targets all selected
hosts. Use `{"18": ["__all__"]}` for every Kubernetes namespace, omit
`selected_namespaces` when no Kubernetes scope is needed, and use
`chat.select_servers(...)` between turns to replace an existing chat's scope.
When replacing scope, omitting namespace data preserves retained selections
while `{}` clears them. The singular `server_id=12` creation form remains
supported.

Set `SKYPORTALAI_API_KEY` instead of passing a key directly. The client also
supports `SKYPORTALAI_BASE_URL` for self-hosted deployments.

## Automation

The `skyportalai` command provides stable JSON output for scripts and CI:

```bash
skyportalai chat send --server 12 --wait "Diagnose the latest regression"
skyportalai chat send --server 12 --server 18 \
  --namespace 18=default --namespace 18=vllm --wait \
  "Compare GPU health on all selected hosts"
skyportalai --json chat messages 123
```

Set the full scope of an existing chat between turns with repeatable `--server`
options:

```bash
skyportalai chat select-servers 123 \
  --server 12 --server 18 --active-server 12 \
  --namespace 18=default --namespace 18=vllm
skyportalai chat send --chat-id 123 --wait "Compare all selected hosts"
```

Use `--clear-scope` to remove every selected server explicitly.

Check which account a key belongs to, and what it may do, before automating
with it:

```bash
skyportalai whoami
skyportalai --json whoami
```

`whoami` shows the account, the approval mode, read-only mode, the policy your
own hosts get per environment, and for each team your role and that role's
ceiling per environment (`denied` where it has no grant). It only reports: the
server still decides every command.

Run `skyportalai --help` for the complete command reference.

## Your own machine

Open `skyportalai` in a project folder and the agent can work on this machine,
the way Claude Code does: the model runs on Skyportal and the commands run in
your terminal. The first time, the CLI asks whether you trust the folder, and
trust covers that folder and everything inside it. Say yes and new chats run on
this machine, `/server auto` included; `/local` shows it, `/local off` stops it,
and `/server local` adds it to another chat's scope.

A trusted folder is only where commands start, not a boundary. A command can
`cd` anywhere and read or change anything you can: your files, your git
credentials, your cloud logins, the CLI's own saved sign-in. Only trust a folder
whose code you trust. The CLI won't connect from a filesystem or drive root
(`/`, `C:\`), and the trust prompt adds a warning when the folder is your home
folder.

Skyportal decides what runs: every command passes the same approval rules as on
any host. A command from a turn you typed in this CLI runs as soon as it is
approved. One from anywhere else, such as your browser continuing a chat this
CLI started, runs only after you answer `y` at this terminal. It shows the
command, its chat and where the turn came from, and asks `Run this on
<machine>? [y/N]`. `N`, Ctrl-C, or no answer in time tells the agent you
refused, and it never runs. You have a little under five minutes: a later
yes couldn't finish before Skyportal stops waiting for the result. No setting
skips the question, and `/permission autoapprove` doesn't answer it. Before
any command runs, even one you typed, the CLI checks with Skyportal that it
is still wanted, so if the turn is stopped while the question waits, a later
`y` runs nothing.
Nothing connects into your machine: the CLI fetches its commands over outbound
HTTPS, and only while it is open. It sends the repository's remote (without
credentials) and branch, never a full path. kubectl and helm don't run here,
because your kubeconfig can point at any cluster; add the cluster itself
instead.

Commands run in `bash`. On Windows that is the `bash` on your PATH unless it is
WSL's (System32's, or the Store's `WindowsApps` alias), then the bash that comes
with Git for Windows, and without either Windows PowerShell 5.1, which can't run
`a && b`.

The terminal prints every command it runs here, with the chat it came from and
its exit code, for example `▸ running: git log  (chat #123)`. A command is shown
as one line of up to 500 characters, with `(+N more characters)` after it when
it is longer, and a character a terminal would act on is written out (`\x1b`,
`\n`), so what you read is what runs. The `y/N` question shows the whole
command instead, with each run of five or more spaces counted (`⟨495 spaces⟩`)
so padding can't push the rest out of sight; a command longer than 10,000
characters isn't asked about and doesn't run. A chat this CLI started can also be
continued from your browser while the CLI is open, and its commands still show
up here. Ctrl-C at the prompt stops every command running here, including ones
started from a chat you continued in the browser. Ctrl-C while a turn runs stops
that turn and only its chat's commands.

Commands run without any `SKYPORTALAI_*` (or legacy `SKYPORTAL_*`) environment
variable, so the key you signed in with doesn't show in `printenv`; a command can
still read the files you can read, the CLI's saved sign-in included. A command
that writes more than 64 MiB to stdout or stderr is stopped (exit code 153) and
the chat is told why. If Skyportal removes the machine, or refuses the CLI's key,
the CLI says so once, stops fetching commands and drops `local#` from the prompt:
reconnect with `/local on` (after `/login`, for a refused key). `/logout`
disconnects the machine, and so does signing in with a new key, which may be
another account's; the next message connects it again with that key. While Skyportal
can't be reached the CLI keeps trying, waiting longer between tries, up to 30
seconds.

## Kubernetes clusters

Connect a cluster with its kubeconfig. The CLI sends the credential only to the
authenticated Skyportal API, where the same validation and encrypted storage as
the web application are used; kubeconfigs are never returned by lifecycle APIs.

```bash
skyportalai kubernetes connect production --kubeconfig ~/.kube/config --environment Production
skyportalai kubernetes list
```

Use the returned cluster ID as a normal chat target. Namespace scope is an
allowlist and every mutating command keeps the existing approval gate:

```bash
skyportalai chat send --server 17 --namespace 17=default --wait \
  "Restart the api deployment and verify the rollout"
```

Remove the stored cluster credential when it is no longer needed:

```bash
skyportalai kubernetes disconnect 17
```

## Ansible playbooks

Store validated playbooks in Skyportal and reuse them across account-owned SSH
targets. List responses omit YAML bodies; `show` retrieves one playbook when
you need to inspect or edit it.

```bash
skyportalai ansible create bootstrap --file playbook.yml --description "Base host setup"
skyportalai ansible list
skyportalai ansible show 4
skyportalai ansible update 4 --file playbook.yml
```

Deployments run through the ops agent and the normal command-approval policy.
The playbook executes on the selected SSH host with a temporary, mode-restricted
file that is removed after `ansible-playbook` exits.

```bash
skyportalai ansible deploy 4 --server 12
skyportalai chat wait 91
skyportalai chat approve 91 APPROVAL_ID --command "COMMAND_FROM_STATUS"
skyportalai ansible delete 4 --yes
```

The Python SDK exposes the same lifecycle as `client.ansible.create(...)`,
`.list()`, `.get(...)`, `.update(...)`, `.deploy(...)`, and `.delete(...)`.

## Observability agent

Install the agent and review the deployment guide before running it on
experiment volumes. `[agent]` does not need `[cli]`, and the agent never loads
the CLI:

```bash
pip install "skyportalai[agent]"
```

See [agent deployment and data handling](https://github.com/SkyportalAi/skyportalai/blob/main/docs/agent.md).
For Kubernetes installation, see the [Helm chart and container deployment guide](deploy/agent/README.md).

## Development

```bash
poetry install --all-extras
poetry run pytest
poetry run ruff check .
poetry check --strict
```

See [CONTRIBUTING.md](https://github.com/SkyportalAi/skyportalai/blob/main/CONTRIBUTING.md) to contribute, and
[RELEASING.md](https://github.com/SkyportalAi/skyportalai/blob/main/docs/RELEASING.md) to cut a release. Report security issues
privately using [SECURITY.md](https://github.com/SkyportalAi/skyportalai/blob/main/SECURITY.md).

## License

[MIT](https://github.com/SkyportalAi/skyportalai/blob/main/LICENSE)
