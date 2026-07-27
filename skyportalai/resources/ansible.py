"""Ansible playbook lifecycle and deployment resource."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..types import AnsibleDeployment, AnsiblePlaybook

if TYPE_CHECKING:
    from .._client import Skyportal


def _positive_id(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


class AnsibleResource:
    """Create, manage, list, and deploy account-owned Ansible playbooks."""

    def __init__(self, client: "Skyportal"):
        self._client = client

    def list(self) -> list[AnsiblePlaybook]:
        data = self._client._request("GET", "/api/v1/infrastructure/ansible/")
        return [
            AnsiblePlaybook.from_dict(item)
            for item in data.get("playbooks") or []
            if isinstance(item, dict)
        ]

    def get(self, playbook_id: int) -> AnsiblePlaybook:
        playbook_id = _positive_id(playbook_id, "playbook_id")
        data = self._client._request(
            "GET",
            f"/api/v1/infrastructure/ansible/{playbook_id}/",
        )
        return AnsiblePlaybook.from_dict(data.get("playbook") or {})

    def create(
        self,
        name: str,
        content: str,
        *,
        description: str = "",
    ) -> AnsiblePlaybook:
        name = name.strip()
        if not name:
            raise ValueError("name cannot be empty")
        if not content.strip():
            raise ValueError("content cannot be empty")
        data = self._client._request(
            "POST",
            "/api/v1/infrastructure/ansible/",
            json={
                "name": name,
                "description": description.strip(),
                "content": content,
            },
        )
        return AnsiblePlaybook.from_dict(data.get("playbook") or {})

    def update(
        self,
        playbook_id: int,
        *,
        name: str | None = None,
        content: str | None = None,
        description: str | None = None,
    ) -> AnsiblePlaybook:
        playbook_id = _positive_id(playbook_id, "playbook_id")
        body = {}
        if name is not None:
            if not name.strip():
                raise ValueError("name cannot be empty")
            body["name"] = name.strip()
        if content is not None:
            if not content.strip():
                raise ValueError("content cannot be empty")
            body["content"] = content
        if description is not None:
            body["description"] = description.strip()
        if not body:
            raise ValueError("provide name, content, description, or a combination")
        data = self._client._request(
            "PATCH",
            f"/api/v1/infrastructure/ansible/{playbook_id}/",
            json=body,
        )
        return AnsiblePlaybook.from_dict(data.get("playbook") or {})

    def delete(self, playbook_id: int) -> dict:
        playbook_id = _positive_id(playbook_id, "playbook_id")
        return self._client._request(
            "DELETE",
            f"/api/v1/infrastructure/ansible/{playbook_id}/",
        )

    def deploy(self, playbook_id: int, *, server_id: int) -> AnsibleDeployment:
        playbook_id = _positive_id(playbook_id, "playbook_id")
        server_id = _positive_id(server_id, "server_id")
        data = self._client._request(
            "POST",
            f"/api/v1/infrastructure/ansible/{playbook_id}/deploy/",
            json={"server_id": server_id},
        )
        return AnsibleDeployment.from_dict(data)
