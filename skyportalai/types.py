"""Typed objects returned by the SDK."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class User:
    """A SkyPortal user.

    ``raw`` holds the full auth-check payload so new server fields (id, email…)
    are available immediately, before the SDK grows typed accessors for them.
    """

    name: str
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        return cls(name=data.get("name", ""), raw=dict(data))


@dataclass(frozen=True)
class KubernetesCluster:
    """A Kubernetes cluster connected to the authenticated SkyPortal account."""

    id: int
    name: str
    environment: str = "Custom"
    status: str = ""
    api_endpoint: str = ""
    namespaces: list[str] = field(default_factory=list)
    connection_verified: bool | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "KubernetesCluster":
        verified = data.get("connection_verified")
        return cls(
            id=int(data.get("id", 0) or 0),
            name=str(data.get("name") or data.get("hostname") or ""),
            environment=str(data.get("environment") or data.get("host_type") or "Custom"),
            status=str(data.get("status") or ""),
            api_endpoint=str(data.get("api_endpoint") or ""),
            namespaces=[str(item) for item in data.get("namespaces", [])],
            connection_verified=bool(verified) if verified is not None else None,
            raw=dict(data),
        )


@dataclass(frozen=True)
class PendingApproval:
    """An approval the agent is blocked on (bash command or plan)."""

    approval_id: str
    type: str = ""
    command: str = ""
    plan_id: str = ""
    reason: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "PendingApproval":
        return cls(
            approval_id=str(data.get("approval_id", "") or ""),
            type=data.get("type", "") or "",
            command=data.get("command", "") or "",
            plan_id=str(data.get("plan_id", "") or ""),
            reason=data.get("reason", "") or "",
            raw=dict(data),
        )


@dataclass(frozen=True)
class ApprovalResult:
    """Outcome of submitting an approval decision."""

    success: bool = False
    decision: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "ApprovalResult":
        return cls(
            success=bool(data.get("success", False)),
            decision=data.get("decision", "") or "",
            raw=dict(data),
        )


@dataclass(frozen=True)
class ChatStatus:
    """Workflow status for a chat, as returned by the status endpoint."""

    status: str
    workflow_type: str = ""
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "ChatStatus":
        return cls(
            status=data.get("status", "") or "",
            workflow_type=data.get("workflow_type", "") or "",
            pending_approvals=[
                PendingApproval.from_dict(a)
                for a in data.get("pending_approvals") or []
                if isinstance(a, dict)
            ],
            raw=dict(data),
        )


def _flatten_content(content: object) -> str:
    """Normalize message content to plain text.

    The live server stores content as a list of typed blocks
    (``[{"type": "text", "text": "..."}]``), not a plain string; ``Message``
    promises ``content: str``, so block lists are flattened to their text
    parts (joined with newlines). The untouched value stays in ``raw``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(block, str) and block:
                parts.append(block)
        return "\n".join(parts)
    return ""


@dataclass(frozen=True)
class Message:
    """One chat message; ``raw`` keeps every server field."""

    role: str = ""
    content: str = ""
    sequence: int = 0
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        try:
            sequence = int(data.get("sequence", 0) or 0)
        except (TypeError, ValueError):
            sequence = 0
        return cls(
            role=data.get("role", "") or "",
            content=_flatten_content(data.get("content")),
            sequence=sequence,
            raw=dict(data),
        )


@dataclass(frozen=True)
class MessagesPage:
    """One page of chat messages plus the ``has_more`` pagination flag."""

    messages: list[Message] = field(default_factory=list)
    has_more: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "MessagesPage":
        return cls(
            messages=[
                Message.from_dict(m)
                for m in data.get("messages") or []
                if isinstance(m, dict)
            ],
            has_more=bool(data.get("has_more", False)),
            raw=dict(data),
        )
