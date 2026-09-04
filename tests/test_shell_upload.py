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

    @pytest.mark.parametrize(
        "filename, content",
        [
            ("metrics.csv", "timestamp,requests,p95_ms\n2026-09-02T12:00:00Z,3410,118\n"),
            ("vllm.log", "2026-09-02 12:00:01 INFO throughput 42.1 tokens/s\n"),
            ("notes.txt", "Restarted the pod at 12:05.\n"),
            # Real logs are often bare names. mimetypes guesses octet-stream for
            # these; the server decides by decoding, not by the declared type, so
            # this must keep being sent rather than refused here.
            ("syslog", "Sep  2 12:00:01 host kernel: oom-killer invoked\n"),
        ],
    )
    def test_the_formats_people_actually_upload(self, tmp_path, monkeypatch, filename, content):
        client, sent = self._client(tmp_path, monkeypatch)
        target = tmp_path / filename
        target.write_text(content)

        client.upload_chat_files(7, [str(target)])

        body = sent["body"].decode()
        assert 'filename="{}"'.format(filename) in body
        assert content.strip() in body

    def test_several_files_dropped_together_all_reach_the_body(self, tmp_path, monkeypatch):
        client, sent = self._client(tmp_path, monkeypatch)
        names = ["metrics.csv", "vllm.log", "notes.txt"]
        for name in names:
            (tmp_path / name).write_text("content of {}\n".format(name))

        client.upload_chat_files(7, [str(tmp_path / name) for name in names])

        body = sent["body"].decode()
        for name in names:
            assert 'filename="{}"'.format(name) in body
            assert "content of {}".format(name) in body


class TestDroppedFiles:
    """Dropping a file on a running terminal types its path at the prompt.

    Driven through _route, the real entry point, because the routing is the
    thing under test: a quoted path does not start with "/" and never reached
    the command dispatcher at all.
    """

    @pytest.fixture
    def routed(self, shell, tmp_path):
        instance, client, out = shell
        instance.chat_id = 42
        sent = []
        instance._send_prompt = sent.append
        log = tmp_path / "vllm.log"
        log.write_text("latency_ms=118\n")
        return instance, client, out, sent, log

    def test_a_plain_dropped_path_uploads(self, routed):
        instance, client, _, sent, log = routed

        instance._route(str(log))

        assert client.uploads == [(42, [str(log)])]
        assert sent == []

    def test_a_backslash_escaped_path_uploads(self, routed, tmp_path):
        instance, client, _, _, _ = routed
        folder = tmp_path / "my logs"
        folder.mkdir()
        log = folder / "vllm.log"
        log.write_text("x\n")

        instance._route(str(log).replace(" ", "\\ "))

        assert client.uploads == [(42, [str(log)])]

    def test_a_quoted_path_uploads(self, routed, tmp_path):
        instance, client, _, sent, _ = routed
        folder = tmp_path / "my logs"
        folder.mkdir()
        log = folder / "vllm.log"
        log.write_text("x\n")

        instance._route("'{}'".format(log))

        assert client.uploads == [(42, [str(log)])]
        assert sent == []

    def test_two_dropped_files_upload_together(self, routed, tmp_path):
        instance, client, _, _, _ = routed
        first = tmp_path / "a.log"
        first.write_text("a\n")
        second = tmp_path / "b.csv"
        second.write_text("b\n")

        instance._route("{} {}".format(first, second))

        assert client.uploads == [(42, [str(first), str(second)])]

    def test_a_file_uri_uploads(self, routed):
        instance, client, _, _, log = routed

        instance._route("file://{}".format(log))

        assert client.uploads == [(42, [str(log)])]

    def test_a_trailing_space_does_not_break_the_drop(self, routed):
        instance, client, _, _, log = routed

        instance._route("{} ".format(log))

        assert client.uploads == [(42, [str(log)])]

    def test_a_drop_before_the_chat_starts_says_so(self, routed):
        instance, client, out, _, log = routed
        instance.chat_id = None

        instance._route(str(log))

        assert client.uploads == []
        assert "vllm.log" in out.getvalue()
        assert "send a message" in out.getvalue().lower()

    def test_a_real_command_still_dispatches(self, routed):
        instance, client, out, sent, _ = routed

        instance._route("/help")

        assert client.uploads == []
        assert sent == []
        assert "Skyportal commands" in out.getvalue()

    def test_upload_with_an_argument_still_reaches_its_handler(self, routed):
        instance, client, _, _, log = routed

        instance._route("/upload {}".format(log))

        assert client.uploads == [(42, [str(log)])]

    def test_an_unknown_slash_command_is_still_unknown(self, routed):
        instance, client, out, sent, _ = routed

        instance._route("/nope/not/a/file")

        assert client.uploads == []
        assert sent == []
        assert "Unknown command" in out.getvalue()

    def test_a_bare_filename_is_a_message_not_an_upload(self, routed):
        instance, client, _, sent, _ = routed

        instance._route("check vllm.log for errors")

        assert client.uploads == []
        assert sent == ["check vllm.log for errors"]

    def test_a_relative_path_that_exists_is_still_a_message(self, routed, tmp_path, monkeypatch):
        """A drop always types an ABSOLUTE path, so a relative one is someone typing.

        Without the is_absolute() guard, running the shell from a directory that
        happens to contain vllm.log would turn the message "vllm.log" into an
        upload.
        """
        instance, client, _, sent, _ = routed
        monkeypatch.chdir(tmp_path)

        instance._route("vllm.log")

        assert client.uploads == []
        assert sent == ["vllm.log"]

    def test_an_apostrophe_in_a_message_is_not_a_parse_error(self, routed):
        instance, client, out, sent, _ = routed

        instance._route("don't restart it")

        assert client.uploads == []
        assert sent == ["don't restart it"]
        assert "Could not parse" not in out.getvalue()

    def test_a_dropped_directory_is_not_an_upload(self, routed, tmp_path):
        instance, client, _, _, _ = routed

        instance._route(str(tmp_path))

        assert client.uploads == []


