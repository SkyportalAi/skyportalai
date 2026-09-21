# SkyPortal observability agent container image.
#
# The agent is the `agent` extra of the skyportalai package on PyPI
# (https://pypi.org/project/skyportalai/, MIT). Its deployment assets and build
# workflows live alongside the SDK. The builder installs a pinned release of the
# package into a self contained virtualenv, and the runtime stage copies only
# that venv onto a fresh slim base. Nothing from the builder (pip caches)
# reaches the final image, and the process runs as an unprivileged user.
#
# CI builds and publishes this image (.github/workflows/agent-image.yml): pushing
# an `agent-v<X.Y.Z>` tag installs skyportalai X.Y.Z from PyPI and publishes
# ghcr.io/skyportalai/skyportalai-agent:<X.Y.Z>. See deploy/agent/RELEASING.md.
#
# Local build, from the skyportalai repo root. No credentials are needed:
#   docker build -t skyportalai-agent:dev deploy/agent

# ---- builder ----------------------------------------------------------------
FROM python:3.11-slim AS builder

# Release of skyportalai on PyPI to build the image from. Pin a new version
# here when releasing a new agent version (CI passes the same value from the
# `agent-v<X.Y.Z>` git tag). Always an exact version: pip has no lockfile here,
# so a range would make every rebuild resolve to a different image. Must match
# appVersion in the chart and the image tag in manifests/deployment.yaml; CI
# checks the three agree.
ARG SKYPORTALAI_VERSION=0.2.2

ENV VENV=/opt/venv
RUN python -m venv "$VENV"
ENV PATH="$VENV/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip

# The `agent` extra adds the daemon only dependency (wandb) on top of the SDK;
# pyyaml is a base dependency of the package.
RUN pip install --no-cache-dir "skyportalai[agent]==${SKYPORTALAI_VERSION}"

# ---- runtime ----------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Source labels describe the build's repository. Existing GHCR package access
# must be configured separately; do not enable inherited access during cutover.
# Version and revision are added by CI.
LABEL org.opencontainers.image.title="skyportalai-agent" \
      org.opencontainers.image.description="SkyPortal observability agent: ships Weights & Biases and MLflow run metadata to SkyPortal" \
      org.opencontainers.image.source="https://github.com/SkyportalAi/skyportalai" \
      org.opencontainers.image.url="https://github.com/SkyportalAi/skyportalai/tree/main/deploy/agent" \
      org.opencontainers.image.documentation="https://github.com/SkyportalAi/skyportalai/blob/main/deploy/agent/README.md"

ENV VENV=/opt/venv
ENV PATH="$VENV/bin:$PATH" \
    HOME=/home/skyportal \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non root user (uid 1000) and the state dir it owns. The agent writes its spool
# queue and run catalog under the state dir; in Kubernetes that path is a
# mounted volume, but owning it here keeps a plain `docker run` working too.
# The directory keeps the pre rename name on purpose: it is the agent's own
# default and renaming it on an existing install would orphan the spool.
RUN groupadd --gid 1000 skyportal \
    && useradd --uid 1000 --gid 1000 --home-dir /home/skyportal --create-home --shell /usr/sbin/nologin skyportal \
    && mkdir -p /var/lib/skyportal-agent \
    && chown -R skyportal:skyportal /var/lib/skyportal-agent

COPY --from=builder /opt/venv /opt/venv

USER skyportal
WORKDIR /home/skyportal

# Liveness endpoint (see SKYPORTALAI_AGENT_HEALTHZ_PORT, default 8080).
EXPOSE 8080

# The console script declared in the SDK's [project.scripts]. It was named
# `skyportal-agent` before 0.2.0; the CLI rename to the `skyportalai` surface
# made `skyportalai-agent` the installed name, so anything older here fails at
# container start with "executable file not found in $PATH". Exits at startup
# unless SKYPORTALAI_AGENT_TOKEN is set.
ENTRYPOINT ["skyportalai-agent"]
