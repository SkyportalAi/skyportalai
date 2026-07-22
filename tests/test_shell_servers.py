"""/servers renders SSH hosts and kube clusters as uniform rows with a Kind column."""

from io import StringIO

from rich.console import Console

from skyportal.shell import InteractiveShell


class FakeClient:
    base_url = "https://app.skyportal.ai"

    def __init__(self, payload):
        self._payload = payload

    def is_authenticated(self):
        return True

    def servers(self):
        return self._payload


def _render(payload, tmp_path, monkeypatch):
    monkeypatch.setenv("SKYPORTAL_LAST_CHAT_PATH", str(tmp_path / "last_chat"))
    out = StringIO()
    console = Console(file=out, width=160, force_terminal=False)
    shell = InteractiveShell(
        console=console,
        client_factory=lambda: FakeClient(payload),
        session=object(),
        token_prompt=lambda _prompt: "",
    )
    shell._cmd_servers([])
    return out.getvalue()


def test_kube_and_ssh_rows_share_columns(tmp_path, monkeypatch):
    text = _render([
        {"id": 1, "name": "ssh-box", "status": "connected", "host_type": "Physical",
         "target_kind": "ssh", "vcpu": 32, "ram": 128, "gpus": 2},
        {"id": 2, "name": "cluster-prod", "status": "connected", "host_type": "Production",
         "target_kind": "kubernetes", "vcpu": 128, "ram": 1024, "gpus": 8},
    ], tmp_path, monkeypatch)
    assert "Kind" in text
    assert "kubernetes" in text
    assert "128 vCPU / 1024 GB RAM / 8 GPU" in text
    assert "32 vCPU / 128 GB RAM / 2 GPU" in text


def test_missing_target_kind_defaults_to_ssh(tmp_path, monkeypatch):
    text = _render([{"id": 3, "name": "old-backend", "status": "connected"}], tmp_path, monkeypatch)
    assert "ssh" in text
