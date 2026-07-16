"""Handle for one agent chat, bound to a client and a ``chat_id``."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .types import ApprovalResult, ChatStatus, MessagesPage, PendingApproval

if TYPE_CHECKING:
    from ._client import Skyportal

#: ``on_approval`` gets each pending approval; True approves, False rejects,
#: None leaves it for the caller.
ApprovalCallback = Callable[[PendingApproval], "bool | None"]


class Chat:
    """Sugar over ``client.chat``: every method delegates with this chat's id.

    Returned by ``client.chat.create_chat()``; ``raw`` keeps the creation
    payload (``chat_id``, ``poll_url``, initial ``status``).
    """

    def __init__(self, client: "Skyportal", chat_id: int, raw: dict | None = None):
        self._client = client
        self.chat_id = int(chat_id)
        self.raw = dict(raw or {})

    def __repr__(self) -> str:
        return f"Chat(chat_id={self.chat_id})"

    def send(self, message: str) -> None:
        self._client.chat.send_message(self.chat_id, message)

    def status(self) -> ChatStatus:
        return self._client.chat.get_status(self.chat_id)

    def messages(self, after_sequence: int = 0, limit: int = 100) -> MessagesPage:
        return self._client.chat.get_messages(
            self.chat_id, after_sequence=after_sequence, limit=limit
        )

    def approve(self, approval_id: str, *, approval_type: str = "bash_command",
                command: str | None = None) -> ApprovalResult:
        return self._client.chat.approve(
            self.chat_id, approval_id, approval_type=approval_type, command=command
        )

    def reject(self, approval_id: str, *, approval_type: str = "bash_command",
               reason: str | None = None) -> ApprovalResult:
        return self._client.chat.reject(
            self.chat_id, approval_id, approval_type=approval_type, reason=reason
        )

    def select_server(self, server_id: int) -> None:
        self._client.chat.select_server(self.chat_id, server_id)

    def cancel(self, reason: str | None = None) -> dict:
        return self._client.chat.cancel(self.chat_id, reason=reason)

    def wait(self, poll_interval: float = 1.0, timeout: float = 300.0,
             on_approval: ApprovalCallback | None = None) -> ChatStatus:
        return self._client.chat.wait(
            self.chat_id, poll_interval=poll_interval, timeout=timeout,
            on_approval=on_approval,
        )

    def execution_status(self) -> dict:
        return self._client.chat.get_execution_status(self.chat_id)

    def events(self, **filters) -> dict:
        return self._client.chat.get_events(self.chat_id, **filters)

    def tool_calls(self, *, limit: int = 100) -> dict:
        return self._client.chat.get_tool_calls(self.chat_id, limit=limit)

    def reasoning(self, *, limit: int = 100) -> dict:
        return self._client.chat.get_reasoning(self.chat_id, limit=limit)

    def plan(self) -> dict:
        return self._client.chat.get_plan(self.chat_id)

    def evaluations(self, *, evaluator_type: str | None = None) -> dict:
        return self._client.chat.get_evaluations(self.chat_id, evaluator_type=evaluator_type)

    def environment(self) -> dict:
        return self._client.chat.get_environment(self.chat_id)
