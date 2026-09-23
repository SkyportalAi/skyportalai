# SkyPortal observability agent container image.
#
# The agent is the `agent` extra of the SkyportalAi/skyportalai package (public,
# MIT). Its deployment assets and build workflows live alongside the SDK.
# The builder installs a pinned release of the package into a self
# contained virtualenv, and the runtime stage copies only that venv onto a fresh
# slim base. Nothing from the builder (pip caches, git) reaches the final image,
# and the process runs as an unprivileged user.
#
# CI builds and publishes this image (.github/workflows/agent-image.yml): pushing
# an `agent-v<X.Y.Z>` tag builds from the skyportalai release tag v<X.Y.Z> and
# publishes ghcr.io/skyportalai/skyportalai-agent:<X.Y.Z>. See deploy/agent/RELEASING.md.
#
# Local build (BuildKit), from the skyportalai repo root:
#   docker build -t skyportalai-agent:dev deploy/agent
# A GitHub token can be passed as a BuildKit secret; it is only needed if the
# source repository is ever private again or to dodge anonymous rate limits:
#   GITHUB_TOKEN=$(gh auth token) docker build \
#     --secret id=github_token,env=GITHUB_TOKEN -t skyportalai-agent:dev deploy/agent

# ---- builder ----------------------------------------------------------------
FROM python:3.11-slim AS builder

# Release tag of SkyportalAi/skyportalai to build the image from. Pin a new tag
# here when releasing a new agent version (CI passes the same value from the
# git tag). Prefer a tag over a bare commit SHA: a tag is a deliberate release
# marker that survives rebases and force pushes, so a wrong value here fails
# when we cut the release rather than months later when someone rebuilds and
# the commit is gone. Must match appVersion in the chart and the image tag in
# manifests/deployment.yaml; CI checks the three agree.
ARG SKYPORTALAI_REF=v0.2.2

ENV VENV=/opt/venv
RUN python -m venv "$VENV"
ENV PATH="$VENV/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

# `.[agent]` pulls in the daemon only deps (pyyaml + wandb) on top of the SDK.
# The optional token is a BuildKit secret: it is mounted for this RUN only and
# never written to an image layer. Without one the clone is anonymous, which is
# all a public repository needs.
RUN --mount=type=secret,id=github_token,required=false \
    set -e; \
    if [ -s /run/secrets/github_token ]; then \
        AUTH="x-access-token:$(cat /run/secrets/github_token)@"; \
    else \
        AUTH=""; \
    fi; \
    pip install --no-cache-dir \
        "skyportalai[agent] @ git+https://${AUTH}github.com/SkyportalAi/skyportalai@${SKYPORTALAI_REF}"

# kubectl for the Kubernetes roles (#3566). Pinned to the version skyportal-website's
# workers run (KUBECTL_VERSION in its development/Dockerfile): the server parses
# kubectl's table output, so the agent's kubectl and the server's must agree for
# an agent-fed cluster to record the same rows as a kubeconfig one. Checked against
# the published sha256 before it is used.
ARG KUBECTL_VERSION=v1.35.6
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && echo "$(curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256")  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/kubectl

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
COPY --from=builder /usr/local/bin/kubectl /usr/local/bin/kubectl

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
