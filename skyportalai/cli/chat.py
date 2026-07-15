"""Public chat subcommands backed exclusively by ``client.chat``."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from skyportalai.types import ChatStatus, MessagesPage

from .context import CLIContext, run_command
from .context import get_state as _state

chat_app = typer.Typer(help="Create, inspect, approve, and cancel ops-agent chats.")


@chat_app.command("send")
def send(
    context: typer.Context,
    message: Annotated[str, typer.Argument(help="Instruction for the ops agent.")],
    server_id: Annotated[
        int | None,
        typer.Option("--server", help="Connected server ID for a new chat."),
    ] = None,
    chat_id: Annotated[
        int | None,
        typer.Option("--chat-id", min=1, help="Send a follow-up to an existing chat."),
    ] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for the turn to settle.")] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=0.001, help="Maximum wait in seconds."),
    ] = 300.0,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", min=0.0, help="Seconds between status polls."),
    ] = 1.0,
) -> None:
    """Start a chat or send a follow-up message."""
    if chat_id is not None and server_id is not None:
        state = _state(context)
        state.output.failure("--server can only be used when creating a new chat.")
        raise typer.Exit(2)

    def operation(
        state: CLIContext,
    ) -> tuple[int, ChatStatus | None, MessagesPage | None, dict[str, Any]]:
        client = state.client()
        if chat_id is None:
            chat = client.chat.create_chat(message, server_id=server_id)
            current_chat_id = chat.chat_id
            initial = dict(chat.raw)
        else:
            current_chat_id = chat_id
            initial = dict(client.chat.send_message(chat_id, message))
        status = (
            client.chat.wait(current_chat_id, timeout=timeout, poll_interval=poll_interval)
            if wait
            else None
        )
        page = (
            client.chat.get_messages(current_chat_id, after_sequence=0, limit=100)
            if status is not None
            else None
        )
        return current_chat_id, status, page, initial

    current_chat_id, status, page, initial = run_command(context, operation)
    state = _state(context)
    status_name = status.status if status is not None else str(initial.get("status", "processing"))
    human = f"Chat #{current_chat_id}: {status_name}"
    if page is not None:
        human = f"{human}\n{_messages_text(page)}"
    state.output.success(
        {
            "chat_id": current_chat_id,
            "status": status_name,
            "result": status or initial,
            "messages": page.messages if page is not None else [],
        },
        human=human,
    )
    _exit_for_status(status)


@chat_app.command("status")
def status(context: typer.Context, chat_id: Annotated[int, typer.Argument(min=1)]) -> None:
    """Show a chat's current workflow status."""
    current = run_command(context, lambda state: state.client().chat.get_status(chat_id))
    _state(context).output.success(
        current,
        human=(
            f"Chat #{chat_id}: {current.status}\n"
            f"Pending approvals: {len(current.pending_approvals)}"
        ),
    )
    _exit_for_status(current)


@chat_app.command("messages")
def messages(
    context: typer.Context,
    chat_id: Annotated[int, typer.Argument(min=1)],
    after_sequence: Annotated[
        int,
        typer.Option("--after-sequence", min=0, help="Only return newer messages."),
    ] = 0,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 100,
) -> None:
    """List messages using the server's sequence cursor."""
    page = run_command(
        context,
        lambda state: state.client().chat.get_messages(
            chat_id,
            after_sequence=after_sequence,
            limit=limit,
        ),
    )
    _state(context).output.success(page, human=_messages_text(page))


@chat_app.command("approve")
def approve(
    context: typer.Context,
    chat_id: Annotated[int, typer.Argument(min=1)],
    approval_id: Annotated[str, typer.Argument(help="Pending approval identifier.")],
    approval_type: Annotated[
        str,
        typer.Option("--type", help="Approval type, usually bash_command or plan."),
    ] = "bash_command",
    command: Annotated[
        str | None,
        typer.Option("--command", help="Approved command when required by the server."),
    ] = None,
) -> None:
    """Approve one pending agent action."""
    result = run_command(
        context,
        lambda state: state.client().chat.approve(
            chat_id,
            approval_id,
            approval_type=approval_type,
            command=command,
        ),
    )
    _state(context).output.success(result, human=f"Approval {approval_id}: {result.decision or 'approved'}")


@chat_app.command("reject")
def reject(
    context: typer.Context,
    chat_id: Annotated[int, typer.Argument(min=1)],
    approval_id: Annotated[str, typer.Argument(help="Pending approval identifier.")],
    approval_type: Annotated[
        str,
        typer.Option("--type", help="Approval type, usually bash_command or plan."),
    ] = "bash_command",
    reason: Annotated[str | None, typer.Option("--reason", help="Reason for rejection.")] = None,
) -> None:
    """Reject one pending agent action."""
    result = run_command(
        context,
        lambda state: state.client().chat.reject(
            chat_id,
            approval_id,
            approval_type=approval_type,
            reason=reason,
        ),
    )
    _state(context).output.success(result, human=f"Approval {approval_id}: {result.decision or 'rejected'}")


@chat_app.command("cancel")
def cancel(
    context: typer.Context,
    chat_id: Annotated[int, typer.Argument(min=1)],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Cancel an active chat workflow."""
    result = run_command(
        context,
        lambda state: state.client().chat.cancel(chat_id, reason=reason),
    )
    _state(context).output.success(result, human=f"Chat #{chat_id}: {result.get('status', 'cancelled')}")


@chat_app.command("wait")
def wait_for_chat(
    context: typer.Context,
    chat_id: Annotated[int, typer.Argument(min=1)],
    timeout: Annotated[float, typer.Option("--timeout", min=0.001)] = 300.0,
    poll_interval: Annotated[float, typer.Option("--poll-interval", min=0.0)] = 1.0,
) -> None:
    """Wait until a chat settles or needs approval."""
    current = run_command(
        context,
        lambda state: state.client().chat.wait(
            chat_id,
            timeout=timeout,
            poll_interval=poll_interval,
        ),
    )
    _state(context).output.success(current, human=f"Chat #{chat_id}: {current.status}")
    _exit_for_status(current)


def _messages_text(page: MessagesPage) -> str:
    if not page.messages:
        return "No messages."
    return "\n".join(
        f"[{message.sequence}] {message.role or 'unknown'}: {message.content}"
        for message in page.messages
    )


def _exit_for_status(status: ChatStatus | None) -> None:
    if status is None:
        return
    if status.status == "awaiting_approval":
        raise typer.Exit(2)
    if status.status == "error":
        raise typer.Exit(1)