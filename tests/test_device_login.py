"""The browser handshake behind `skyportalai login` (#3404).

`login` used to open the key page and block on a paste the browser could never
answer. It now starts an authorization, shows a short code and polls, so the
terminal finishes on its own — and still falls back to the paste when the
deployment has no handshake endpoint.
"""

import json
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
from typer.testing import CliRunner

from skyportalai.cli.main import app
from skyportalai.shell.portal import CredentialStore, DeviceLogin, PortalError, SkyportalClient

runner = CliRunner()

STARTED = {
    "device_code": "device-secret",
    "user_code": "BCDF-GHJK",
    "verification_uri": "https://app.skyportal.ai/cli/authorize/",
    "verification_uri_complete": "https://app.skyportal.ai/cli/authorize/?code=BCDF-GHJK",
    "interval": 5,
    "expires_in": 600,
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


class _Clock:
    """A monotonic clock the injected sleep advances, so deadlines are testable."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _http_error(status):
    return HTTPError(
        "https://app.skyportal.ai/api/v1/cli/login/start/",
        status,
        "error",
        hdrs=None,
        fp=BytesIO(b"{}"),
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("SKYPORTALAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    for name in ("SKYPORTALAI_API_KEY", "SKYPORTALAI_ACCESS_TOKEN", "SKYPORTALAI_BASE_URL", "SKYPORTALAI_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "300")
    return tmp_path / "credentials.json"


def test_begin_device_login_reads_the_handshake(isolated):
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse(STARTED)) as call:
        handshake = client.begin_device_login()

    assert handshake.user_code == "BCDF-GHJK"
    assert handshake.verification_uri_complete.endswith("?code=BCDF-GHJK")
    assert handshake.interval == 5
    request = call.call_args[0][0]
    assert request.full_url == "https://app.skyportal.ai/api/v1/cli/login/start/"
    # Unauthenticated by design: this is what a CLI with no credential calls.
    assert "Authorization" not in request.headers
    body = json.loads(request.data.decode())
    assert body["client_version"]


@pytest.mark.parametrize("status", [403, 404, 405])
def test_a_deployment_without_the_endpoint_reports_no_handshake(isolated, status):
    # 404/405 predates the handshake; 403 is an edge or WAF in front of one.
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportalai.shell.portal.urlopen", side_effect=_http_error(status)):
        assert client.begin_device_login() is None


def test_other_failures_are_not_mistaken_for_a_missing_endpoint(isolated):
    client = SkyportalClient("https://app.skyportal.ai")

    with patch("skyportalai.shell.portal.urlopen", side_effect=_http_error(500)):
        with pytest.raises(PortalError):
            client.begin_device_login()


def test_a_transient_poll_failure_costs_a_retry_not_the_login(isolated):
    # The user may already have clicked approve; a 502 at second 30 of 600 must
    # not throw that away.
    client = SkyportalClient("https://app.skyportal.ai")
    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse(STARTED)):
        handshake = client.begin_device_login()
    slept = []
    responses = [
        _http_error(502),
        URLError("connection reset"),
        _http_error(429),
        FakeResponse({"status": "approved", "key": "sk_delivered"}),
    ]

    with patch("skyportalai.shell.portal.urlopen", side_effect=responses):
        key = client.await_device_login(handshake, sleep=slept.append)

    assert key == "sk_delivered"
    # Backs off while the far side is unhappy, rather than hammering it.
    assert slept == [5, 10, 20]


def test_a_failure_about_the_request_itself_still_stops(isolated):
    client = SkyportalClient("https://app.skyportal.ai")
    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse(STARTED)):
        handshake = client.begin_device_login()

    with patch("skyportalai.shell.portal.urlopen", side_effect=_http_error(400)):
        with pytest.raises(PortalError):
            client.await_device_login(handshake, sleep=lambda _seconds: None)


def test_transient_failures_stop_at_the_deadline(isolated):
    client = SkyportalClient("https://app.skyportal.ai")
    handshake = DeviceLogin(**{**STARTED, "interval": 1, "expires_in": 4})
    clock = _Clock()

    with patch("skyportalai.shell.portal.time.monotonic", clock.monotonic):
        with patch("skyportalai.shell.portal.urlopen", side_effect=_http_error(503)):
            with pytest.raises(PortalError):
                client.await_device_login(handshake, sleep=clock.sleep)

    assert clock.now >= 4


def test_polling_waits_through_pending_and_returns_the_key(isolated):
    client = SkyportalClient("https://app.skyportal.ai")
    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse(STARTED)):
        handshake = client.begin_device_login()
    slept = []
    responses = [
        FakeResponse({"status": "pending", "interval": 1}),
        FakeResponse({"status": "pending", "interval": 1}),
        FakeResponse({"status": "approved", "key": "sk_delivered"}),
    ]

    with patch("skyportalai.shell.portal.urlopen", side_effect=responses):
        key = client.await_device_login(handshake, sleep=slept.append)

    assert key == "sk_delivered"
    assert slept == [1, 1]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("denied", "denied in the browser"),
        ("expired", "expired before it was approved"),
    ],
)
def test_a_refused_handshake_says_so_instead_of_waiting(isolated, status, expected):
    client = SkyportalClient("https://app.skyportal.ai")
    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse(STARTED)):
        handshake = client.begin_device_login()

    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse({"status": status})):
        with pytest.raises(PortalError, match=expected):
            client.await_device_login(handshake, sleep=lambda _seconds: None)


def test_an_approval_without_a_key_is_an_error_not_an_empty_credential(isolated):
    client = SkyportalClient("https://app.skyportal.ai")
    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse(STARTED)):
        handshake = client.begin_device_login()

    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse({"status": "approved"})):
        with pytest.raises(PortalError, match="no API key"):
            client.await_device_login(handshake, sleep=lambda _seconds: None)


def test_login_completes_without_a_prompt_once_the_browser_approves(isolated):
    responses = [
        FakeResponse(STARTED),
        FakeResponse({"status": "approved", "key": "sk_delivered"}),
        FakeResponse([]),  # the credential is validated before it is saved
    ]

    with patch("skyportalai.shell.portal.urlopen", side_effect=responses):
        with patch("skyportalai.shell.portal.webbrowser.open", return_value=True) as browser:
            result = runner.invoke(app, ["login"], input="")

    assert result.exit_code == 0, result.output
    assert "BCDF-GHJK" in result.output
    assert "Skyportal API key" not in result.output
    browser.assert_called_once_with(STARTED["verification_uri_complete"])
    assert json.loads(isolated.read_text())["access_token"] == "sk_delivered"


def test_no_browser_prints_the_url_without_opening_it(isolated):
    responses = [
        FakeResponse(STARTED),
        FakeResponse({"status": "approved", "key": "sk_delivered"}),
        FakeResponse([]),
    ]

    with patch("skyportalai.shell.portal.urlopen", side_effect=responses):
        with patch("skyportalai.shell.portal.webbrowser.open", return_value=True) as browser:
            result = runner.invoke(app, ["login", "--no-browser"], input="")

    assert result.exit_code == 0, result.output
    assert STARTED["verification_uri_complete"] in result.output
    browser.assert_not_called()


def test_a_start_failure_names_the_paste_route(isolated):
    with patch("skyportalai.shell.portal.urlopen", side_effect=_http_error(502)):
        result = runner.invoke(app, ["login"])

    assert result.exit_code == 1
    assert "skyportalai login --token" in result.output
    assert not isolated.exists()


def test_login_falls_back_to_pasting_when_the_deployment_has_no_handshake(isolated):
    def responses(request, **kwargs):
        if request.full_url.endswith("/api/v1/cli/login/start/"):
            raise _http_error(404)
        return FakeResponse([])

    with patch("skyportalai.shell.portal.urlopen", side_effect=responses):
        with patch("skyportalai.shell.portal.webbrowser.open", return_value=True):
            result = runner.invoke(app, ["login"], input="sk_pasted\n")

    assert result.exit_code == 0, result.output
    assert "/keys/?source=cli" in result.output
    assert json.loads(isolated.read_text())["access_token"] == "sk_pasted"


def test_token_flag_skips_the_handshake_entirely(isolated):
    with patch("skyportalai.shell.portal.urlopen", return_value=FakeResponse([])) as call:
        result = runner.invoke(app, ["login", "--token"], input="sk_pasted\n")

    assert result.exit_code == 0, result.output
    assert "cli/authorize" not in result.output
    # One call only: the credential validation. No handshake was started.
    assert len(call.call_args_list) == 1
    assert json.loads(isolated.read_text())["access_token"] == "sk_pasted"


def test_a_cancelled_wait_points_at_the_manual_route(isolated):
    responses = [FakeResponse(STARTED)]

    with patch("skyportalai.shell.portal.urlopen", side_effect=responses):
        with patch("skyportalai.shell.portal.webbrowser.open", return_value=True):
            with patch(
                "skyportalai.shell.portal.SkyportalClient.await_device_login",
                side_effect=KeyboardInterrupt,
            ):
                result = runner.invoke(app, ["login"])

    assert result.exit_code == 1
    assert "skyportalai login --token" in result.output
    assert not isolated.exists()
    assert CredentialStore.load() is None


class _HandshakeClient:
    """Just enough client for the interactive shell's connect paths."""

    base_url = "https://app.skyportal.ai"

    def __init__(self, handshake):
        self._handshake = handshake
        self.saved = []
        self.paste_page_opened = 0
        self.opened = []

    def is_authenticated(self):
        return False

    def begin_device_login(self):
        return self._handshake

    def open_verification_page(self, url):
        self.opened.append(url)
        return True

    def await_device_login(self, handshake, sleep=None):
        return "sk_from_browser"

    def login(self, open_browser=True):
        self.paste_page_opened += 1
        return {"verification_url": "https://app.skyportal.ai/keys/?source=cli", "browser_opened": True}

    def set_access_token(self, token, validate=True):
        self.saved.append(token)


def _shell(monkeypatch, tmp_path, handshake):
    from io import StringIO

    from rich.console import Console

    from skyportalai.shell.interactive import InteractiveShell

    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    out = StringIO()
    client = _HandshakeClient(handshake)
    shell = InteractiveShell(console=Console(file=out, width=200, force_terminal=False), client_factory=lambda: client)
    return shell, client, out


def test_the_shell_login_command_uses_the_browser_handshake(monkeypatch, tmp_path):
    from skyportalai.shell.portal import DeviceLogin

    handshake = DeviceLogin(**STARTED)
    shell, client, out = _shell(monkeypatch, tmp_path, handshake)

    shell._cmd_login([])

    assert client.saved == ["sk_from_browser"]
    assert client.opened == [STARTED["verification_uri_complete"]]
    assert client.paste_page_opened == 0
    assert "BCDF-GHJK" in out.getvalue()


def test_the_shell_falls_back_to_the_key_page_without_a_handshake(monkeypatch, tmp_path):
    shell, client, out = _shell(monkeypatch, tmp_path, None)
    shell._token_prompt = lambda _prompt: "sk_pasted"

    shell._cmd_login([])

    assert client.paste_page_opened == 1
    assert client.saved == ["sk_pasted"]


def test_the_shell_token_command_still_pastes(monkeypatch, tmp_path):
    from skyportalai.shell.portal import DeviceLogin

    shell, client, out = _shell(monkeypatch, tmp_path, DeviceLogin(**STARTED))
    shell._token_prompt = lambda _prompt: "sk_pasted"

    shell._cmd_token([])

    assert client.paste_page_opened == 1
    assert client.saved == ["sk_pasted"]
    assert client.opened == []
