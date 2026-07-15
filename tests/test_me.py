import pytest

from skyportalai._client import Skyportal
from skyportalai._exceptions import AuthenticationError
from skyportalai.types import User


def _client():
    c = Skyportal(api_key="sk-test", base_url="https://api.test")
    c._backoff_base = 0.0
    return c


def test_me_returns_user(requests_mock):
    requests_mock.get(
        "https://api.test/api/v1/auth/check/",
        json={"authenticated": True, "user": {"name": "Henrique"}},
    )
    user = _client().me()
    assert isinstance(user, User)
    assert user.name == "Henrique"
    assert user.raw == {"name": "Henrique"}


def test_me_hits_auth_check_path(requests_mock):
    requests_mock.get(
        "https://api.test/api/v1/auth/check/",
        json={"authenticated": True, "user": {"name": "x"}},
    )
    _client().me()
    assert requests_mock.last_request.path == "/api/v1/auth/check/"
    assert requests_mock.last_request.headers["Authorization"] == "Bearer sk-test"


def test_me_unauthenticated_body_raises(requests_mock):
    requests_mock.get(
        "https://api.test/api/v1/auth/check/",
        json={"authenticated": False, "detail": "key revoked"},
        status_code=200,
    )
    with pytest.raises(AuthenticationError) as exc:
        _client().me()
    assert exc.value.body == {"authenticated": False, "detail": "key revoked"}


def test_me_401_raises(requests_mock):
    requests_mock.get("https://api.test/api/v1/auth/check/", status_code=401, json={})
    with pytest.raises(AuthenticationError):
        _client().me()


def test_me_missing_user_key_yields_empty_name(requests_mock):
    requests_mock.get(
        "https://api.test/api/v1/auth/check/",
        json={"authenticated": True},
    )
    user = _client().me()
    assert user.name == ""
