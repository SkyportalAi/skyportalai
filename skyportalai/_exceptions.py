"""Exception tree for the SkyPortal SDK.

Callers only ever see ``skyportalai`` exceptions — a raw ``requests`` exception
never escapes the client.
"""
from __future__ import annotations


class SkyportalError(Exception):
    """Base for every SDK error, including client configuration errors."""


class APIConnectionError(SkyportalError):
    """The API could not be reached (network failure or timeout)."""


class WaitTimeoutError(SkyportalError):
    """``chat.wait()`` gave up before the workflow left a busy state."""


class APIStatusError(SkyportalError):
    """The API returned a non-2xx response (or an unauthenticated result)."""

    def __init__(self, message: str, *, status_code: int | None = None, body: object = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AuthenticationError(APIStatusError):
    """Missing or rejected credentials — HTTP 401/403, or ``authenticated: false``."""


class APIError(APIStatusError):
    """Any other non-2xx response; carries ``status_code`` and ``body``."""
