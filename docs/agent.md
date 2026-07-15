# Observability agent

`skyportal-agent` discovers local Weights & Biases and MLflow runs, stores new
run batches in a bounded disk queue, and sends them to SkyPortal. It is intended
for a dedicated host or container with only the experiment volumes it needs.

## Install and run

```console
pip install "skyportalai[agent]"
export SKYPORTAL_AGENT_TOKEN='agt_...'
skyportal-agent
```

The agent exposes `GET /healthz` on port 8080 and handles `SIGTERM`/`SIGINT` with
a final delivery attempt.

## Configuration

| Environment variable | Default | Description |
|---|---:|---|
| `SKYPORTAL_AGENT_TOKEN` | required | Host-bound observability upload token |
| `SKYPORTAL_BASE_URL` | `https://app.skyportal.ai` | SkyPortal API root |
| `SKYPORTAL_AGENT_INTERVAL_SECONDS` | `60` | Seconds between scans |
| `SKYPORTAL_AGENT_STATE_DIR` | `/var/lib/skyportal-agent` | Catalog and delivery spool |
| `SKYPORTAL_AGENT_QUEUE_MAX_BATCHES` | `1000` | Maximum on-disk batches |
| `SKYPORTAL_AGENT_HEALTHZ_PORT` | `8080` | Liveness endpoint port |
| `SKYPORTAL_AGENT_ENABLE_WANDB` | `true` | Enable W&B discovery |
| `SKYPORTAL_AGENT_WANDB_DIR` | auto-discover | Restrict W&B scanning to one root |
| `SKYPORTAL_AGENT_ENABLE_MLFLOW` | `true` | Enable MLflow discovery |
| `SKYPORTAL_AGENT_MLFLOW_DIR` | auto-discover | Restrict filesystem MLflow scanning |
| `SKYPORTAL_AGENT_MLFLOW_MODE` | `filesystem` | `filesystem` or `rest` |
| `SKYPORTAL_AGENT_MLFLOW_TRACKING_URI` | unset | Tracking server for REST mode |
| `SKYPORTAL_AGENT_CLUSTER_NAME` | unset | Optional deployment label |

For REST-backed MLflow discovery:

```console
export SKYPORTAL_AGENT_MLFLOW_MODE=rest
export SKYPORTAL_AGENT_MLFLOW_TRACKING_URI=https://mlflow.example
skyportal-agent
```

## Data and privacy

Run IDs, names, status, timestamps, parameters, tags, metrics, local run paths,
and available W&B script content may be queued on disk and uploaded. Restrict
the mounted/search roots and review the deployment's data policy before use.
Credential-like dictionary values (for example `api_key`, `access_token`,
`password`, and `client_secret`) are replaced with `[REDACTED]` before catalog
or spool persistence, but secret material embedded in arbitrary source text
cannot be detected reliably.

The state directory may contain proprietary experiment metadata. Give it the
same access controls and retention treatment as the source experiment data.
