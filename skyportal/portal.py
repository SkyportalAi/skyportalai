"""Skyportal authentication and headless-agent API client."""

import json
import os
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from skyportalai._client import _validate_base_url
from skyportalai._exceptions import SkyportalError
from skyportalai._version import __version__

PRODUCTION_MARKETING_URL = "https://skyportal.ai"
PRODUCTION_APP_URL = "https://app.skyportal.ai"
CLI_USER_AGENT = f"Skyportal-CLI/{__version__} (+https://app.skyportal.ai)"

_BUSY_CHAT_STATUSES = {"processing", "uninitialized"}
_IMMEDIATE_CHAT_STATUSES = {"awaiting_approval", "error"}
_PERMISSION_MODES = frozenset({"ask", "autoapprove"})
_TERMINAL_MESSAGE_SETTLEMENT_ATTEMPTS = 5
_TERMINAL_MESSAGE_SETTLEMENT_INITIAL_DELAY = 0.25
_TERMINAL_MESSAGE_SETTLEMENT_MAX_DELAY = 2.0


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep Bearer credentials from being forwarded through HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Keep this module-level callable patchable in tests while using a redirect-safe
# opener in production. urllib's default redirect handler copies Authorization
# headers to the redirected request, including redirects to a different host.
urlopen = build_opener(_NoRedirectHandler).open


