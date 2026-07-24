import skyportalai
from skyportal import __version__ as cli_version


def test_public_exports_present():
    for name in (
        "Skyportal",
        "User",
        "Chat",
        "ChatStatus",
        "AnsiblePlaybook",
        "AnsibleDeployment",
        "KubernetesCluster",
        "PendingApproval",
        "ApprovalResult",
        "Message",
        "MessagesPage",
        "SkyportalError",
        "APIConnectionError",
        "APIStatusError",
        "AuthenticationError",
        "APIError",
        "WaitTimeoutError",
    ):
        assert hasattr(skyportalai, name), f"missing export: {name}"


def test_top_level_import_works():
    from skyportalai import Skyportal, User  # noqa: F401

    client = Skyportal(api_key="sk-x")
    assert client.api_key == "sk-x"


def test_dunder_version_exported():
    assert isinstance(skyportalai.__version__, str)
    assert cli_version == skyportalai.__version__
