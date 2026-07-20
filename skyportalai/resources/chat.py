"""Agent-chat resource: wraps the headless agent REST API.

Endpoint contract (see ``skyportal/urls.py`` and
``website/chat/api/headless_agent.py`` / ``headless_observability.py`` in the
server repo): plain JSON under ``/api/v1/agent/chat/...``, Bearer-key auth,
poll-based — the agent runs server-side in a background thread and clients
poll ``get_status``.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING
from urllib.parse import quote

from .._exceptions import WaitTimeoutError
from ..chat import ApprovalCallback, Chat
from ..types import ApprovalResult, ChatStatus, MessagesPage

if TYPE_CHECKING:
    from .._client import Skyportal

#: ``wait()`` keeps polling while the workflow is in one of these states.
BUSY_STATUSES = frozenset({"processing", "uninitialized"})


class ChatResource:
    """``client.chat`` — drive the SkyPortal ops agent over REST.

    Every method takes the ``chat_id`` explicitly; ``create_chat()`` returns a
    bound :class:`~skyportalai.chat.Chat` handle so callers can chain
    ``chat.wait()`` / ``chat.send(...)`` without threading the id around.
    """

    def __init__(self, client: "Skyportal"):
        self._client = client

    # -- lifecycle -----------------------------------------------------------

    def create_chat(
        self,
        message: str,
        *,
        server_id: int | None = None,
        server_ids: list[int] | None = None,
        active_server_id: int | None = None,
        active_host_id: int | None = None,
        selected_namespaces: dict[int | str, list[str]] | None = None,
    ) -> Chat:
        """Create a chat and send the first message (agent starts processing).

        ``server_id`` is the backward-compatible single-server form.
        ``server_ids`` selects the full multi-server scope atomically before
        the first turn starts. The active ids and namespace scope are valid
        only with ``server_ids``; mixing the plural and singular forms is
        ambiguous and raises ``ValueError`` before making a request.
        """
        if server_id is not None and server_ids is not None:
            raise ValueError("server_id and server_ids cannot be used together")
        if server_ids is None and (
            active_server_id is not None
            or active_host_id is not None
            or selected_namespaces is not None
        ):
            raise ValueError(
                "server_ids is required with active_server_id, active_host_id, "
                "or selected_namespaces"
            )

        body: dict = {"message": message}
        if server_ids is not None:
            body["selected_server_ids"] = list(server_ids)
            if active_server_id is not None:
                body["active_server_id"] = active_server_id
            if active_host_id is not None:
                body["active_host_id"] = active_host_id
            if selected_namespaces is not None:
                body["selected_namespaces"] = selected_namespaces
        elif server_id is not None:
            body["server_id"] = server_id
        data = self._client._request("POST", "/api/v1/agent/chat/", json=body)
        return Chat(self._client, int(data.get("chat_id", 0) or 0), raw=dict(data))

    def send_message(self, chat_id: int, message: str) -> dict:
        """Send a follow-up message to an existing chat (409 while processing)."""
        return self._client._request(
            "POST", f"/api/v1/agent/chat/{int(chat_id)}/message/",
            json={"message": message},
        )

    def get_status(self, chat_id: int) -> ChatStatus:
        """Poll the workflow status (idle / processing / awaiting_approval / …)."""
        data = self._client._request("GET", f"/api/v1/agent/chat/{int(chat_id)}/status/")
        return ChatStatus.from_dict(data)

    def get_messages(self, chat_id: int, *, after_sequence: int = 0,
                     limit: int = 100) -> MessagesPage:
        """Fetch messages after ``after_sequence`` (cursor pagination)."""
        data = self._client._request(
            "GET", f"/api/v1/agent/chat/{int(chat_id)}/messages/",
            params={"after_sequence": after_sequence, "limit": limit},
        )
        return MessagesPage.from_dict(data)

    def submit_approval(self, chat_id: int, approval_id: str, *, decision: str,
                        approval_type: str = "bash_command",
                        command: str | None = None,
                        reason: str | None = None) -> ApprovalResult:
        """Submit an approval decision (``approved`` or ``rejected``)."""
        body: dict = {"decision": decision, "type": approval_type}
        if command is not None:
            body["command"] = command
        if reason is not None:
            body["rejection_reason"] = reason
        data = self._client._request(
            "POST", self._approval_path(chat_id, approval_id), json=body,
        )
        return ApprovalResult.from_dict(data)

    def approve(self, chat_id: int, approval_id: str, *,
                approval_type: str = "bash_command",
                command: str | None = None) -> ApprovalResult:
        """Approve a pending bash command or plan."""
        return self.submit_approval(
            chat_id, approval_id, decision="approved",
            approval_type=approval_type, command=command,
        )

    def reject(self, chat_id: int, approval_id: str, *,
               approval_type: str = "bash_command",
               reason: str | None = None) -> ApprovalResult:
        """Reject a pending bash command or plan."""
        return self.submit_approval(
            chat_id, approval_id, decision="rejected",
            approval_type=approval_type, reason=reason,
        )

    def select_server(self, chat_id: int, server_id: int) -> dict:
        """Point the chat's agent at one of the account's servers."""
        return self._client._request(
            "POST", f"/api/v1/agent/chat/{int(chat_id)}/select-server/",
            json={"server_id": server_id},
        )

    def select_servers(self, chat_id: int, server_ids: list[int], *,
                       active_server_id: int | None = None,
                       active_host_id: int | None = None,
                       selected_namespaces: dict[int | str, list[str]] | None = None) -> dict:
        """Replace a chat's full multi-server execution scope.

        ``server_ids`` contains the account server IDs that the agent may use.
        An empty list clears the scope. ``active_server_id`` chooses the
        default execution target. Omitting ``active_host_id`` preserves the
        terminal/Jupyter binding when possible, and omitting
        ``selected_namespaces`` preserves namespace selections for servers
        that remain in scope. Pass ``{}`` to clear all namespace selections;
        ``["__all__"]`` selects every namespace on a Kubernetes server. Call
        this between turns, while the chat is not actively processing.
        """
        body: dict = {"selected_server_ids": list(server_ids)}
        if active_server_id is not None:
            body["active_server_id"] = active_server_id
        if active_host_id is not None:
            body["active_host_id"] = active_host_id
        if selected_namespaces is not None:
            body["selected_namespaces"] = selected_namespaces
        return self._client._request(
            "POST", f"/api/v1/agent/chat/{int(chat_id)}/select-servers/", json=body,
        )

    def cancel(self, chat_id: int, *, reason: str | None = None) -> dict:
        """Cancel the active workflow (409 from the server if it is idle)."""
        body: dict = {}
        if reason is not None:
            body["reason"] = reason
        return self._client._request(
            "POST", f"/api/v1/agent/chat/{int(chat_id)}/cancel/", json=body,
        )

    def wait(self, chat_id: int, *, poll_interval: float = 1.0,
             timeout: float = 300.0,
             on_approval: ApprovalCallback | None = None) -> ChatStatus:
        """Poll until the workflow settles, or raise ``WaitTimeoutError``.

        ``processing`` and ``uninitialized`` keep polling. On
        ``awaiting_approval``: with no callback the status is returned for the
        caller to decide; with ``on_approval``, each pending approval is passed
        to the callback — True approves, False rejects, None leaves it — and
        polling continues if any decision was submitted.
        """
        deadline = time.monotonic() + timeout
        while True:
            current = self.get_status(chat_id)
            if current.status == "awaiting_approval":
                if on_approval is None:
                    return current
                acted = False
                for approval in current.pending_approvals:
                    decision = on_approval(approval)
                    if decision is None:
                        continue
                    self.submit_approval(
                        chat_id, approval.approval_id,
                        decision="approved" if decision else "rejected",
                        approval_type=approval.type or "bash_command",
                        command=approval.command or None,
                    )
                    acted = True
                if not acted:
                    return current
            elif current.status not in BUSY_STATUSES:
                return current
            if time.monotonic() >= deadline:
                raise WaitTimeoutError(
                    f"Chat {chat_id} was still busy after {timeout:.0f}s."
                )
            time.sleep(poll_interval)

    # -- read-only observability ----------------------------------------------

    def get_execution_status(self, chat_id: int) -> dict:
        """Detailed status: workflow id, current plan, approvals, metadata."""
        return self._client._request(
            "GET", f"/api/v1/agent/chat/{int(chat_id)}/execution-status/",
        )

    def get_events(self, chat_id: int, *, after_timestamp: str | None = None,
                   event_types: list[str] | str | None = None,
                   limit: int = 100) -> dict:
        """Workflow event trace (cursor pagination via ``next_timestamp``)."""
        params: dict = {"limit": limit}
        if after_timestamp is not None:
            params["after_timestamp"] = after_timestamp
        if event_types is not None:
            if not isinstance(event_types, str):
                event_types = ",".join(event_types)
            params["event_types"] = event_types
        return self._client._request(
            "GET", f"/api/v1/agent/chat/{int(chat_id)}/events/", params=params,
        )

    def get_tool_calls(self, chat_id: int, *, limit: int = 100) -> dict:
        """Tool calls the agent made (name, input, output, exit code)."""
        return self._client._request(
            "GET", f"/api/v1/agent/chat/{int(chat_id)}/tool-calls/",
            params={"limit": limit},
        )

    def get_reasoning(self, chat_id: int, *, limit: int = 100) -> dict:
        """ReAct reasoning steps (goal, iterations, goal_achieved)."""
        return self._client._request(
            "GET", f"/api/v1/agent/chat/{int(chat_id)}/reasoning/",
            params={"limit": limit},
        )

    def get_plan(self, chat_id: int) -> dict:
        """The chat's active plan, or None — a chat has at most one active
        plan at a time; no history is kept once it's replaced or cleared."""
        return self._client._request("GET", f"/api/v1/agent/chat/{int(chat_id)}/plan/")

    def get_evaluations(self, chat_id: int, *, evaluator_type: str | None = None) -> dict:
        """Evaluator results plus a pass/fail summary."""
        params = {"evaluator_type": evaluator_type} if evaluator_type else None
        return self._client._request(
            "GET", f"/api/v1/agent/chat/{int(chat_id)}/evaluations/", params=params,
        )

    def get_environment(self, chat_id: int) -> dict:
        """Execution environment: server id, agent name, workflow type, status."""
        return self._client._request(
            "GET", f"/api/v1/agent/chat/{int(chat_id)}/environment/",
        )

    @staticmethod
    def _approval_path(chat_id: int, approval_id: str) -> str:
        return (
            f"/api/v1/agent/chat/{int(chat_id)}/approve/"
            f"{quote(str(approval_id), safe='')}/"
        )
