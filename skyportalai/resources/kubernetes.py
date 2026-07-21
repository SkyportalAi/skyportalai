"""Kubernetes cluster lifecycle resource."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..types import KubernetesCluster

if TYPE_CHECKING:
    from .._client import Skyportal


class KubernetesResource:
    """Connect, list, and disconnect Kubernetes clusters.

    Kubeconfigs are sent only to the authenticated SkyPortal API, where the
    existing server-side validation and encrypted-storage path handles them.
    No lifecycle response returns kubeconfig credential material.
    """

    def __init__(self, client: "Skyportal"):
        self._client = client

    def list(self) -> list[KubernetesCluster]:
        data = self._client._request("GET", "/api/v1/infrastructure/kubernetes/")
        return [
            KubernetesCluster.from_dict(item)
            for item in data.get("clusters") or []
            if isinstance(item, dict)
        ]

    def connect(
        self,
        name: str,
        kubeconfig: str,
        *,
        environment: str = "Custom",
    ) -> KubernetesCluster:
        name = name.strip()
        kubeconfig = kubeconfig.strip()
        environment = environment.strip() or "Custom"
        if not name:
            raise ValueError("name cannot be empty")
        if not kubeconfig:
            raise ValueError("kubeconfig cannot be empty")
        data = self._client._request(
            "POST",
            "/api/v1/infrastructure/kubernetes/",
            json={"name": name, "environment": environment, "kubeconfig": kubeconfig},
        )
        return KubernetesCluster.from_dict(data.get("cluster") or {})

    def disconnect(self, cluster_id: int) -> dict:
        if isinstance(cluster_id, bool) or not isinstance(cluster_id, int) or cluster_id <= 0:
            raise ValueError("cluster_id must be a positive integer")
        return self._client._request(
            "DELETE",
            f"/api/v1/infrastructure/kubernetes/{cluster_id}/",
        )
