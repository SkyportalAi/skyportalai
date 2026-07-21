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
    server_ids: Annotated[
        list[int] | None,
        typer.Option(
            "--server",
            "-s",
            min=1,
            help="Server ID for a new chat; repeat to set multi-host scope.",
        ),
    ] = None,
    active_server_id: Annotated[
        int | None,
        typer.Option(
            "--active-server",
            min=1,
            help="Default execution server; defaults to the first --server.",
        ),
    ] = None,
    active_host_id: Annotated[
        int | None,
        typer.Option(
            "--active-host",
            min=1,
            help="Terminal/Jupyter host; defaults to the active server for a new chat.",
        ),
    ] = None,
    namespaces: Annotated[
        list[str] | None,
        typer.Option(
            "--namespace",
            help=(
                "Kubernetes scope as SERVER_ID=NAMESPACE; repeat as needed, "
                "or use __all__ for cluster-wide scope."
            ),
        ),
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
    selected_server_ids = list(dict.fromkeys(server_ids or []))
    has_scope_options = bool(
        selected_server_ids
        or active_server_id is not None
        or active_host_id is not None
        or namespaces
    )

    if chat_id is not None and has_scope_options:
        state = _state(context)
        state.output.failure(
            "--server can only be used when creating a new chat; --active-server, "
            "--active-host, and --namespace are also creation-only."
        )
        raise typer.Exit(2)
    if not selected_server_ids and has_scope_options:
        state = _state(context)
        state.output.failure(
            "--active-server, --active-host, and --namespace require at least one --server."
        )
        raise typer.Exit(2)

    if selected_server_ids:
        error = _scope_option_error(
            selected_server_ids,
            active_server_id=active_server_id,
            active_host_id=active_host_id,
            namespaces=namespaces or [],
            clear_namespaces=False,
            clear_scope=False,
        )
        if error is not None:
            state = _state(context)
            state.output.failure(error)
            raise typer.Exit(2)

    selected_namespaces = _parse_namespaces(namespaces or []) or None
    effective_active_server_id = active_server_id
    if effective_active_server_id is None and selected_server_ids:
        effective_active_server_id = active_host_id or selected_server_ids[0]

    # Keep the established one-host request intact for compatibility with
    # deployments that predate atomic first-turn multi-host scope.
    use_legacy_single_server = (
        len(selected_server_ids) == 1
        and active_server_id is None
        and active_host_id is None
        and selected_namespaces is None
    )

    def operation(
        state: CLIContext,
    ) -> tuple[int, ChatStatus | None, MessagesPage | None, dict[str, Any]]:
        client = state.client()
        if chat_id is None:
            if use_legacy_single_server:
                chat = client.chat.create_chat(message, server_id=selected_server_ids[0])
            elif selected_server_ids:
                chat = client.chat.create_chat(
                    message,
                    server_ids=selected_server_ids,
                    active_server_id=effective_active_server_id,
                    active_host_id=active_host_id,
                    selected_namespaces=selected_namespaces,
                )
            else:
                chat = client.chat.create_chat(message)
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


@chat_app.command("select-servers")
def select_servers(
    context: typer.Context,
    chat_id: Annotated[int, typer.Argument(min=1)],
    server_ids: Annotated[
        list[int] | None,
        typer.Option(
            "--server",
            "-s",
            min=1,
            help="Server ID to include in scope; repeat for multiple hosts.",
        ),
    ] = None,
    active_server_id: Annotated[
        int | None,
        typer.Option(
            "--active-server",
            min=1,
            help="Default execution server; defaults to the first --server.",
        ),
    ] = None,
    active_host_id: Annotated[
        int | None,
        typer.Option(
            "--active-host",
            min=1,
            help="Terminal/Jupyter host; omit to preserve its current binding.",
        ),
    ] = None,
    namespaces: Annotated[
        list[str] | None,
        typer.Option(
            "--namespace",
            help=(
                "Kubernetes scope as SERVER_ID=NAMESPACE; repeat as needed, "
                "or use __all__ for cluster-wide scope."
            ),
        ),
    ] = None,
    clear_namespaces: Annotated[
        bool,
        typer.Option("--clear-namespaces", help="Clear every Kubernetes namespace selection."),
    ] = False,
    clear_scope: Annotated[
        bool,
        typer.Option("--clear-scope", help="Explicitly clear every server from the chat scope."),
    ] = False,
) -> None:
    """Replace the full multi-server scope of an existing chat."""
    state = _state(context)
    selected_server_ids = list(dict.fromkeys(server_ids or []))

    error = _scope_option_error(
        selected_server_ids,
        active_server_id=active_server_id,
        active_host_id=active_host_id,
        namespaces=namespaces or [],
        clear_namespaces=clear_namespaces,
        clear_scope=clear_scope,
    )
    if error is not None:
        state.output.failure(error)
        raise typer.Exit(2)

    selected_namespaces = (
        {} if clear_namespaces else _parse_namespaces(namespaces or []) or None
    )
    effective_active_server_id = active_server_id
    if effective_active_server_id is None and selected_server_ids:
        effective_active_server_id = active_host_id or selected_server_ids[0]

    def operation(cli_state: CLIContext) -> dict[str, Any]:
        client = cli_state.client()
        current = client.chat.get_status(chat_id)
        if current.status in {"processing", "uninitialized", "awaiting_approval"}:
            raise typer.BadParameter(
                f"Chat #{chat_id} is {current.status}; finish the current turn or approval "
                "before changing scope."
            )
        return client.chat.select_servers(
            chat_id,
            selected_server_ids,
            active_server_id=effective_active_server_id,
            active_host_id=active_host_id,
            selected_namespaces=selected_namespaces,
        )

    try:
        result = run_command(context, operation)
    except typer.BadParameter as exc:
        state.output.failure(str(exc))
        raise typer.Exit(2) from None

    scope = result.get("selected_server_ids", selected_server_ids)
    shown_scope = ", ".join(str(server_id) for server_id in scope) or "cleared"
    state.output.success(
        result,
        human=f"Chat #{chat_id} server scope: {shown_scope}",
    )


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


def _scope_option_error(
    server_ids: list[int],
    *,
    active_server_id: int | None,
    active_host_id: int | None,
    namespaces: list[str],
    clear_namespaces: bool,
    clear_scope: bool,
) -> str | None:
    selected = set(server_ids)
    if clear_scope and selected:
        return "--clear-scope cannot be combined with --server."
    if not clear_scope and not selected:
        return "Provide at least one --server or use --clear-scope."
    if active_server_id is not None and active_server_id not in selected:
        return "--active-server must also be included with --server."
    if active_host_id is not None and active_host_id not in selected:
        return "--active-host must also be included with --server."
    if (
        active_server_id is not None
        and active_host_id is not None
        and active_server_id != active_host_id
    ):
        return "--active-server and --active-host must match when both are provided."
    if clear_namespaces and namespaces:
        return "--clear-namespaces cannot be combined with --namespace."
    try:
        parsed = _parse_namespaces(namespaces)
    except ValueError as exc:
        return str(exc)
    outside_scope = sorted(int(server_id) for server_id in parsed if int(server_id) not in selected)
    if outside_scope:
        return "Namespace server IDs must be included with --server: " + ", ".join(
            str(server_id) for server_id in outside_scope
        )
    return None


def _parse_namespaces(values: list[str]) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for value in values:
        server_text, separator, namespace = value.partition("=")
        if not separator or not server_text or not namespace:
            raise ValueError(
                f"Invalid --namespace {value!r}; expected SERVER_ID=NAMESPACE."
            )
        try:
            server_id = int(server_text)
        except ValueError as exc:
            raise ValueError(
                f"Invalid --namespace {value!r}; SERVER_ID must be a positive integer."
            ) from exc
        if server_id < 1:
            raise ValueError(
                f"Invalid --namespace {value!r}; SERVER_ID must be a positive integer."
            )
        parsed.setdefault(str(server_id), []).append(namespace)
    return parsed


def _exit_for_status(status: ChatStatus | None) -> None:
    if status is None:
        return
    if status.status == "awaiting_approval":
        raise typer.Exit(2)
    if status.status == "error":
        raise typer.Exit(1)
