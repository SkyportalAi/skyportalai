"""Current-user resource."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .._exceptions import AuthenticationError
from ..types import User

if TYPE_CHECKING:
    from .._client import Skyportal


def fetch_me(client: "Skyportal") -> User:
    """Return the user the API key belongs to, via ``GET /api/v1/auth/check/``."""
    data = client._request("GET", "/api/v1/auth/check/")
    if not data.get("authenticated"):
        raise AuthenticationError(
            "Not authenticated — the API key was rejected by the auth service.",
            status_code=200,
            body=data,
        )
    return User.from_dict(data.get("user") or {})
