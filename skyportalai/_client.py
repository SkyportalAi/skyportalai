"""The SkyPortal API client."""
from __future__ import annotations

import os
import time
import warnings
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from ._exceptions import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    SkyportalError,
)
from ._version import __version__
from .resources.ansible import AnsibleResource
from .resources.chat import ChatResource
from .resources.kubernetes import KubernetesResource
from .resources.me import fetch_me
from .types import PermissionMode, User

DEFAULT_BASE_URL = "https://app.skyportal.ai"

#: Hosts allowed to use plain ``http://`` (local development only).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PERMISSION_MODES = frozenset({"ask", "autoapprove"})


def _redacted(base_url: str) -> str:
    """Strip userinfo (``user:pass@``) so credentials never reach logs."""
    parts = urlsplit(base_url)
    # A scheme-less input like ``user:pass@host:8000`` lands entirely in .path,
    # so urlsplit never fills in username/password. Re-parse it behind a "//"
    # marker so the authority (and any userinfo) is recognised and stripped.
    schemeless = (
        parts.username is None
        and parts.password is None
        and not parts.netloc
        and "@" in base_url
    )
    if schemeless:
        parts = urlsplit("//" + base_url)
    if parts.username is None and parts.password is None:
        return base_url
    netloc = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        netloc = f"{netloc}:{port}"
    shown = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return shown.removeprefix("//") if schemeless else shown