class TestMarkupSafety:
    """A filename is untrusted text on its way through Rich's markup parser.

    Rich RAISES MarkupError on an unbalanced closing tag rather than rendering
    it, so an unescaped name does not just restyle the line — it crashes the
    handler printing it.
    """

    EVIL = "report[/green][bold red]INJECTED[/bold red].log"

    def test_a_bracketed_filename_from_the_server_does_not_crash(self, shell, tmp_path):
        instance, client, out = shell
        instance.chat_id = 42
        client.response = {"success": True, "files": [{"name": self.EVIL, "size": 1024}]}
        log = tmp_path / "vllm.log"
        log.write_text("x\n")

        instance._cmd_upload([str(log)])

        text = out.getvalue()
        assert "INJECTED" in text
        assert "Attached" in text

    def test_a_bracketed_filename_is_shown_literally(self, shell, tmp_path):
        instance, client, out = shell
        instance.chat_id = 42
        client.response = {"success": True, "files": [{"name": self.EVIL, "size": 1024}]}
        log = tmp_path / "vllm.log"
        log.write_text("x\n")

        instance._cmd_upload([str(log)])

        assert "[bold red]" in out.getvalue()

    def test_a_bracketed_name_in_an_error_does_not_crash(self, shell, tmp_path):
        instance, client, out = shell
        instance.chat_id = 42
        client.error = PortalError("{} is empty".format(self.EVIL))
        log = tmp_path / "vllm.log"
        log.write_text("x\n")

        try:
            instance._cmd_upload([str(log)])
        except PortalError as exc:
            instance._show_portal_error(exc)

        assert "INJECTED" in out.getvalue()

    def test_a_bracketed_name_in_the_no_chat_notice_does_not_crash(self, shell, tmp_path):
        instance, client, out = shell
        instance.chat_id = None
        # No slash: it must be a name the filesystem accepts, while still
        # carrying the brackets Rich would try to parse.
        odd = tmp_path / "weird[dim]name.log"
        odd.write_text("x\n")

        instance._route(str(odd))

        assert "weird" in out.getvalue()
        assert client.uploads == []
