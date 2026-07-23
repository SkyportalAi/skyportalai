import pytest
import requests

from skyportalai._client import DEFAULT_BASE_URL, Skyportal
from skyportalai._exceptions import APIError, SkyportalError


def test_api_key_from_argument():
    client = Skyportal(api_key="sk-arg")
    assert client.api_key == "sk-arg"


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk-env")
    client = Skyportal()
    assert client.api_key == "sk-env"


def test_argument_beats_env(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_API_KEY", "sk-env")
    client = Skyportal(api_key="sk-arg")
    assert client.api_key == "sk-arg"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("SKYPORTAL_API_KEY", raising=False)
    with pytest.raises(SkyportalError):
        Skyportal()


def test_default_base_url(monkeypatch):
    monkeypatch.delenv("SKYPORTAL_BASE_URL", raising=False)
    client = Skyportal(api_key="sk-x")
    assert DEFAULT_BASE_URL == "https://app.skyportal.ai"
    assert client.base_url == DEFAULT_BASE_URL


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_BASE_URL", "http://localhost:8000/")
    client = Skyportal(api_key="sk-x")
    assert client.base_url == "http://localhost:8000"


def test_base_url_argument_trailing_slash_stripped():
    client = Skyportal(api_key="sk-x", base_url="https://api.example.com/")
    assert client.base_url == "https://api.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example.com",
        "https://",
        "https://api.example.com:invalid",
        "https://api.example.com?target=other",
        "https://api.example.com#fragment",
    ],
)
def test_malformed_base_urls_raise(url):
    with pytest.raises(SkyportalError, match="base_url"):
        Skyportal(api_key="sk-x", base_url=url)


def test_embedded_url_credentials_are_rejected_without_leaking_them():
    with pytest.raises(SkyportalError) as caught:
        Skyportal(api_key="sk-x", base_url="https://alice:hunter2@api.example.com")
    assert "embedded URL credentials" in str(caught.value)
    assert "alice" not in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_defaults_and_session():
    client = Skyportal(api_key="sk-x")
    assert client.timeout == 30.0
    assert client.max_retries == 2
    assert isinstance(client._session, requests.Session)


def test_injected_session_is_used():
    session = requests.Session()
    client = Skyportal(api_key="sk-x", session=session)
    assert client._session is session


