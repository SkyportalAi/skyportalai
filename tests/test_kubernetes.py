from skyportalai import KubernetesCluster
from skyportalai._client import Skyportal


def _client():
    return Skyportal(api_key="sk-test", base_url="https://api.test")


def test_connect_sends_kubeconfig_without_returning_it(requests_mock):
    requests_mock.post(
        "https://api.test/api/v1/infrastructure/kubernetes/",
        json={
            "success": True,
            "cluster": {
                "id": 17,
                "name": "prod",
                "environment": "Production",
                "target_kind": "kubernetes",
                "status": "connected",
                "connection_verified": True,
                "namespaces": ["default"],
            },
        },
        status_code=201,
    )

    cluster = _client().kubernetes.connect(
        "prod",
        "apiVersion: v1\nkind: Config\nclusters: []",
        environment="Production",
    )

    assert isinstance(cluster, KubernetesCluster)
    assert cluster.id == 17
    assert cluster.connection_verified is True
    sent = requests_mock.last_request.json()
    assert sent["kubeconfig"].startswith("apiVersion")


def test_list_and_disconnect(requests_mock):
    requests_mock.get(
        "https://api.test/api/v1/infrastructure/kubernetes/",
        json={"clusters": [{"id": 17, "name": "prod", "namespaces": ["default", "vllm"]}]},
    )
    requests_mock.delete(
        "https://api.test/api/v1/infrastructure/kubernetes/17/",
        json={"success": True, "cluster_id": 17},
    )

    client = _client()
    assert client.kubernetes.list()[0].namespaces == ["default", "vllm"]
    assert client.kubernetes.disconnect(17)["success"] is True


def test_lifecycle_validates_empty_values_without_network():
    client = _client()
    for name, config in (("", "config"), ("prod", "")):
        try:
            client.kubernetes.connect(name, config)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_connect_redacts_kubeconfig_from_raw(requests_mock):
    """Server echo of kubeconfig must not appear in the raw field."""
    requests_mock.post(
        "https://api.test/api/v1/infrastructure/kubernetes/",
        json={
            "success": True,
            "cluster": {
                "id": 5,
                "name": "staging",
                "environment": "Staging",
                "status": "connected",
                "connection_verified": True,
                "namespaces": [],
                "kubeconfig": "SENSITIVE_CREDENTIAL",
            },
        },
        status_code=201,
    )

    cluster = _client().kubernetes.connect("staging", "apiVersion: v1", environment="Staging")

    assert "kubeconfig" not in cluster.raw
    assert "SENSITIVE_CREDENTIAL" not in str(cluster.raw)


def test_disconnect_rejects_boolean_and_non_integer_ids():
    client = _client()
    for bad_id in (True, False, 1.5, "1"):
        try:
            client.kubernetes.disconnect(bad_id)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for cluster_id={bad_id!r}")
