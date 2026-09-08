"""A batch approval covers several commands, and the CLI used to flatten them into one
160-character line: the order was lost and the tail was cut, so you approved commands you
were never shown. A batch needs approval precisely because something in it is gated, so
the hidden ones are as likely to be the dangerous ones."""

from skyportalai.shell.interactive import InteractiveShell


def _item(command_id, display_command, server_ids=(1,), namespace=""):
    return {
        "command_id": command_id,
        "display_command": display_command,
        "resolved_server_ids": list(server_ids),
        "namespace": namespace,
        "target_kind": "ssh",
    }


def _rendered(approval, ids=(), names=()):
    shell = InteractiveShell.__new__(InteractiveShell)
    shell.selected_server_ids = list(ids)
    shell.selected_server_names = list(names)
    return InteractiveShell._approval_detail(shell, approval).plain


def test_every_command_in_a_batch_is_shown():
    approval = {
        "command": "Concurrent command batch:\n[a] ...\n[b] ...",
        "batch_commands": [
            _item("preflight", "df -h"),
            _item("restart", "systemctl restart nginx"),
            _item("verify", "systemctl status nginx"),
        ],
    }
    out = _rendered(approval)
    for command in ("df -h", "systemctl restart nginx", "systemctl status nginx"):
        assert command in out, f"{command!r} was not shown to the user"


def test_commands_are_shown_in_the_order_they_will_run():
    approval = {
        "batch_commands": [
            _item("first", "echo one"),
            _item("second", "echo two"),
            _item("third", "echo three"),
        ]
    }
    out = _rendered(approval)
    assert out.index("echo one") < out.index("echo two") < out.index("echo three")


def test_one_command_per_line():
    approval = {"batch_commands": [_item("a", "echo one"), _item("b", "echo two")]}
    out = _rendered(approval)
    assert "echo one" not in [line.strip() for line in out.splitlines()][0]
    one = next(i for i, l in enumerate(out.splitlines()) if "echo one" in l)
    two = next(i for i, l in enumerate(out.splitlines()) if "echo two" in l)
    assert one != two, "commands share a line; the batch reads as one run-on string"


def test_a_long_batch_says_how_many_are_hidden_rather_than_trailing_an_ellipsis():
    batch = [_item(f"cmd-{i}", f"systemctl restart service-{i}") for i in range(20)]
    out = _rendered({"batch_commands": batch})
    assert "20 commands" in out
    assert "more not shown" in out
    assert "approving covers all 20" in out
    assert not out.rstrip().endswith("…"), "a silent ellipsis hides what is being approved"


def test_the_target_hosts_are_named_not_numbered():
    # "server 3" is not something anyone can consent to. The shell already knows the
    # names of the hosts in scope, so no API round trip is needed to say them.
    approval = {"batch_commands": [_item("a", "df -h", server_ids=(1, 2))]}
    out = _rendered(approval, ids=(1, 2), names=("prod-web", "prod-db"))
    assert "prod-web" in out
    assert "prod-db" in out


def test_an_unknown_host_falls_back_to_its_id():
    # Better a bare id than nothing: the command and its target must both be visible.
    approval = {"batch_commands": [_item("a", "df -h", server_ids=(7,))]}
    assert "7" in _rendered(approval, ids=(1,), names=("prod-web",))


def test_a_kubernetes_item_names_its_namespace():
    approval = {
        "batch_commands": [_item("pods", "kubectl get pods", namespace="production")]
    }
    assert "ns=production" in _rendered(approval)


def test_a_single_command_approval_still_renders():
    assert "systemctl restart nginx" in _rendered(
        {"command": "systemctl restart nginx", "type": "bash_command"}
    )


def test_an_approval_with_no_command_says_something():
    assert _rendered({"approval_id": "abc-123"}).strip()


def test_a_raw_command_is_never_shown_in_place_of_the_redacted_one():
    # display_command is the server's redacted form. Falling back to `command` would put
    # a secret the server deliberately masked in front of the user.
    item = _item("a", "")
    item["command"] = "curl -H 'Authorization: Bearer sk-live-SECRET'"
    out = _rendered({"batch_commands": [item]})
    assert "sk-live-SECRET" not in out
