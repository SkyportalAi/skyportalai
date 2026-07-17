# Skyportal Agent

[![CI](https://github.com/SkyportalAi/skyportalai/actions/workflows/ci.yml/badge.svg)](https://github.com/SkyportalAi/skyportalai/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/skyportalai.svg)](https://pypi.org/project/skyportalai/)
[![Python](https://img.shields.io/pypi/pyversions/skyportalai.svg)](https://pypi.org/project/skyportalai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An open-source AI infrastructure engineer that explains what changed before
production breaks.

![Skyportal diagnosing an infrastructure regression](docs/assets/skyportal-diagnose.gif)

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

- Kubernetes
- Prometheus
- OpenTelemetry
- vLLM
- Ray
- Docker
- Bare metal
- NVIDIA GPUs

## Get started

Requires Python 3.11 or newer.

```bash
pip install skyportalai
skyportal
```

Inside the terminal, run `/login` once, select a server with `/servers`, and ask
what changed:

```text
skyportal [connected] > diagnose the latest deployment
```

Useful commands:

```text
/login          Connect your Skyportal account
/servers        List available infrastructure
/server <id>    Select a server
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
    chat = client.chat.create_chat(
        "What changed before GPU utilization dropped?",
        server_id=12,
    )
    result = chat.wait(on_approval=lambda approval: True)
    print(result.status)
```

Set `SKYPORTAL_API_KEY` instead of passing a key directly. The client also
supports `SKYPORTAL_BASE_URL` for self-hosted deployments.

## Automation

The `skyportalai` command provides stable JSON output for scripts and CI:

```bash
skyportalai chat send --server 12 --wait "Diagnose the latest regression"
skyportalai --json chat messages 123
```

Run `skyportalai --help` for the complete command reference.

## Observability agent

Install the collector dependencies and review the deployment guide before
running the agent on experiment volumes:

```bash
pip install "skyportalai[agent]"
```

See [agent deployment and data handling](docs/agent.md).

## Development

```bash
poetry install --all-extras
poetry run pytest
poetry run ruff check .
poetry check --strict
```

See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute. Report security issues
privately using [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
