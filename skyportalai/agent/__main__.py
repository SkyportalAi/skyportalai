"""Agent entrypoint — ``python -m skyportalai.agent`` / ``skyportal-agent``.

Thin orchestration: assemble the runner from config, start the /healthz probe
server on a background thread, route SIGTERM/SIGINT to a graceful stop (which
triggers the final queue flush), and run the loop on the main thread.
"""

from __future__ import annotations

import logging
import signal
import threading

from .._client import Skyportal
from .config import AgentConfig
from .health import HealthServer
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


def _install_signal_handlers(runner: AgentRunner) -> None:
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
    config = AgentConfig.from_env()
    runner = build_runner(config)
    _install_signal_handlers(runner)

    health = HealthServer(config.healthz_port)
    health.start()
    logger.info(
        "skyportal-agent started: base_url=%s interval=%ss state_dir=%s healthz=:%d",
        config.base_url,
        config.interval_seconds,
        config.state_dir,
        health.port,
    )
    try:
        runner.run_forever()
    finally:
        health.stop()
        logger.info("skyportal-agent stopped")


if __name__ == "__main__":
    main()
