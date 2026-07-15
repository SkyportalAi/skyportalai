"""Tests for HealthServer — the /healthz liveness endpoint for k8s probes."""

from __future__ import annotations

import threading

import requests

from skyportalai.agent.health import HealthServer


def test_healthz_returns_200_ok():
    server = HealthServer(0)  # port 0 -> OS-assigned ephemeral port
    server.start()
    try:
        resp = requests.get(f"http://127.0.0.1:{server.port}/healthz", timeout=2)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        server.stop()


def test_unknown_path_returns_404():
    server = HealthServer(0)
    server.start()
    try:
        resp = requests.get(f"http://127.0.0.1:{server.port}/nope", timeout=2)
        assert resp.status_code == 404
    finally:
        server.stop()


def test_stop_before_start_returns_promptly():
    # stdlib shutdown() blocks on an event only set by a running serve_forever(),
    # so stop() before start() must skip it. Run stop() on a side thread guarded
    # by a timeout so a regression (hang) fails this test instead of wedging the
    # whole suite.
    server = HealthServer(0)
    done = threading.Event()

    threading.Thread(target=lambda: (server.stop(), done.set()), daemon=True).start()

    assert done.wait(timeout=5), "stop() before start() hung"
