# Releasing the observability agent image and chart

Maintainer notes for `deploy/agent/`. Operators installing the agent want
[README.md](README.md) instead.

Two artifacts come out of this directory, published by two workflows:

| Artifact | Published to | Workflow | Trigger |
|---|---|---|---|
| Image | `ghcr.io/skyportalai/skyportalai-agent:<X.Y.Z>` (and `:latest`) | `.github/workflows/agent-image.yml` | git tag `agent-v<X.Y.Z>` |
| Chart | `oci://ghcr.io/skyportalai/charts/skyportalai-agent:<chart version>` | `.github/workflows/agent-chart.yml` | merge to `main` with a new `version` in `Chart.yaml` |

Neither needs a secret beyond the workflow's own `GITHUB_TOKEN`: the image
build installs its pinned release of
[`skyportalai`](https://pypi.org/project/skyportalai/) from PyPI, which needs
no credential. The workflows publish independently of the SDK's PyPI release
workflow, but the image can only be built once the SDK release it pins is on
PyPI (see [docs/RELEASING.md](../../docs/RELEASING.md)).
PR and main image checks and chart validation do not log in to GHCR; only the
separate publish jobs receive `packages: write`. Image publishing accepts an
`agent-v*` tag pointing into `main` history, or a manual run on `main` with
`push=true`. A manual run on another branch cannot publish.
The image jobs share `.github/actions/build-agent`; publishing rebuilds and
smoke tests the image on its own runner before pushing that same image.
Images, Docker build caches, and build records are not uploaded as public
Actions artifacts or caches.

## Migration from skyportal-website

The package names and existing releases stay unchanged. Chart `0.2.1` updates
the source links to this repository; chart `0.2.0` must not be overwritten.
The default agent image remains `0.2.2`, installing `skyportalai==0.2.2`
from PyPI, rather than whatever source happens to be on `main`.

Use this order for the handover:

1. Freeze agent releases in `SkyportalAi/skyportal-website` during handover.
   Keep its retirement PR unmerged until the SDK publisher is validated, so
   the old workflows remain available for rollback.
2. A package admin must open **Package settings → Manage Actions access** for
   **both existing packages** and add `SkyportalAi/skyportalai` with **Write**:
   - [Agent image](https://github.com/SkyportalAi/skyportal-website/pkgs/container/skyportalai-agent)
   - [Agent chart](https://github.com/SkyportalAi/skyportal-website/pkgs/container/charts%2Fskyportalai-agent)
   These are the existing package links before an admin changes their linked
   repository.
   Repository access alone is not evidence of this grant. At the time of this
   migration, authenticated package metadata requests returned HTTP 403, so
   the grant could not be verified and must be confirmed by a package admin.
3. Keep both packages **private**, with their current explicit access rules.
   Do not enable automatic inheritance of access from this public repository
   or change package visibility as part of the migration.
4. Review package source/repository links separately from Actions access.
   Updating the chart metadata and image source labels does not replace the
   Actions write grant. If an admin reconnects a package to this repository,
   leave inherited repository access disabled and verify it remains private.
5. After the admin confirms private visibility and Actions write access,
   merge the SDK migration and validate its new publisher. Verify published
   references through an account that already has package read access. A
   successful PR build does not prove that the SDK repository can publish or
   that an operator can pull packages.
6. Once SDK publishing is validated, merge the website retirement PR. Only
   the SDK workflows should publish agent releases after this cutover.

The migration does not republish image `0.2.2`. Its old source label remains
until a separately requested rebuild; the chart can use the existing image.

## Versioning

- **The image tag is the agent release.** The image installs the `skyportalai`
  release `<X.Y.Z>` from PyPI and is tagged `<X.Y.Z>`; there is no separate
  image version. `latest` moves to the newest release.
- **Three places pin the agent release and must agree:** `appVersion` in
  `helm/skyportalai-agent/Chart.yaml`, `ARG SKYPORTALAI_VERSION` in `Dockerfile`, and
  the image tag in `manifests/deployment.yaml`. The chart workflow fails when
  they differ. Change all three in one PR, and bump the chart `version` with
  them (a new default image is a chart change).
- **The chart `version` bumps on any change under `helm/skyportalai-agent/`.**
  The chart workflow refuses a PR that changes chart files without changing
  `version`, and the publish job only pushes a version the registry does not
  have yet, so a forgotten bump is caught in review instead of silently
  republishing. Patch for fixes that change no values or defaults, minor for
  new values or a new `appVersion`, major for anything an existing install has
  to change its values for.
- `SKYPORTALAI_VERSION` is an exact PyPI version, never a range. The build has
  no lockfile, so a range would make every rebuild resolve to a different
  image. PyPI releases are immutable, so a pinned version can always be rebuilt,
  which the git commit SHA pinned before 0.2.2 could not guarantee.
- **Chart `0.2.0` renamed the Kubernetes objects** from `skyportal-agent` to
  `skyportalai-agent`, and a PersistentVolumeClaim is identified by its name, so
  applying it over an install made before it orphans that install's spool volume.
  Point anyone in that position at
  [Upgrading from the pre-rename manifests](README.md#upgrading-from-the-pre-rename-manifests).

## Cutting an agent release

1. Confirm the SDK release is on PyPI:
   `https://pypi.org/project/skyportalai/<X.Y.Z>/`. The GitHub Release alone is
   not enough: the PyPI publish job waits for a reviewer, and the image build
   fails until the version is installable. If the release changed the console
   script, `agent` extra, or environment variables, read the SDK changelog
   first; the entrypoint and the env var names in the chart follow the SDK.
2. Open a PR that sets `ARG SKYPORTALAI_VERSION=<X.Y.Z>`, `appVersion: "<X.Y.Z>"`,
   the manifests' image tag, and bumps the chart `version`. Update the
   configuration table in `README.md` if the SDK gained or renamed settings. CI
   builds the image with the new release (without pushing), smoke tests it, and
   installs the chart with it on a kind cluster.
3. Merge. The chart workflow publishes the new chart version to GHCR.
4. Tag the merge commit and push the tag; the image workflow installs that
   release from PyPI, runs the same smoke tests, and publishes `<X.Y.Z>` and
   `latest`:

   ```bash
   git fetch origin main
   git tag agent-v<X.Y.Z> origin/main
   git push origin agent-v<X.Y.Z>
   ```

   Until this job finishes (a few minutes), the just published chart points at
   an image tag that does not exist yet, so tag right after merging.
5. Log in to GHCR with an account that has package read access, then verify
   the published references (see the operator README for Helm login and the
   Kubernetes image pull secret):

   ```bash
   helm show chart oci://ghcr.io/skyportalai/charts/skyportalai-agent --version <chart version>
   docker pull ghcr.io/skyportalai/skyportalai-agent:<X.Y.Z>
   ```

## Publishing a chart-only change

Bump `version` in `Chart.yaml`, merge. The publish job packages the chart,
checks the registry, and pushes.

## Rebuilding an existing release

Run the **Agent image** workflow on `main` by hand (`workflow_dispatch`) with `version`
set to the release and `push` ticked. This is for base image updates (the
runtime is `python:3.11-slim`) and overwrites the `<X.Y.Z>` tag with a new
digest; anyone who needs immutability should pin the digest, not the tag.

## Package visibility

Both existing GHCR packages remain private. Operators need an account with
package access and a token with `read:packages`, as described in the README.
Package visibility, repository linking, inherited access, and Actions write
access are separate settings; this migration only moves build ownership.
Do not make either package public or enable inherited access without a
separate decision from the package owner.

## Clean install checklist

CI installs every PR's chart on a fresh kind cluster with the image built from
that PR, so chart mechanics are covered. This checklist is for the part CI
cannot do: the published references, on a real cluster, run by someone who did
not write the chart. Do it once per release you care about and record the
result on the tracking issue (chart version, agent version, Kubernetes
distribution and version, anything that surprised you).

Start from a cluster with no SkyPortal state and no clone of this repository.

1. `helm show chart oci://ghcr.io/skyportalai/charts/skyportalai-agent` prints
   the expected `version` and `appVersion`.
2. Mint a token on your instance's `/agents/` page and create the Secret
   (README step 2).
3. `helm install` from the OCI reference with `--version`, the Secret name and
   your `config.baseUrl`. Expected: the command returns, and `helm status`
   shows the release deployed.
4. `kubectl rollout status deploy/skyportalai-agent` completes. The pod pulls
   the private image using its configured pull secret and
   its logs show `skyportalai-agent started`.
5. The `/healthz` exec from README step 4 returns `{"status": "ok"}`, and after
   two minutes the pod still shows `0` restarts.
6. Your instance's `/agents/` page shows the agent with a last contact time
   and the version you installed.
7. `helm uninstall skyportalai-agent`, then
   `kubectl get deploy,pod,pvc,sa,networkpolicy -l app.kubernetes.io/instance=skyportalai-agent`
   returns nothing. Delete the Secret yourself; the chart never owned it.

Anything that needed a step not written in the README is a documentation bug.