class PortalError(RuntimeError):
    """Raised when communication with Skyportal fails."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class ChatTurnResult:
    """Result of one headless Skyportal agent turn."""

    chat_id: int
    status: str
    messages: List[Dict[str, Any]]
    pending_approvals: List[Dict[str, Any]]
    latest_sequence: int


class CredentialStore:
    """Persist API credentials with user-only file permissions."""

    DEFAULT_PATH = Path.home() / ".skyportal" / "credentials.json"

    @classmethod
    def get_path(cls) -> Path:
        path = os.environ.get("SKYPORTAL_CREDENTIALS_PATH")
        return Path(path).expanduser() if path else cls.DEFAULT_PATH

    @classmethod
    def load(cls) -> Optional[Dict[str, Any]]:
        path = cls.get_path()
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as credentials_file:
                credentials = json.load(credentials_file)
        except (OSError, json.JSONDecodeError) as error:
            raise PortalError(f"Could not read Skyportal credentials from {path}: {error}") from error
        if not isinstance(credentials, dict):
            raise PortalError(f"Invalid Skyportal credentials in {path}: expected an object")
        return credentials

    @classmethod
    def save(cls, credentials: Dict[str, Any]) -> None:
        path = cls.get_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as credentials_file:
                json.dump(credentials, credentials_file)
                credentials_file.flush()
                os.fsync(credentials_file.fileno())
            if os.name != "nt":
                temporary_path.chmod(0o600)
            os.replace(temporary_path, path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def clear(cls) -> None:
        try:
            cls.get_path().unlink()
        except FileNotFoundError:
            pass


class SkyportalClient:
    """Client for Skyportal API-key authentication and headless chat APIs."""

    def __init__(self, base_url: str, timeout: int = 30):
        if timeout <= 0:
            raise PortalError("Request timeout must be greater than zero")
        requested_base_url = base_url.rstrip("/")
        self.base_url = (
            PRODUCTION_APP_URL
            if requested_base_url == PRODUCTION_MARKETING_URL
            else requested_base_url
        )
        try:
            _validate_base_url(self.base_url)
        except SkyportalError as error:
            raise PortalError(str(error)) from error
        self.timeout = timeout

    def login(
        self,
        open_browser: bool = True,
        authorization_callback: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> Dict[str, Any]:
        """Open the page where a user can create a CLI API key."""
        verification_url = self.api_key_url()
        if authorization_callback:
            authorization_callback(verification_url, None)
        browser_opened = False
        if open_browser:
            try:
                browser_opened = bool(webbrowser.open(verification_url))
            except webbrowser.Error:
                browser_opened = False
        return {
            "verification_url": verification_url,
            "browser_opened": browser_opened,
        }

    def api_key_url(self) -> str:
        """Return the account API-key page used by the CLI."""
        return "{}/keys/?{}".format(self.base_url, urlencode({"source": "cli"}))

    def set_access_token(self, access_token: str, validate: bool = True) -> None:
        """Validate and persist an API key or short-lived access token."""
        token = access_token.strip()
        if not token:
            raise PortalError("The API credential cannot be empty")
        self._reject_agent_token(token)
        if validate:
            self.validate_access_token(token)
        CredentialStore.save(
            {
                "access_token": token,
                "token_type": "Bearer",
                "base_url": self.base_url,
            }
        )

    def validate_access_token(self, access_token: str) -> None:
        """Verify a credential without replacing the current saved credential."""
        self._reject_agent_token(access_token)
        self._request(
            "GET",
            "/api/v1/experiments/my-servers/",
            authenticated=False,
            bearer_token=access_token,
        )

    def is_authenticated(self) -> bool:
        token = self._env_access_token()
        if not token:
            credentials = CredentialStore.load()
            if credentials:
                stored_base_url = credentials.get("base_url")
                if stored_base_url not in (None, self.base_url):
                    return False
                token = credentials.get("access_token")
        return bool(token and not token.startswith("agt_"))

    def logout(self) -> None:
        """Remove locally stored API credentials."""
        CredentialStore.clear()

    def get_github_token_status(self) -> Dict[str, Any]:
        """Return whether a GitHub PAT is saved and its masked value."""
        return self._request("GET", "/api/v1/agent/github-token/")

    def save_github_token(self, token: str, repo: Optional[str] = None) -> Dict[str, Any]:
        """Validate and persist a GitHub Personal Access Token on the server.

        Raises PortalError with status_code=400 when the token is invalid or
        lacks the required scopes (the server validates it against GitHub).
        """
        body: Dict[str, Any] = {"token": token}
        if repo is not None:
            body["repo"] = repo
        return self._request("POST", "/api/v1/agent/github-token/save/", json_body=body)

    def delete_github_token(self) -> None:
        """Remove the stored GitHub Personal Access Token from the server."""
        self._request("DELETE", "/api/v1/agent/github-token/delete/")

    def get_permission_mode(self) -> str:
        """Return the account's shared agent-approval mode."""
        payload = self._request("GET", "/api/v1/agent/permission/")
        return self._parse_permission_mode(payload)

    def set_permission_mode(self, mode: str) -> str:
        """Persist ``ask`` or ``autoapprove`` for the authenticated account."""
        if mode not in _PERMISSION_MODES:
            raise PortalError("Permission mode must be 'ask' or 'autoapprove'")
        payload = self._request(
            "PUT",
            "/api/v1/agent/permission/",
            json_body={"permission_mode": mode},
        )
        return self._parse_permission_mode(payload)

    @staticmethod
    def _parse_permission_mode(payload: Any) -> str:
        mode = payload.get("permission_mode") if isinstance(payload, dict) else None
        if mode not in _PERMISSION_MODES:
            raise PortalError("Skyportal returned an invalid permission mode")
        return str(mode)

    def agents(self) -> List[Dict[str, str]]:
        """Describe the single Skyportal ReAct agent."""
        return [{"id": "skyportal", "name": "Skyportal Agent", "status": "ready"}]

    def servers(self) -> Any:
        """List servers owned by the authenticated user."""
        return self._request("GET", "/api/v1/experiments/my-servers/")

    def create_chat(
        self,
        message: str,
        server_id: Optional[int] = None,
        *,
        server_ids: Optional[List[int]] = None,
        active_server_id: Optional[int] = None,
        active_host_id: Optional[int] = None,
        selected_namespaces: Optional[Dict[Any, List[str]]] = None,
    ) -> Dict[str, Any]:
        """Create a headless chat and atomically scope its first turn."""
        if server_id is not None and server_ids is not None:
            raise PortalError("Use server_id or server_ids, not both")
        if server_ids is None and (
            active_server_id is not None
            or active_host_id is not None
            or selected_namespaces is not None
        ):
            raise PortalError(
                "server_ids is required with active_server_id, active_host_id, "
                "or selected_namespaces"
            )
        body: Dict[str, Any] = {"message": message}
        if server_id is not None:
            body["server_id"] = server_id
        if server_ids is not None:
            deduped = list(dict.fromkeys(int(value) for value in server_ids))
            body["selected_server_ids"] = deduped
            if active_server_id is None and deduped:
                active_server_id = deduped[0]
        if active_server_id is not None:
            body["active_server_id"] = active_server_id
        if active_host_id is not None:
            body["active_host_id"] = active_host_id
        if selected_namespaces is not None:
            body["selected_namespaces"] = selected_namespaces
        return self._request("POST", "/api/v1/agent/chat/", json_body=body)

    def send_chat_message(self, chat_id: int, message: str) -> Dict[str, Any]:
        """Send a follow-up message to a headless chat."""
        return self._request(
            "POST",
            "/api/v1/agent/chat/{}/message/".format(chat_id),
            json_body={"message": message},
        )

    def chat_status(
        self,
        chat_id: int,
        *,
        after_sequence: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Get workflow status, optionally with incremental messages."""
        path = "/api/v1/agent/chat/{}/status/".format(chat_id)
        if after_sequence is not None:
            path += "?" + urlencode({"after_sequence": after_sequence})
        return self._request("GET", path)

    def get_execution_status(self, chat_id: int) -> Dict[str, Any]:
        """Get detailed execution state for an explicit status inspection."""
        return self._request(
            "GET",
            "/api/v1/agent/chat/{}/execution-status/".format(chat_id),
        )

    def chat_messages(self, chat_id: int, after_sequence: int = 0) -> Dict[str, Any]:
        """Get messages created after the supplied sequence cursor."""
        query = urlencode({"after_sequence": after_sequence, "limit": 500})
        return self._request(
            "GET",
            "/api/v1/agent/chat/{}/messages/?{}".format(chat_id, query),
        )

    def submit_chat_approval(
        self,
        chat_id: int,
        approval: Dict[str, Any],
        decision: str,
        *,
        autoapproved: bool = False,
    ) -> Dict[str, Any]:
        """Approve or reject one pending headless-agent action."""
        approval_id = quote(str(approval.get("approval_id", "")), safe="")
        body: Dict[str, Any] = {
            "decision": decision,
            "type": approval.get("type", "bash_command"),
        }
        if approval.get("command"):
            body["command"] = approval["command"]
        if autoapproved:
            body["autoapproved"] = True
        return self._request(
            "POST",
            "/api/v1/agent/chat/{}/approve/{}/".format(chat_id, approval_id),
            json_body=body,
        )

    def select_chat_server(self, chat_id: int, server_id: int) -> Dict[str, Any]:
        """Select an owned server for subsequent agent execution."""
        return self._request(
            "POST",
            "/api/v1/agent/chat/{}/select-server/".format(chat_id),
            json_body={"server_id": server_id},
        )

    def select_chat_servers(
        self,
        chat_id: int,
        server_ids: List[int],
        *,
        active_server_id: Optional[int] = None,
        active_host_id: Optional[int] = None,
        selected_namespaces: Optional[Dict[Any, List[str]]] = None,
    ) -> Dict[str, Any]:
        """Replace an existing chat's complete multi-server scope."""
        deduped = list(dict.fromkeys(int(value) for value in server_ids))
        body: Dict[str, Any] = {"selected_server_ids": deduped}
        if active_server_id is None and deduped:
            active_server_id = deduped[0]
        if active_server_id is not None:
            body["active_server_id"] = active_server_id
        if active_host_id is not None:
            body["active_host_id"] = active_host_id
        if selected_namespaces is not None:
            body["selected_namespaces"] = selected_namespaces
        return self._request(
            "POST",
            "/api/v1/agent/chat/{}/select-servers/".format(chat_id),
            json_body=body,
        )

    def wait_for_chat(
        self,
        chat_id: int,
        after_sequence: int = 0,
        timeout: Optional[float] = None,
        poll_interval: float = 1,
        on_progress: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        on_status: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> ChatTurnResult:
        """Poll a headless chat until it completes, pauses, or fails.

        While the workflow is busy, persisted messages are fetched alongside
        its lightweight status. ``on_progress`` receives each newly observed
        message batch at most once, while ``on_status`` receives each current
        workflow snapshot so interactive clients can display the active plan
        step or command without exposing private model reasoning. Callback
        failures never interrupt the remote turn. The returned result retains
        all observed messages regardless of callback delivery, and
        ``latest_sequence`` covers that complete set so renderers can
        deduplicate by sequence. The timeout is an idle deadline and is
        extended by a new message batch or a real workflow-status transition.
        ``None`` disables that deadline.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        state: Dict[str, Any] = {"status": "processing", "pending_approvals": []}
        previous_status: Optional[str] = None
        latest_sequence = after_sequence
        result_messages: List[Dict[str, Any]] = []

        def fetch_new_messages(
            *,
            deliver_progress: bool,
            payload: Optional[Dict[str, Any]] = None,
        ) -> tuple[List[Dict[str, Any]], bool]:
            """Fetch, order, and record messages strictly beyond the local cursor."""
            nonlocal deadline, latest_sequence

            if not isinstance(payload, dict) or "messages" not in payload:
                payload = self.chat_messages(chat_id, after_sequence=latest_sequence)
            raw_messages = payload.get("messages", []) if isinstance(payload, dict) else []
            sequenced: List[tuple[int, Dict[str, Any]]] = []
            for message in raw_messages if isinstance(raw_messages, list) else []:
                if not isinstance(message, dict):
                    continue
                try:
                    sequence = int(message.get("sequence"))
                except (TypeError, ValueError):
                    continue
                if sequence > latest_sequence:
                    sequenced.append((sequence, message))

            batch: List[Dict[str, Any]] = []
            batch_sequences = set()
            for sequence, message in sorted(sequenced, key=lambda item: item[0]):
                if sequence in batch_sequences:
                    continue
                batch_sequences.add(sequence)
                batch.append(message)

            if batch:
                latest_sequence = max(batch_sequences)
                if timeout is not None:
                    deadline = time.monotonic() + timeout

                result_messages.extend(batch)
                if deliver_progress and on_progress is not None:
                    try:
                        on_progress(batch)
                    except Exception:
                        # Progress rendering is additive and the complete
                        # batch remains available in the final result.
                        pass

            has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
            return batch, has_more

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise PortalError(
                    "Skyportal has made no progress for {} seconds. Chat #{} remains available.".format(
                        f"{timeout:g}", chat_id
                    )
                )

            state = self.chat_status(chat_id, after_sequence=latest_sequence)
            if on_status is not None:
                try:
                    on_status(state)
                except Exception:
                    pass
            status = str(state.get("status", "unknown"))
            if (
                timeout is not None
                and previous_status is not None
                and status != previous_status
            ):
                deadline = time.monotonic() + timeout
            previous_status = status

            if status not in _BUSY_CHAT_STATUSES:
                break

            while True:
                batch, has_more = fetch_new_messages(
                    deliver_progress=True,
                    payload=state,
                )
                state = {}
                if not has_more or not batch:
                    break
            time.sleep(poll_interval)

        # Approval and error states already contain the information the caller
        # needs to act. One persisted-message fetch is useful context, but a
        # consistency-settlement delay would only make the prompt/error noisy.
        if status in _IMMEDIATE_CHAT_STATUSES:
            fetch_new_messages(deliver_progress=False, payload=state)
        else:
            # A terminal status write can win a short race with its final
            # persisted message. Settle that race with a small fixed budget
            # independent of the (possibly disabled or exhausted) idle timeout.
            settlement_delay = _TERMINAL_MESSAGE_SETTLEMENT_INITIAL_DELAY
            messages_seen_before_terminal = bool(result_messages)
            for attempt in range(_TERMINAL_MESSAGE_SETTLEMENT_ATTEMPTS):
                batch, has_more = fetch_new_messages(
                    deliver_progress=False,
                    payload=state,
                )
                state = {}
                if batch:
                    while has_more:
                        next_batch, has_more = fetch_new_messages(deliver_progress=False)
                        if not next_batch:
                            break
                    break
                # Earlier rows can be only the user's own prompt or an
                # intermediate thought/tool call. Always allow one short
                # retry after an empty terminal fetch so those rows cannot
                # suppress the final assistant-message settlement entirely.
                if messages_seen_before_terminal and attempt >= 1:
                    break
                if attempt + 1 < _TERMINAL_MESSAGE_SETTLEMENT_ATTEMPTS:
                    time.sleep(settlement_delay)
                    settlement_delay = min(
                        settlement_delay * 2,
                        _TERMINAL_MESSAGE_SETTLEMENT_MAX_DELAY,
                    )

        pending = state.get("pending_approvals", [])
        if not isinstance(pending, list):
            pending = []
        return ChatTurnResult(
            chat_id=chat_id,
            status=status,
            messages=result_messages,
            pending_approvals=[item for item in pending if isinstance(item, dict)],
            latest_sequence=latest_sequence,
        )

    def begin_chat_turn(
        self,
        message: str,
        chat_id: Optional[int] = None,
        server_id: Optional[int] = None,
        *,
        server_ids: Optional[List[int]] = None,
        active_server_id: Optional[int] = None,
        active_host_id: Optional[int] = None,
        selected_namespaces: Optional[Dict[Any, List[str]]] = None,
    ) -> int:
        """Start or continue a chat and return its ID without waiting.

        Splitting this from the wait lets an interactive caller hold the chat
        ID up front, so it can cancel the turn while the agent is still working.
        """
        if chat_id is None:
            if (
                server_ids is None
                and active_server_id is None
                and active_host_id is None
                and selected_namespaces is None
            ):
                started = self.create_chat(message, server_id=server_id)
            else:
                started = self.create_chat(
                    message,
                    server_id=server_id,
                    server_ids=server_ids,
                    active_server_id=active_server_id,
                    active_host_id=active_host_id,
                    selected_namespaces=selected_namespaces,
                )
            try:
                return int(started["chat_id"])
            except (KeyError, TypeError, ValueError) as error:
                raise PortalError("Skyportal did not return a valid chat ID") from error
        self.send_chat_message(chat_id, message)
        return chat_id

    def cancel_chat(self, chat_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
        """Ask the server to cancel the active workflow for a chat."""
        body: Dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        return self._request(
            "POST",
            "/api/v1/agent/chat/{}/cancel/".format(chat_id),
            json_body=body,
        )

    def run_chat_turn(
        self,
        message: str,
        chat_id: Optional[int] = None,
        after_sequence: int = 0,
        server_id: Optional[int] = None,
        timeout: Optional[float] = None,
        poll_interval: float = 1,
        on_progress: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        on_status: Optional[Callable[[Dict[str, Any]], None]] = None,
        *,
        server_ids: Optional[List[int]] = None,
        active_server_id: Optional[int] = None,
        active_host_id: Optional[int] = None,
        selected_namespaces: Optional[Dict[Any, List[str]]] = None,
    ) -> ChatTurnResult:
        """Start or continue a chat and wait for the resulting agent turn."""
        chat_id = self.begin_chat_turn(
            message,
            chat_id=chat_id,
            server_id=server_id,
            server_ids=server_ids,
            active_server_id=active_server_id,
            active_host_id=active_host_id,
            selected_namespaces=selected_namespaces,
        )
        return self.wait_for_chat(
            chat_id,
            after_sequence=after_sequence,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
            on_status=on_status,
        )

    @staticmethod
    def assistant_text(messages: List[Dict[str, Any]]) -> str:
        """Extract text from the newest assistant message."""
        assistants = [message for message in messages if message.get("role") == "assistant"]
        if not assistants:
            return ""
        latest = max(assistants, key=lambda message: int(message.get("sequence", 0)))
        content = latest.get("content", [])
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        )

    def _access_token(self) -> str:
        token = self._env_access_token()
        if not token:
            credentials = CredentialStore.load()
            if not credentials or not credentials.get("access_token"):
                raise PortalError("Not connected. Run 'skyportal login' first.")
            if credentials.get("base_url") not in (None, self.base_url):
                raise PortalError(
                    "Stored credentials belong to another Skyportal deployment. "
                    "Run 'skyportal login' again."
                )
            token = str(credentials["access_token"])
        self._reject_agent_token(token)
        return token

    @staticmethod
    def _env_access_token() -> Optional[str]:
        token = os.environ.get("SKYPORTAL_ACCESS_TOKEN")
        if token:
            return token
        return os.environ.get("SKYPORTAL_API_KEY")

    def _reject_agent_token(self, token: str) -> None:
        if token.startswith("agt_"):
            raise PortalError(
                "Agent deployment tokens (agt_) only upload observability data and cannot "
                "authenticate the Skyportal CLI. Create an account API key (sk_) at {}.".format(
                    self.api_key_url()
                )
            )

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
        bearer_token: Optional[str] = None,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": CLI_USER_AGENT,
        }
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        if bearer_token:
            headers["Authorization"] = "Bearer " + bearer_token
        elif authenticated:
            headers["Authorization"] = "Bearer " + self._access_token()

        request = Request(
            "{}{}".format(self.base_url, path),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = response.status
                content = response.read()
                if status >= 300:
                    raise PortalError(
                        "Skyportal request failed ({})".format(status),
                        status_code=status,
                    )
                try:
                    return json.loads(content.decode()) if content else {}
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise PortalError(
                        "Skyportal returned a non-JSON response ({})".format(status),
                        status_code=status,
                    ) from error
        except HTTPError as error:
            code = None
            try:
                payload = json.loads(error.read().decode())
                code = payload.get("code") if isinstance(payload, dict) else None
                message = (
                    payload.get("error_description")
                    or payload.get("error")
                    or payload.get("message")
                    or payload.get("detail")
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                message = None
            raise PortalError(
                str(message) if message else "Skyportal request failed ({})".format(error.code),
                status_code=error.code,
                code=str(code) if code else None,
            ) from error
        except URLError as error:
            raise PortalError("Could not connect to Skyportal: {}".format(error.reason)) from error
        except TimeoutError as error:
            raise PortalError("Could not connect to Skyportal: request timed out") from error