def _validate_base_url(base_url: str) -> None:
    """Validate an API root before a Bearer credential can be sent to it.

    The Bearer key travels in a header on every request, so a non-HTTPS
    ``base_url`` (a typo, a stale env var, a poisoned CI secret) would leak a
    live credential to whatever host it names. ``http://`` stays allowed for
    loopback hosts so local development against a dev server keeps working.
    Messages show a redacted URL so embedded userinfo cannot leak either.
    """
    try:
        parts = urlsplit(base_url)
        host = (parts.hostname or "").lower()
        # Accessing ``port`` validates malformed/non-numeric port values.
        _ = parts.port
    except ValueError as exc:
        raise SkyportalError("Invalid base_url: expected a well-formed http(s) URL.") from exc

    shown = _redacted(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or not host:
        raise SkyportalError(
            f"Invalid base_url {shown!r}: expected an absolute http:// or https:// URL."
        )
    if parts.username is not None or parts.password is not None:
        raise SkyportalError(
            f"Refusing base_url {shown!r}: embedded URL credentials are not supported."
        )
    if parts.query or parts.fragment:
        raise SkyportalError(
            f"Invalid base_url {shown!r}: query strings and fragments are not allowed."
        )

    loopback = host in _LOOPBACK_HOSTS or host.endswith(".localhost")
    if parts.scheme != "https" and not loopback:
        if os.environ.get("SKYPORTAL_ALLOW_INSECURE") == "1":
            warnings.warn(
                f"SKYPORTAL_ALLOW_INSECURE=1: sending the API key over "
                f"non-HTTPS base URL {shown}",
                stacklevel=3,
            )
        else:
            raise SkyportalError(
                f"Refusing non-HTTPS base_url {shown!r}: the API key would be "
                "sent in cleartext. Use an https:// URL (plain http:// is "
                "allowed for loopback hosts, or set SKYPORTAL_ALLOW_INSECURE=1 "
                "to override for a trusted internal setup)."
            )


def _check_base_url(base_url: str) -> None:
    """Validate a base URL before creating the client."""
    _validate_base_url(base_url)


class Skyportal:
    """Synchronous SkyPortal API client.

    Args:
        api_key: Bearer credential. Falls back to ``SKYPORTAL_API_KEY``.
        base_url: API root. Falls back to ``SKYPORTAL_BASE_URL`` then
            ``DEFAULT_BASE_URL``. A trailing slash is stripped.
        timeout: per-request timeout in seconds.
        max_retries: retry budget for idempotent (GET) requests on network
            failure / 5xx.
        session: an optional ``requests.Session`` to reuse (else one is created).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        session: requests.Session | None = None,
    ):
        api_key = api_key or os.environ.get("SKYPORTAL_API_KEY")
        if not api_key:
            raise SkyportalError(
                "No API key provided. Pass api_key=... or set the "
                "SKYPORTAL_API_KEY environment variable."
            )
        self.api_key = api_key

        if timeout <= 0:
            raise ValueError(f"timeout must be greater than zero, got {timeout}")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")

        base_url = base_url or os.environ.get("SKYPORTAL_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = base_url.rstrip("/")
        _check_base_url(self.base_url)

        self.timeout = timeout
        self.max_retries = max_retries
        self._owns_session = session is None
        self._session = session or requests.Session()
        self._backoff_base = 0.5

        self.chat = ChatResource(self)
        self.ansible = AnsibleResource(self)
        self.kubernetes = KubernetesResource(self)

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: object = None) -> Any:
        """Perform an HTTP request and return the parsed JSON body.

        Retries idempotent (GET) requests on connection errors and 5xx, bounded
        by ``max_retries`` with exponential backoff. Maps every failure to a
        ``skyportalai`` exception.
        """
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise SkyportalError(f"Invalid API path {path!r}: expected a root-relative path.")
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": f"skyportalai-python/{__version__}",
            "Accept": "application/json",
        }
        retryable = method.upper() == "GET"
        attempt = 0
        while True:
            try:
                response = self._session.request(
                    method, url, headers=headers, params=params, json=json,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if retryable and attempt < self.max_retries:
                    time.sleep(self._backoff_base * (2 ** attempt))
                    attempt += 1
                    continue
                raise APIConnectionError(f"Could not reach {url}: {exc}") from exc

            if response.status_code >= 500 and retryable and attempt < self.max_retries:
                response.close()
                time.sleep(self._backoff_base * (2 ** attempt))
                attempt += 1
                continue

            return self._handle_response(response)

    @staticmethod
    def _handle_response(response: requests.Response) -> Any:
        if 200 <= response.status_code < 300:
            if response.status_code in (204, 205):
                response.close()
                return {}
            if not response.content:
                raise APIError(
                    "API returned an empty response body.",
                    status_code=response.status_code,
                    body=response.text,
                )
            try:
                return response.json()
            except ValueError as exc:
                raise APIError(
                    "API returned a non-JSON response.",
                    status_code=response.status_code,
                    body=response.text,
                ) from exc

        body = _safe_body(response)
        if response.status_code in (401, 403):
            raise AuthenticationError(
                "Authentication failed — the API key was missing or rejected.",
                status_code=response.status_code,
                body=body,
            )
        raise APIError(
            f"API request failed with status {response.status_code}.",
            status_code=response.status_code,
            body=body,
        )

    def me(self) -> User:
        """Return the authenticated user (the owner of the API key)."""
        return fetch_me(self)

    def get_permission_mode(self) -> PermissionMode:
        """Return the account's shared agent-approval mode.

        ``ask`` leaves each gated action for an explicit decision;
        ``autoapprove`` lets supported clients submit those concrete approvals
        automatically. Server-side safety and environment policies still
        apply in either mode.
        """
        data = self._request("GET", "/api/v1/agent/permission/")
        return self._parse_permission_mode(data)

    def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        """Persist the shared agent-approval mode for this account."""
        if mode not in _PERMISSION_MODES:
            raise ValueError("permission mode must be 'ask' or 'autoapprove'")
        data = self._request(
            "PUT",
            "/api/v1/agent/permission/",
            json={"permission_mode": mode},
        )
        return self._parse_permission_mode(data)

    @staticmethod
    def _parse_permission_mode(data: object) -> PermissionMode:
        mode = data.get("permission_mode") if isinstance(data, dict) else None
        if mode not in _PERMISSION_MODES:
            raise APIError(
                "API returned an invalid permission mode.",
                body=data,
            )
        return mode

    def close(self) -> None:
        """Close the internally-created HTTP session.

        A session supplied by the caller remains owned by the caller and is not
        closed here.
        """
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> Skyportal:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def _safe_body(response: requests.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text
