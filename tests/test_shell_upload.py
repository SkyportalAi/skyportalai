"""/upload attaches logs and exports to the current chat (skyportal-website#3432)."""

from io import StringIO

import pytest
from rich.console import Console

from skyportalai.shell import portal
from skyportalai.shell.interactive import COMMANDS, InteractiveShell
from skyportalai.shell.portal import PortalError


class FakeClient:
    base_url = "https://app.skyportal.ai"

    def __init__(self):
        self.uploads = []
        self.response = {"success": True, "files": [{"name": "vllm.log", "size": 2048}]}
        self.error = None

    def is_authenticated(self):
        return True

    def upload_chat_files(self, chat_id, paths):
        if self.error:
            raise self.error
        self.uploads.append((chat_id, list(paths)))
        return self.response


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTALAI_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    out = StringIO()
    console = Console(file=out, width=160, force_terminal=False)
    client = FakeClient()
    instance = InteractiveShell(
        console=console,
        client_factory=lambda: client,
        session=object(),
        token_prompt=lambda _prompt: "",
    )
    return instance, client, out


def test_upload_sends_the_paths_to_the_active_chat(shell, tmp_path):
    instance, client, out = shell
    instance.chat_id = 42
    log = tmp_path / "vllm.log"
    log.write_text("latency_ms=118\n")

    instance._cmd_upload([str(log)])

    assert client.uploads == [(42, [str(log)])]
    assert "Attached" in out.getvalue()
    assert "vllm.log" in out.getvalue()


def test_upload_without_a_chat_explains_rather_than_failing(shell, tmp_path):
    instance, client, out = shell
    instance.chat_id = None
    log = tmp_path / "vllm.log"
    log.write_text("x\n")

    instance._cmd_upload([str(log)])

    assert client.uploads == []
    assert "Send a message first" in out.getvalue()


def test_upload_without_arguments_shows_usage(shell):
    instance, client, out = shell
    instance.chat_id = 42

    instance._cmd_upload([])

    assert client.uploads == []
    assert "/upload" in out.getvalue()


def test_upload_is_dispatchable_and_documented(shell):
    instance, _, _ = shell

    assert instance._handlers["/upload"] == instance._cmd_upload
    assert "/upload" in COMMANDS


class TestMultipartEncoding:
    def test_body_is_parseable_multipart(self):
        body, content_type = portal._encode_multipart(
            [("vllm.log", b"latency_ms=118\n", "text/plain")]
        )

        assert content_type.startswith("multipart/form-data; boundary=")
        boundary = content_type.split("boundary=", 1)[1]
        text = body.decode()
        assert text.startswith("--" + boundary)
        assert text.rstrip().endswith("--" + boundary + "--")
        assert 'name="files"; filename="vllm.log"' in text
        assert "latency_ms=118" in text

    def test_each_file_becomes_its_own_part(self):
        body, content_type = portal._encode_multipart(
            [("a.log", b"one", "text/plain"), ("b.csv", b"two", "text/csv")]
        )
        boundary = content_type.split("boundary=", 1)[1]

        assert body.decode().count("--" + boundary) == 3  # two parts + terminator
        assert 'filename="a.log"' in body.decode()
        assert 'filename="b.csv"' in body.decode()

    def test_a_filename_cannot_inject_headers(self):
        body, _ = portal._encode_multipart(
            [('evil"\r\nX-Injected: yes\r\n\r\nbad.log', b"x", "text/plain")]
        )

        assert "X-Injected: yes\r\n" not in body.decode().replace(
            'filename="evilX-Injected: yesbad.log"', ""
        )
        assert body.decode().count("Content-Disposition") == 1

    def test_boundary_is_not_reused_between_calls(self):
        _, first = portal._encode_multipart([("a", b"x", "text/plain")])
        _, second = portal._encode_multipart([("a", b"x", "text/plain")])

        assert first != second


class TestUploadClient:
    def _client(self, tmp_path, monkeypatch):
        client = portal.SkyportalClient.__new__(portal.SkyportalClient)
        client.base_url = "https://example.invalid"
        sent = {}

        def fake_request(method, path, **kwargs):
            sent["method"] = method
            sent["path"] = path
            sent.update(kwargs)
            return {"success": True, "files": []}

        client._request = fake_request
        return client, sent

    def test_missing_file_is_refused_before_any_request(self, tmp_path, monkeypatch):
        client, sent = self._client(tmp_path, monkeypatch)

        with pytest.raises(PortalError, match="No such file"):
            client.upload_chat_files(1, [str(tmp_path / "absent.log")])

        assert sent == {}

    def test_empty_file_is_refused(self, tmp_path, monkeypatch):
        client, sent = self._client(tmp_path, monkeypatch)
        empty = tmp_path / "empty.log"
        empty.write_bytes(b"")

        with pytest.raises(PortalError, match="empty"):
            client.upload_chat_files(1, [str(empty)])

        assert sent == {}

    def test_oversized_file_is_refused_without_reading_it(self, tmp_path, monkeypatch):
        client, sent = self._client(tmp_path, monkeypatch)
        big = tmp_path / "big.log"
        big.write_bytes(b"x" * (portal.MAX_UPLOAD_BYTES + 1))

        with pytest.raises(PortalError, match="limit is 10 MB"):
            client.upload_chat_files(1, [str(big)])

        assert sent == {}

    def test_a_good_file_posts_multipart_to_the_upload_route(self, tmp_path, monkeypatch):
        client, sent = self._client(tmp_path, monkeypatch)
        log = tmp_path / "vllm.log"
        log.write_text("latency_ms=118\n")

        client.upload_chat_files(7, [str(log)])

        assert sent["method"] == "POST"
        assert sent["path"] == "/api/v1/agent/chat/7/upload/"
        assert sent["content_type"].startswith("multipart/form-data")
        assert b"latency_ms=118" in sent["body"]
