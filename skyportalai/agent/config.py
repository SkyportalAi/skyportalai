"""AgentConfig — typed, env-first configuration for the observability agent.

Mirrors the SDK client's env-with-fallback style (see ``Skyportal.__init__``):
values come from the environment — which is also how a mounted ConfigMap
surfaces — with sensible defaults. The agent token is the only required field.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .._client import DEFAULT_BASE_URL
from .._exceptions import SkyportalError

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_HEALTHZ_PORT = 8080
DEFAULT_QUEUE_MAX_BATCHES = 1000
DEFAULT_STATE_DIR = Path("/var/lib/skyportal-agent")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


def _parse_int(
    value: str | None,
    default: int,
    *,
    name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r (not an integer); using default %d", name, value, default)
        return default
    if minimum is not None and parsed < minimum:
        logger.warning("Ignoring %s=%d (< minimum %d); using default %d", name, parsed, minimum, default)
        return default
    if maximum is not None and parsed > maximum:
        logger.warning("Ignoring %s=%d (> maximum %d); using default %d", name, parsed, maximum, default)
        return default
    return parsed


def _parse_path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


@dataclass(frozen=True)
class AgentConfig:
    """Typed configuration for the SkyPortal observability agent."""

    token: str
    base_url: str = DEFAULT_BASE_URL
    wandb_dir: Path | None = None
    mlflow_dir: Path | None = None
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    enable_wandb: bool = True
    enable_mlflow: bool = True
    mlflow_mode: str = "filesystem"
    mlflow_tracking_uri: str | None = None
    cluster_name: str | None = None
    state_dir: Path = DEFAULT_STATE_DIR
    healthz_port: int = DEFAULT_HEALTHZ_PORT
    queue_max_batches: int = DEFAULT_QUEUE_MAX_BATCHES

    @property
    def spool_dir(self) -> Path:
        """Disk-backed queue location (under the state dir)."""
        return self.state_dir / "spool"

    @property
    def catalog_path(self) -> Path:
        """Run-diffing catalog (existing_experiments.json) under the state dir."""
        return self.state_dir / "existing_experiments.json"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AgentConfig":
        """Build config from the environment (or an explicit mapping).

        The agent token is required; everything else falls back to a default.
        """
        env = os.environ if environ is None else environ

        token = (env.get("SKYPORTAL_AGENT_TOKEN") or "").strip()
        if not token:
            raise SkyportalError(
                "No agent token provided. Set the SKYPORTAL_AGENT_TOKEN "
                "environment variable (sourced from the Kubernetes Secret)."
            )

        base_url = (env.get("SKYPORTAL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")

        # MLflow source mode: "filesystem" (scan mlruns/) or "rest" (tracking
        # server API). Unknown values fall back to filesystem.
        mlflow_mode = (env.get("SKYPORTAL_AGENT_MLFLOW_MODE") or "filesystem").strip().lower()
        if mlflow_mode not in ("filesystem", "rest"):
            mlflow_mode = "filesystem"

        enable_mlflow = _parse_bool(env.get("SKYPORTAL_AGENT_ENABLE_MLFLOW"), True)
        mlflow_tracking_uri = env.get("SKYPORTAL_AGENT_MLFLOW_TRACKING_URI") or None
        if enable_mlflow and mlflow_mode == "rest" and not mlflow_tracking_uri:
            logger.warning(
                "SKYPORTAL_AGENT_MLFLOW_MODE=rest but SKYPORTAL_AGENT_MLFLOW_TRACKING_URI "
                "is unset; the MLflow REST scanner will be unavailable and MLflow "
                "ingest will be skipped."
            )

        return cls(
            token=token,
            base_url=base_url,
            wandb_dir=_parse_path(env.get("SKYPORTAL_AGENT_WANDB_DIR")),
            mlflow_dir=_parse_path(env.get("SKYPORTAL_AGENT_MLFLOW_DIR")),
            interval_seconds=_parse_int(
                env.get("SKYPORTAL_AGENT_INTERVAL_SECONDS"),
                DEFAULT_INTERVAL_SECONDS,
                name="SKYPORTAL_AGENT_INTERVAL_SECONDS",
                minimum=1,
            ),
            enable_wandb=_parse_bool(env.get("SKYPORTAL_AGENT_ENABLE_WANDB"), True),
            enable_mlflow=enable_mlflow,
            mlflow_mode=mlflow_mode,
            mlflow_tracking_uri=mlflow_tracking_uri,
            cluster_name=env.get("SKYPORTAL_AGENT_CLUSTER_NAME") or None,
            state_dir=_parse_path(env.get("SKYPORTAL_AGENT_STATE_DIR")) or DEFAULT_STATE_DIR,
            healthz_port=_parse_int(
                env.get("SKYPORTAL_AGENT_HEALTHZ_PORT"),
                DEFAULT_HEALTHZ_PORT,
                name="SKYPORTAL_AGENT_HEALTHZ_PORT",
                minimum=1,
                maximum=65535,
            ),
            queue_max_batches=_parse_int(
                env.get("SKYPORTAL_AGENT_QUEUE_MAX_BATCHES"),
                DEFAULT_QUEUE_MAX_BATCHES,
                name="SKYPORTAL_AGENT_QUEUE_MAX_BATCHES",
                minimum=1,
            ),
        )
