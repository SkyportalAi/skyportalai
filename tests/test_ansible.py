import pytest

from skyportalai import AnsibleDeployment, AnsiblePlaybook
from skyportalai._client import Skyportal


def _client():
    return Skyportal(api_key="sk-test", base_url="https://api.test")


def test_create_list_get_update_delete_and_deploy(requests_mock):
    base = "https://api.test/api/v1/infrastructure/ansible"
    playbook_payload = {
        "id": 4,
        "name": "bootstrap",
        "description": "Base packages",
        "content": "- hosts: all\n  tasks: []\n",
        "updated_at": "2026-07-24T12:00:00+00:00",
    }
    requests_mock.post(f"{base}/", json={"playbook": playbook_payload}, status_code=201)
    requests_mock.get(
        f"{base}/",
        json={"playbooks": [{key: value for key, value in playbook_payload.items() if key != "content"}]},
    )
    requests_mock.get(f"{base}/4/", json={"playbook": playbook_payload})
    requests_mock.patch(
        f"{base}/4/",
        json={"playbook": {**playbook_payload, "description": "Updated"}},
    )
    requests_mock.delete(f"{base}/4/", json={"success": True, "playbook_id": 4})
    requests_mock.post(
        f"{base}/4/deploy/",
        json={
            "chat_id": 91,
            "playbook_id": 4,
            "server_id": 12,
            "status": "processing",
            "poll_url": "/api/v1/agent/chat/91/status/",
        },
        status_code=202,
    )

    resource = _client().ansible
    created = resource.create(
        "bootstrap",
        "- hosts: all\n  tasks: []",
        description="Base packages",
    )
    assert isinstance(created, AnsiblePlaybook)
    assert resource.list()[0].content == ""
    assert resource.get(4).content.startswith("- hosts")
    assert resource.update(4, description="Updated").description == "Updated"
    assert resource.delete(4)["success"] is True
    deployment = resource.deploy(4, server_id=12)
    assert isinstance(deployment, AnsibleDeployment)
    assert deployment.chat_id == 91


def test_lifecycle_validation_happens_before_network():
    resource = _client().ansible
    with pytest.raises(ValueError):
        resource.create("", "- hosts: all")
    with pytest.raises(ValueError):
        resource.create("name", "")
    with pytest.raises(ValueError):
        resource.get(True)
    with pytest.raises(ValueError):
        resource.update(1)
    with pytest.raises(ValueError):
        resource.deploy(1, server_id=0)
