# Deploying the SkyPortal observability agent

The agent is a small daemon that runs in your cluster and reports to SkyPortal
over outbound HTTPS. It does two jobs:

- **Experiment runs:** scans Weights & Biases and MLflow stores and ships run
  metadata. On by default.
- **Kubernetes monitoring** (chart 0.3.0+, agent 0.3.0+): with
  `kubernetes.enabled`, it reports the cluster's pods, events, workloads and crash
  logs, plus every node's CPU, memory, disk, load and GPUs. See
  [Kubernetes monitoring](#kubernetes-monitoring).

Nothing connects in to your cluster, and SkyPortal never holds a kubeconfig: you
install the chart once with your own access, and from then on the agent uses its
own read-only ServiceAccount.

This is the operator guide for running it on Kubernetes from the published Helm
chart, or without Helm from the plain manifests in `manifests/`.

Both artifacts are built and published by CI from this directory; maintainers
cut releases as described in [RELEASING.md](RELEASING.md).

| Artifact | Reference |
|---|---|
| Helm chart | `oci://ghcr.io/skyportalai/charts/skyportalai-agent` |
| Container image | `ghcr.io/skyportalai/skyportalai-agent:<agent version>` |

The chart's `appVersion` is the agent release it was written against and the
image tag it pulls by default, so installing it involves no image build and no
tag to pick.

## What you need first

- A Kubernetes cluster and `kubectl` pointed at it.
- `helm` 3.8 or newer for the chart (OCI registry support is on by default from
  3.8). The plain manifests need only `kubectl`.
- An agent token from your SkyPortal instance (step 1).
- GHCR read access for the private chart and image. Log Helm in before
  installation and configure a Kubernetes registry pull secret (see
  [Registry access](#registry-access)).

## 1. Mint an agent token

Each agent authenticates with its own token. Create one on the `/agents/` page
of your SkyPortal instance (name the agent, name the cluster, optional expiry).
The token looks like `agt_...` and is shown in full exactly once, at creation
time, so copy it then. The Django admin works too.

## 2. Create the token Secret

The chart and the manifests both read the token from an existing Secret rather
than taking it as a plain value, so it never lands in your Helm release or a
committed file. Create it imperatively, in the namespace you will install into:

```bash
kubectl create secret generic skyportalai-agent-token \
  --from-literal=SKYPORTALAI_AGENT_TOKEN=agt_xxxxxxxxxxxxxxxxxxxxxxxx
```

The key must be `SKYPORTALAI_AGENT_TOKEN`. With Helm you can point at a different
key name via the `token.secretKey` value. The plain manifests have no such
value, so to use a different key name there you edit the `key:` under
`secretKeyRef` in `manifests/deployment.yaml` to match. If you prefer a
manifest for the Secret itself, edit and apply `manifests/secret.example.yaml`,
but keep it out of git.

## 3. Install

### Option A: Helm

Complete [Registry access](#registry-access) first; the chart and image are
private packages.

```bash
helm install skyportalai-agent oci://ghcr.io/skyportalai/charts/skyportalai-agent \
  --version 0.2.1 \
  --set token.existingSecret=skyportalai-agent-token \
  --set 'imagePullSecrets[0].name=ghcr-pull' \
  --set config.baseUrl=https://skyportal.example.com
```

`helm show chart oci://ghcr.io/skyportalai/charts/skyportalai-agent` prints the
newest chart version and the agent release (`appVersion`) it installs;
`helm show values` on the same reference prints every default with its
explanation. Pin `--version` so an upgrade is a decision rather than a side
effect of reinstalling.

`token.existingSecret` is required: `helm install` stops with a validation error
before anything reaches the cluster if it is unset. Leave `config.baseUrl` off
to default to `https://app.skyportal.ai`. Every value is checked against the
chart's `values.schema.json`, so a misspelled key or a value of the wrong type
is rejected up front rather than rendered into a broken Deployment.

The chart installs a single replica Deployment (the spool queue is a single
writer, so it is hardcoded to one replica with a Recreate rollout), a
PersistentVolumeClaim for the spool and catalog, a ServiceAccount, and a
NetworkPolicy that denies inbound traffic and allows only DNS and outbound
HTTPS.

The container runs with a read only root filesystem, so the chart also mounts a
small emptyDir at `/tmp`. That one is not optional: the agent imports the W&B
client while loading its scanners, and importing it creates a temp directory, so
with nowhere to write the pod crash loops before it has read any config. Turning
the scanner off with `config.enableWandb=false` does not avoid it, because the
import happens first.

Common overrides:

| Override | Default | Purpose |
|---|---|---|
| `config.baseUrl` | agent default | your SkyPortal API root |
| `config.intervalSeconds` | `60` | seconds between scan and ship cycles |
| `config.clusterName` | none | label shipped runs by cluster |
| `config.mlflowMode` | `filesystem` | `rest` to read an MLflow tracking server instead of `mlruns/` |
| `config.mlflowTrackingUri` | none | the tracking server for `rest` mode |
| `image.tag` | chart appVersion | run a different agent release than the chart was written for |
| `persistence.size` | `1Gi` | spool volume size |
| `persistence.storageClass` | cluster default | pin a StorageClass |
| `tmpDir.sizeLimit` | `64Mi` | cap on the writable `/tmp` emptyDir |
| `networkPolicy.enabled` | `true` | set false if you have no policy controller |
| `extraEnv` | `[]` | env vars the chart has no value for (see below) |

Keep your overrides in a values file and pass it to both install and upgrade,
so moving to a newer chart carries them along:

```bash
helm upgrade skyportalai-agent oci://ghcr.io/skyportalai/charts/skyportalai-agent \
  --version <new chart version> -f my-values.yaml
```

### Option B: plain manifests

`manifests/` holds the static equivalent of the chart's default render, pinned to
the same agent release as the chart's `appVersion`. Download the directory (it is
not packaged anywhere), edit the `# EDIT` placeholders (image tag,
`SKYPORTALAI_BASE_URL`, sizes), then apply:

```bash
kubectl apply -f manifests/pvc.yaml
kubectl apply -f manifests/deployment.yaml
kubectl apply -f manifests/networkpolicy.yaml
```

#### Upgrading from the pre-rename manifests

These objects were named `skyportal-agent` before chart 0.2.0. A
PersistentVolumeClaim is identified by its name, so applying the renamed
manifests over an older install does not reuse the old spool volume: you get a
second, empty claim, and the old Deployment keeps running beside the new one,
with two agents scanning the same directories. Neither the image nor the chart
was published before the rename, so an older install runs an image someone
built by hand and should be rare.

Delete the old Deployment first, so its pod stops shipping and releases the
volume. The spool is a single writer, which is why there is one replica and a
Recreate rollout:

```bash
kubectl delete deployment skyportal-agent
```

Then decide whether to keep the spool. To keep it, edit `claimName` in
`manifests/deployment.yaml` back to `skyportal-agent` and do not apply
`pvc.yaml`; apply the other two files as above. To start clean, apply all three
unchanged: batches that were spooled but never shipped are lost and the run
catalog starts empty, so the agent ships runs it already sent a second time.
The server keys stored runs by run id per agent and skips ids it already has, so
that costs a little bandwidth and nothing else.

Remove what the old names left behind once the new pod is running:

```bash
kubectl delete networkpolicy skyportal-agent
kubectl delete pvc skyportal-agent  # only if you started clean
```

The token Secret is renamed too, to `skyportalai-agent-token` with the key
`SKYPORTALAI_AGENT_TOKEN`. Create it as in step 2 and delete the old one, or
keep the old Secret and point `secretKeyRef` in `manifests/deployment.yaml` at
its name and key.

No chart was published before the rename either, so there is no Helm upgrade
path from a released version. If you installed a release from a path in this
repository under the old chart name, `helm uninstall <your release name>`
before installing the new chart. That deletes its PersistentVolumeClaim with
the rest of the release, so it is the clean start described above.

## 4. Verify it is running

Watch the logs. On a healthy start you see the `skyportalai-agent started` line
with the base URL, interval, and state dir:

```bash
kubectl logs -l app.kubernetes.io/instance=skyportalai-agent -f
```

Check the liveness endpoint from inside the pod (nothing fronts it with a
Service, so there is no external address):

```bash
kubectl exec deploy/skyportalai-agent -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
# {"status": "ok"}
```

After the first successful ship, the agent's row on your instance's `/agents/`
page shows its version and last contact time.

If the pod crashes at startup with a token error, the Secret is missing or the
key name does not match `token.secretKey`. If it crash loops on
`No usable temporary directory found`, it has no writable `/tmp`: the chart
mounts one by default, so this means it was removed or a custom manifest omits
it. `ImagePullBackOff` with `unauthorized` or `denied` means your cluster cannot
read the package; see the next section.

## Kubernetes monitoring

Set `kubernetes.enabled=true` and the chart adds two workloads from the same image:

| Workload | Runs | Reads | Access |
|---|---|---|---|
| `<release>-cluster` Deployment | 1 pod | pods, events, nodes, namespaces, deployments/statefulsets, crash logs of failing pods, `kubectl top` | a read-only ClusterRole: `get`/`list`/`watch`, no secrets, no writes |
| `<release>-node` DaemonSet | 1 pod on every node | the node's CPU, memory, disk, load (host `/proc`, read only) and GPUs (NVML) | no Kubernetes API token at all |

GPU utilisation comes from the node agent (NVML), so no DCGM exporter is needed.
vLLM serving metrics are not collected through the agent yet.

SkyPortal sets how often they collect (every 30 seconds). If SkyPortal is
unreachable, uploads are buffered on disk and delivered in order when it returns.
The cluster pod also runs read-only `kubectl` that you ask for in SkyPortal chat.
The agent refuses any other command itself, whatever the server sends.

### Before you install

```bash
# The port your API server listens on (often 6443). Add it to kubernetes.apiServerPorts.
kubectl get endpoints kubernetes -n default
# GPU clusters: the RuntimeClass that exposes the NVIDIA driver, usually "nvidia".
kubectl get runtimeclass
```

- **Egress:** outbound TCP 443 to your SkyPortal host (`app.skyportal.ai`), plus
  DNS. If you allowlist egress, allowlist the **hostname**: its IP addresses are
  shared and change.
- **Pod Security:** the node DaemonSet mounts the host's `/proc` read only and
  keeps its buffer in a host directory. The "baseline" and "restricted" Pod
  Security Standards refuse host mounts, so label the namespace:

  ```bash
  kubectl create namespace skyportal
  kubectl label namespace skyportal pod-security.kubernetes.io/enforce=privileged
  ```

  The cluster Deployment needs no host access.

### Install

Create the agent on the **Agents** page, using the cluster's name, and create the
token Secret as in step 2 (in the `skyportal` namespace). Then:

```yaml
# skyportal-values.yaml
token:
  existingSecret: skyportalai-agent-token
kubernetes:
  enabled: true
  apiServerPorts: [443, 6443]   # include the port found above
  node:
    gpu:
      runtimeClassName: nvidia   # "" on a cluster without GPUs
config:
  clusterName: my-cluster
  # No W&B or MLflow in this cluster? Turn the experiment scanners off.
  enableWandb: false
  enableMlflow: false
```

```bash
helm install skyportalai-agent oci://ghcr.io/skyportalai/charts/skyportalai-agent \
  --version <chart version> -n skyportal -f skyportal-values.yaml
```

The chart refuses `kubernetes.enabled` on an agent image older than 0.3.0: an
older agent would run and send nothing.

### Verify

```bash
kubectl -n skyportal get pods -o wide   # one -node- pod per node, one -cluster- pod
kubectl -n skyportal logs deploy/skyportalai-agent-cluster | head   # "role=cluster"
```

Within a minute the cluster shows as Connected in SkyPortal, with its pods and
per-node metrics.

The node DaemonSet tolerates every taint so that it reaches every node, GPU and
control-plane nodes included. To monitor a subset, set
`kubernetes.node.tolerations` or `kubernetes.node.nodeSelector`.

## Registry access

The chart and image are private packages in GitHub Container Registry. Use a
GitHub account with access to both packages and a token with the
`read:packages` scope. Log Helm in to pull the chart (enter the token at the
password prompt):

```bash
helm registry login ghcr.io --username <github user>
```

Helm's login only covers the chart download; Kubernetes needs its own pull
secret to download the image. Create it in the agent's namespace:

```bash
kubectl create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io \
  --docker-username=<github user> \
  --docker-password=<token with read:packages>
```

Helm: `--set imagePullSecrets[0].name=ghcr-pull`. Manifests: uncomment
`imagePullSecrets` in `manifests/deployment.yaml`.

To serve the image from your own registry instead (air gapped clusters, or a
registry your nodes already trust), mirror it and point the chart at the copy:

```bash
docker pull ghcr.io/skyportalai/skyportalai-agent:0.2.2
docker tag ghcr.io/skyportalai/skyportalai-agent:0.2.2 registry.example.com/skyportalai-agent:0.2.2
docker push registry.example.com/skyportalai-agent:0.2.2
helm install ... --set image.repository=registry.example.com/skyportalai-agent
```

Building the image from source is a maintainer task; see
[RELEASING.md](RELEASING.md).

## Mounting experiment data

The agent scans paths on its own filesystem, so the W&B and MLflow directories
have to be mounted into the pod. Mount them read only and point the agent at the
mount paths.

With Helm, use `extraVolumes` / `extraVolumeMounts` plus `config.wandbDir` /
`config.mlflowDir`:

```yaml
config:
  wandbDir: /experiments/wandb
  mlflowDir: /experiments/mlruns
extraVolumes:
  - name: experiments
    persistentVolumeClaim:
      claimName: ml-experiments
extraVolumeMounts:
  - name: experiments
    mountPath: /experiments
    readOnly: true
```

With the plain manifests, uncomment the matching `experiments` volume, volume
mount, and `SKYPORTALAI_AGENT_WANDB_DIR` / `SKYPORTALAI_AGENT_MLFLOW_DIR` env
blocks in `manifests/deployment.yaml`.

If your MLflow runs live in a tracking server rather than an `mlruns/`
directory, skip the mount and query the server instead:

```yaml
config:
  mlflowMode: rest
  mlflowTrackingUri: https://mlflow.example.com
```

The default NetworkPolicy allows outbound TCP 443 only. A tracking server on
another port (a plain `http://mlflow:5000` inside the cluster, say) needs an
extra egress rule, or `networkPolicy.enabled=false`.

## Configuration reference

Every setting maps onto an environment variable the agent reads at startup
([agent documentation](https://github.com/SkyportalAi/skyportalai/blob/main/docs/agent.md)).

| Env var | Helm value | Default | Notes |
|---|---|---|---|
| `SKYPORTALAI_AGENT_TOKEN` | `token.existingSecret` (Secret) | required | agent exits at startup if unset |
| `SKYPORTALAI_BASE_URL` | `config.baseUrl` | `https://app.skyportal.ai` | SkyPortal API root |
| `SKYPORTALAI_AGENT_INTERVAL_SECONDS` | `config.intervalSeconds` | `60` | scan and ship cadence |
| `SKYPORTALAI_AGENT_ENABLE_WANDB` | `config.enableWandb` | `true` | toggle the W&B scanner |
| `SKYPORTALAI_AGENT_ENABLE_MLFLOW` | `config.enableMlflow` | `true` | toggle the MLflow scanner |
| `SKYPORTALAI_AGENT_MLFLOW_MODE` | `config.mlflowMode` | `filesystem` | `filesystem` scans `mlruns/`, `rest` queries a tracking server |
| `SKYPORTALAI_AGENT_MLFLOW_TRACKING_URI` | `config.mlflowTrackingUri` | none | tracking server URL for `rest` mode |
| `SKYPORTALAI_AGENT_WANDB_DIR` | `config.wandbDir` | agent default roots | where W&B runs live |
| `SKYPORTALAI_AGENT_MLFLOW_DIR` | `config.mlflowDir` | agent default roots | where `mlruns/` lives |
| `SKYPORTALAI_AGENT_CLUSTER_NAME` | `config.clusterName` | none | label shipped runs |
| `SKYPORTALAI_AGENT_STATE_DIR` | `config.stateDir` | `/var/lib/skyportal-agent` | spool and catalog location |
| `SKYPORTALAI_AGENT_QUEUE_MAX_BATCHES` | `config.queueMaxBatches` | `1000` | cap on spooled batches while the API is unreachable |
| `SKYPORTALAI_AGENT_HEALTHZ_PORT` | `config.healthzPort` | `8080` | liveness port |

The state directory keeps its pre-rename path on purpose: it is the agent's own
default, and changing it on an existing install would orphan the spool queue.

Anything else the agent reads from the environment goes in `extraEnv`, as plain
Kubernetes env entries. The one you are most likely to need: the agent refuses a
`config.baseUrl` on plain `http://` unless the host is loopback, because the
token would travel in cleartext. For an internal instance you trust anyway:

```yaml
config:
  baseUrl: http://skyportal.internal:8000
extraEnv:
  - name: SKYPORTALAI_ALLOW_INSECURE
    value: "1"
```

The NetworkPolicy allows outbound TCP 443 only, so a base URL on another port
also needs an extra egress rule or `networkPolicy.enabled=false`.
