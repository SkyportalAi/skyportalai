"""Agent entrypoint — ``python -m skyportalai.agent`` / ``skyportalai-agent``.

Thin orchestration: assemble the runner from config, start the /healthz probe
server on a background thread, route SIGTERM/SIGINT to a graceful stop (which
triggers the final queue flush), and run the loop on the main thread.
"""

from __future__ import annotations

import logging
import signal
import threading

from .. import _env
from .._client import Skyportal
from .config import ROLE_CLUSTER, ROLE_EXPERIMENTS, ROLE_NODE, AgentConfig
from .health import HealthServer
from .kubernetes import ClusterRole, CommandPoller, KubernetesRunner, KubernetesShipper, NodeRole
from .queue import SpoolQueue
from .runner import AgentRunner
from .scrapers import MlflowRestScanner, MlflowScanner, WandbScanner
from .shipper import Shipper

logger = logging.getLogger(__name__)


def build_scanners(config: AgentConfig) -> list:
    """Instantiate the enabled scanners; availability is checked at scan time."""
    scanners: list = []
    if config.enable_wandb:
        scanners.append(WandbScanner())
    if config.enable_mlflow:
        # Exactly one mlflow scanner — never both. Both share source='mlflow',
        # so running the pair would double-ingest / collide in the catalog.
        if config.mlflow_mode == "rest":
            scanners.append(MlflowRestScanner(config.mlflow_tracking_uri))
        else:
            scanners.append(MlflowScanner())
    return scanners


def build_runner(
    config: AgentConfig,
    *,
    client: Skyportal | None = None,
    stop_event: threading.Event | None = None,
) -> AgentRunner:
    """Assemble the full scan -> queue -> ship pipeline from config."""
    client = client or Skyportal(api_key=config.token, base_url=config.base_url)
    return AgentRunner(
        scanners=build_scanners(config),
        catalog_path=config.catalog_path,
        queue=SpoolQueue(config.spool_dir, max_batches=config.queue_max_batches),
        shipper=Shipper.from_client(client),
        interval_seconds=config.interval_seconds,
        roots={"wandb": config.wandb_dir, "mlflow": config.mlflow_dir},
        stop_event=stop_event,
    )


def build_kubernetes_runner(
    config: AgentConfig,
    *,
    stop_event: threading.Event | None = None,
) -> KubernetesRunner:
    """Assemble the collect -> queue -> ship loop for the cluster or node role."""
    if config.role == ROLE_NODE:
        role = NodeRole(config.node_name, host_proc=config.host_proc, disk_path=config.state_dir)
    else:
        role = ClusterRole()
    return KubernetesRunner(
        role=role,
        queue=SpoolQueue(config.spool_dir, max_batches=config.queue_max_batches, max_bytes=config.queue_max_bytes),
        shipper=KubernetesShipper(config.base_url, config.token),
        interval_seconds=config.interval_seconds,
        stop_event=stop_event,
    )


def _start_command_poller(config: AgentConfig, stop_event: threading.Event) -> threading.Thread:
    poller = CommandPoller(config.base_url, config.token, stop_event=stop_event)
    thread = threading.Thread(target=poller.run_forever, name="kube-command-poller", daemon=True)
    thread.start()
    return thread


def _install_signal_handlers(runner) -> None:
    def handle(signum, _frame):
        logger.info("Received %s; draining queue before exit", signal.Signals(signum).name)
        runner.stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Surface legacy SKYPORTAL_AGENT_* notices; Python hides them by default.
    _env.enable_deprecation_warnings()
    logging.captureWarnings(True)
    config = AgentConfig.from_env()
    stop_event = threading.Event()
    if config.role == ROLE_EXPERIMENTS:
        runner = build_runner(config, stop_event=stop_event)
    else:
        runner = build_kubernetes_runner(config, stop_event=stop_event)
    if config.role == ROLE_CLUSTER:
        _start_command_poller(config, stop_event)
    _install_signal_handlers(runner)

    health = HealthServer(config.healthz_port)
    health.start()
    logger.info(
        "skyportalai-agent started: role=%s base_url=%s interval=%ss state_dir=%s healthz=:%d",
        config.role,
        config.base_url,
        config.interval_seconds,
        config.state_dir,
        health.port,
    )
    try:
        runner.run_forever()
    finally:
        health.stop()
        logger.info("skyportalai-agent stopped")


if __name__ == "__main__":
    main()
