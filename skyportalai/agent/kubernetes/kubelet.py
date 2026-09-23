"""Read this node's own kubelet stats summary (#3588), sent raw like the /proc files.

The kubelet's /stats/summary reports every pod on the node: CPU (usageNanoCores),
memory (workingSetBytes), network and filesystem, per pod and per container. It is the
source metrics-server itself reads, so per-pod usage no longer depends on metrics-server
being installed. Nothing is parsed here; the server derives the numbers.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
STATS_SUMMARY_PATH = "/stats/summary"
TIMEOUT_SECONDS = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer token to wherever the kubelet points; refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class KubeletClient:
    host_ip: str
    port: int
    ca_file: Path | None = None
    insecure_skip_verify: bool = False
    token_file: Path = SERVICE_ACCOUNT_DIR / "token"

    @property
    def url(self) -> str:
        host = f"[{self.host_ip}]" if ":" in self.host_ip else self.host_ip
        return f"https://{host}:{self.port}{STATS_SUMMARY_PATH}"

    def stats_summary(self, max_chars: int) -> tuple[str | None, str | None]:
        """(body, None) on success, else (None, reason). Never raises: a kubelet failure must not cost the /proc upload."""
        try:
            # Read per request: projected service-account tokens rotate.
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return None, f"token unreadable ({type(exc).__name__})"
        request = urllib.request.Request(self.url, headers={"Authorization": f"Bearer {token}"})
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self._tls_context()), _NoRedirect)
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(max_chars + 1)
        except urllib.error.HTTPError as exc:
            return None, f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLError):
                # Not every SSLError carries .reason / .verify_message; never let naming it raise.
                detail = getattr(reason, "verify_message", None) or getattr(reason, "reason", None) or reason
                return None, f"TLS: {detail}"
            return None, f"unreachable ({reason})"
        except (TimeoutError, OSError) as exc:
            return None, f"unreachable ({type(exc).__name__})"
        if len(raw) > max_chars:
            return None, "EFBIG"
        return raw.decode("utf-8", errors="replace"), None

    def _tls_context(self) -> ssl.SSLContext:
        if self.insecure_skip_verify:
            # Opt-in only (kubernetes.node.kubelet.insecureSkipVerify): for kubelets whose
            # serving certificate is self-signed and not issued by any CA we can mount.
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        ca_file = self.ca_file or SERVICE_ACCOUNT_DIR / "ca.crt"
        return ssl.create_default_context(cafile=str(ca_file)) if ca_file.exists() else ssl.create_default_context()