def test_context_manager_closes_only_an_owned_session(monkeypatch):
    closed = []
    owned = Skyportal(api_key="sk-x")
    monkeypatch.setattr(owned._session, "close", lambda: closed.append("owned"))
    with owned as active:
        assert active is owned
    assert closed == ["owned"]

    injected = requests.Session()
    monkeypatch.setattr(injected, "close", lambda: closed.append("injected"))
    with Skyportal(api_key="sk-x", session=injected):
        pass
    assert closed == ["owned"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout": 0}, "timeout"),
        ({"max_retries": -1}, "max_retries"),
    ],
)
def test_invalid_transport_settings_raise(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Skyportal(api_key="sk-x", **kwargs)


def test_http_non_loopback_base_url_raises():
    with pytest.raises(SkyportalError, match="non-HTTPS"):
        Skyportal(api_key="sk-x", base_url="http://api.example.com")


def test_http_non_loopback_env_base_url_raises(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_BASE_URL", "http://internal.corp:8000")
    with pytest.raises(SkyportalError, match="non-HTTPS"):
        Skyportal(api_key="sk-x")


def test_schemeless_base_url_raises():
    with pytest.raises(SkyportalError, match="base_url"):
        Skyportal(api_key="sk-x", base_url="api.example.com")


def test_http_loopback_hosts_allowed():
    for url in ("http://localhost:8000", "http://127.0.0.1:9100", "http://app.localhost"):
        client = Skyportal(api_key="sk-x", base_url=url)
        assert client.base_url == url


def test_non_default_https_base_url_does_not_warn(recwarn):
    Skyportal(api_key="sk-x", base_url="https://staging.skyportal.ai")
    assert not recwarn


def test_default_base_url_does_not_warn(monkeypatch, recwarn):
    monkeypatch.delenv("SKYPORTAL_BASE_URL", raising=False)
    Skyportal(api_key="sk-x")
    assert not [w for w in recwarn if "base URL" in str(w.message)]


def test_rejection_message_redacts_userinfo():
    with pytest.raises(SkyportalError) as exc:
        Skyportal(api_key="sk-x", base_url="http://alice:hunter2@internal.corp:8000")
    assert "hunter2" not in str(exc.value)
    assert "alice" not in str(exc.value)
    assert "internal.corp:8000" in str(exc.value)


def test_rejection_message_redacts_schemeless_userinfo():
    with pytest.raises(SkyportalError) as exc:
        Skyportal(api_key="sk-x", base_url="alice:hunter2@internal.corp:8000")
    assert "hunter2" not in str(exc.value)
    assert "alice" not in str(exc.value)
    assert "internal.corp:8000" in str(exc.value)


def test_zero_host_rejected():
    with pytest.raises(SkyportalError, match="non-HTTPS"):
        Skyportal(api_key="sk-x", base_url="http://0.0.0.0:8000")


def test_allow_insecure_escape_hatch(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_ALLOW_INSECURE", "1")
    with pytest.warns(UserWarning, match="SKYPORTAL_ALLOW_INSECURE"):
        client = Skyportal(api_key="sk-x", base_url="http://internal.corp:8000")
    assert client.base_url == "http://internal.corp:8000"


def test_rejection_mentions_escape_hatch(monkeypatch):
    monkeypatch.delenv("SKYPORTAL_ALLOW_INSECURE", raising=False)
    with pytest.raises(SkyportalError, match="SKYPORTAL_ALLOW_INSECURE"):
        Skyportal(api_key="sk-x", base_url="http://internal.corp:8000")


def test_allow_insecure_env_overrides_with_warning(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_ALLOW_INSECURE", "1")
    with pytest.warns(UserWarning, match="SKYPORTAL_ALLOW_INSECURE"):
        client = Skyportal(api_key="sk-x", base_url="http://internal.corp:8000")
    assert client.base_url == "http://internal.corp:8000"


def test_allow_insecure_other_values_still_raise(monkeypatch):
    monkeypatch.setenv("SKYPORTAL_ALLOW_INSECURE", "true")
    with pytest.raises(SkyportalError, match="non-HTTPS"):
        Skyportal(api_key="sk-x", base_url="http://internal.corp:8000")


def test_permission_mode_get_and_put_use_shared_account_endpoint(requests_mock):
    endpoint = "https://api.test/api/v1/agent/permission/"
    requests_mock.get(endpoint, json={"permission_mode": "ask", "read_only_mode": False})
    requests_mock.put(
        endpoint,
        json={"permission_mode": "autoapprove", "read_only_mode": False},
    )
    client = Skyportal(api_key="sk-test", base_url="https://api.test")

    assert client.get_permission_mode() == "ask"
    assert client.set_permission_mode("autoapprove") == "autoapprove"
    assert requests_mock.last_request.json() == {"permission_mode": "autoapprove"}


def test_permission_mode_rejects_invalid_input_before_request(requests_mock):
    client = Skyportal(api_key="sk-test", base_url="https://api.test")

    with pytest.raises(ValueError, match="ask.*autoapprove"):
        client.set_permission_mode("everything")

    assert not requests_mock.called


def test_permission_mode_rejects_malformed_server_response(requests_mock):
    requests_mock.get(
        "https://api.test/api/v1/agent/permission/",
        json={"permission_mode": "unexpected"},
    )

    with pytest.raises(APIError, match="invalid permission mode"):
        Skyportal(api_key="sk-test", base_url="https://api.test").get_permission_mode()
