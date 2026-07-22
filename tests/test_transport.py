import pytest
import requests

from skyportalai._client import Skyportal
from skyportalai._exceptions import APIConnectionError, APIError, AuthenticationError


def _client():
    c = Skyportal(api_key="sk-test", base_url="https://api.test", max_retries=2)
    c._backoff_base = 0.0  # no real sleeping during retry tests
    return c


def test_get_success_returns_json(requests_mock):
    requests_mock.get("https://api.test/ping", json={"ok": True}, status_code=200)
    assert _client()._request("GET", "/ping") == {"ok": True}


def test_sends_bearer_and_user_agent(requests_mock):
    requests_mock.get("https://api.test/ping", json={})
    _client()._request("GET", "/ping")
    sent = requests_mock.last_request
    assert sent.headers["Authorization"] == "Bearer sk-test"
    assert sent.headers["User-Agent"].startswith("skyportalai-python/")
    assert sent.headers["Accept"] == "application/json"


def test_401_raises_authentication_error(requests_mock):
    requests_mock.get("https://api.test/ping", status_code=401, json={"detail": "no"})
    with pytest.raises(AuthenticationError) as exc:
        _client()._request("GET", "/ping")
    assert exc.value.status_code == 401
    assert exc.value.body == {"detail": "no"}


def test_403_raises_authentication_error(requests_mock):
    requests_mock.get("https://api.test/ping", status_code=403, json={})
    with pytest.raises(AuthenticationError):
        _client()._request("GET", "/ping")


def test_404_raises_api_error(requests_mock):
    requests_mock.get("https://api.test/ping", status_code=404, json={"detail": "gone"})
    with pytest.raises(APIError) as exc:
        _client()._request("GET", "/ping")
    assert exc.value.status_code == 404


def test_200_non_json_raises_api_error(requests_mock):
    requests_mock.get("https://api.test/ping", status_code=200, text="not json")
    with pytest.raises(APIError) as exc:
        _client()._request("GET", "/ping")
    assert exc.value.status_code == 200
    assert exc.value.body == "not json"


def test_5xx_retried_then_raises(requests_mock):
    requests_mock.get("https://api.test/ping", status_code=503, json={})
    with pytest.raises(APIError) as exc:
        _client()._request("GET", "/ping")
    assert exc.value.status_code == 503
    assert requests_mock.call_count == 3


def test_5xx_then_success_recovers(requests_mock):
    requests_mock.get(
        "https://api.test/ping",
        [
            {"status_code": 500, "json": {}},
            {"status_code": 200, "json": {"ok": 1}},
        ],
    )
    assert _client()._request("GET", "/ping") == {"ok": 1}
    assert requests_mock.call_count == 2


def test_5xx_closes_response_before_retry(requests_mock, monkeypatch):
    closed = []
    real_close = requests.Response.close
    monkeypatch.setattr(
        requests.Response,
        "close",
        lambda self: (closed.append(self.status_code), real_close(self))[1],
    )
    requests_mock.get(
        "https://api.test/ping",
        [
            {"status_code": 503, "json": {}},
            {"status_code": 200, "json": {"ok": 1}},
        ],
    )
    assert _client()._request("GET", "/ping") == {"ok": 1}
    # the retried 5xx response is closed before sleeping/continuing
    assert 503 in closed


def test_connection_error_raises_api_connection_error(requests_mock):
    requests_mock.get("https://api.test/ping", exc=requests.ConnectionError("boom"))
    with pytest.raises(APIConnectionError):
        _client()._request("GET", "/ping")
    assert requests_mock.call_count == 3


def test_non_get_not_retried_on_5xx(requests_mock):
    requests_mock.post("https://api.test/ping", status_code=502, json={})
    with pytest.raises(APIError):
        _client()._request("POST", "/ping")
    assert requests_mock.call_count == 1
