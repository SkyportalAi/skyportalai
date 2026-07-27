"""skyportalai — the official Python SDK for the SkyPortal API."""
from ._client import Skyportal
from ._exceptions import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AuthenticationError,
    SkyportalError,
    WaitTimeoutError,
)
from ._version import __version__
from .chat import Chat
from .types import (
    ApprovalResult,
    ChatStatus,
    KubernetesCluster,
    Message,
    MessagesPage,
    PendingApproval,
    PermissionMode,
    User,
)

__all__ = [
    "Skyportal",
    "User",
    "Chat",
    "ChatStatus",
    "KubernetesCluster",
    "PendingApproval",
    "PermissionMode",
    "ApprovalResult",
    "Message",
    "MessagesPage",
    "SkyportalError",
    "APIConnectionError",
    "APIStatusError",
    "AuthenticationError",
    "APIError",
    "WaitTimeoutError",
    "__version__",
]
