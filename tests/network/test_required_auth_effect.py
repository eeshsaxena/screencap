"""End-to-end EFFECT proof that ``--network`` self-recording **cannot** capture the
user's own bearer/refresh/ID-token exchange (SCR-140 / U1, R1).

The blocklist *logic* is already unit-tested (``test_blocklist.py``:
``missing_required_auth_hosts(patterns) == set()``), and ``proxy_runner`` fails closed if
a required auth host is missing. But a unit test only proves the regex *matches* the auth
hosts — it cannot prove that real mitmproxy *tunnels* them at runtime. The documented
team lesson is exactly this gap: a wrong ``ignore_hosts`` pattern once silently tunneled
**every** flow while the unit tests stayed green
(``docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md``).
Its Prevention #1: *"Spawn the tool with the exact production options, then check the
EFFECT. End-to-end EFFECT check >> regex unit test."*

This test does that. It spawns real ``mitmdump`` with the **production** ``ignore_hosts``
(built via ``build_ignore_hosts_regex`` — so it tracks config drift) and proves, by TLS
cert issuer, that:

  * each ``REQUIRED_AUTH_IGNORE_HOSTS`` host is **tunneled** (the served leaf cert is
    issued by the *real* upstream CA, not the proxy's generated CA) → the capture addon
    physically cannot see the plaintext credential exchange; and
  * a non-listed **control** host IS **intercepted** (leaf issued by the proxy CA) — a
    mandatory guard against a vacuous "everything tunnels" pass.

Discriminator: the proxied leaf cert's *issuer* is compared against the mitmdump-generated
CA's actual *subject* (read from the proxy confdir), so there is no hardcoded CA name.

This is a **gated local pre-deploy proof**, not a CI merge gate: it needs ``mitmdump`` on
PATH and network reachability to the real auth hosts, and skips cleanly otherwise. The
*continuous* regression guard against a dropped auth host remains the always-on
``missing_required_auth_hosts`` unit test + the ``proxy_runner`` fail-closed gate; this
test adds the runtime EFFECT proof on top of them. Run before the cloud function deploys.

Scope: the 4-host set is intentionally screencap's own Firebase/Google auth flow — it is
NOT exhaustive across all SSO providers (see the SCR-140 plan's Deferred to Follow-Up Work
re: ``login.microsoftonline.com``).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

import pytest
from cryptography import x509

from screencap.network.blocklist import (
    REQUIRED_AUTH_IGNORE_HOSTS,
    build_ignore_hosts_regex,
)
from screencap.network.config import NetworkConfig
from screencap.privacy.policy import PrivacyConfig

# Opt-in gate. This is a LOCAL PRE-DEPLOY PROOF, not a CI merge gate: it spawns mitmdump and
# makes real TLS connections to live Google auth hosts. It must never run (and must never
# touch the network) during a normal ``pytest`` run. ``@pytest.mark.slow`` alone does NOT
# guarantee that — this repo does not deselect ``slow`` by default — so an explicit env-var
# opt-in is the load-bearing gate. Run before the cloud function deploys:
#     SCREENCAP_NETWORK_EFFECT_TEST=1 pytest tests/network/test_required_auth_effect.py
_OPT_IN = os.environ.get("SCREENCAP_NETWORK_EFFECT_TEST") == "1"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _OPT_IN,
        reason="live pre-deploy proof — set SCREENCAP_NETWORK_EFFECT_TEST=1 to run",
    ),
    pytest.mark.skipif(shutil.which("mitmdump") is None, reason="mitmdump not installed"),
]

# A host that must NOT be on the ignore list, so the proxy intercepts it. Used as the
# positive control: if this is tunneled too, the proxy is misconfigured and the auth-host
# assertions would pass vacuously.
CONTROL_HOST = "example.com"


class _Unreachable(Exception):
    """Raised when a probe can't reach upstream/proxy — turns into a skip, not a fail."""


def _production_ignore_hosts() -> list[str]:
    """The exact ``ignore_hosts`` a default ``screencap start --network`` produces —
    same builder call as ``proxy_runner`` (default privacy + network config, no override).
    Building from the real function (not a hand-copied regex) means this test tracks any
    future change to the production pattern set."""
    return build_ignore_hosts_regex(PrivacyConfig(), NetworkConfig())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _proxied_leaf_issuer(proxy_port: int, host: str, port: int = 443) -> x509.Name:
    """CONNECT to ``host:port`` through the proxy, complete an *unverified* TLS handshake,
    and return the served leaf certificate's issuer. Unverified because an intercepting
    proxy presents a cert chained to a CA the test does not trust — we only want to read
    who signed it, never to validate it."""
    try:
        sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=20)
    except OSError as exc:  # proxy not accepting
        raise _Unreachable(f"proxy unreachable: {exc}") from exc
    try:
        sock.sendall(
            f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
        )
        sock.settimeout(20)
        head = sock.recv(4096)
        status_line = head.split(b"\r\n", 1)[0]
        if b" 200" not in status_line:
            # The proxy refused to open the tunnel (often an upstream-connect failure for
            # an ignored host on a blocked network) — can't prove anything, skip.
            raise _Unreachable(f"CONNECT {host} not 200: {status_line!r}")
        ctx = ssl._create_unverified_context()
        try:
            tls = ctx.wrap_socket(sock, server_hostname=host)
        except ssl.SSLError as exc:
            raise _Unreachable(f"TLS handshake to {host} failed: {exc}") from exc
        try:
            der = tls.getpeercert(binary_form=True)
        finally:
            tls.close()
        if not der:
            raise _Unreachable(f"no peer cert for {host}")
        return x509.load_der_x509_certificate(der).issuer
    except (OSError, ssl.SSLError) as exc:
        raise _Unreachable(f"probe of {host} failed: {exc}") from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass


@pytest.fixture(scope="module")
def proxy():
    """Spawn real ``mitmdump`` with the production ``ignore_hosts`` and a throwaway CA in a
    tmp confdir. Yields ``(proxy_port, ca_subject)``. Guarantees teardown even on failure."""
    import tempfile

    patterns = _production_ignore_hosts()
    port = _free_port()
    confdir = Path(tempfile.mkdtemp(prefix="screencap-mitm-effect-"))

    cmd = [
        "mitmdump",
        "--listen-host", "127.0.0.1",
        "--listen-port", str(port),
        "--set", f"confdir={confdir}",
    ]
    for p in patterns:
        cmd += ["--set", f"ignore_hosts={p}"]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ca_cert_file = confdir / "mitmproxy-ca-cert.pem"
    try:
        # Ready when the CA has been generated AND the proxy port accepts connections.
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.skip(f"mitmdump exited early (rc={proc.returncode})")
            if ca_cert_file.exists():
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        ready = True
                        break
                except OSError:
                    pass
            time.sleep(0.25)
        if not ready:
            pytest.skip("mitmdump did not become ready within 30s")
        time.sleep(0.5)  # small grace for addon/proxy machinery to finish loading

        ca_subject = x509.load_pem_x509_certificate(ca_cert_file.read_bytes()).subject
        # Yield the exact patterns the proxy was spawned with, so the control test asserts
        # against them directly (provably the same list) instead of recomputing.
        yield port, ca_subject, patterns
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(confdir, ignore_errors=True)


def _issuer_is(issuer: x509.Name, subject: x509.Name) -> bool:
    return issuer.rfc4514_string() == subject.rfc4514_string()


def test_control_host_is_not_on_ignore_list_and_is_intercepted(proxy):
    """The positive control. First a pure-regex guarantee that ``example.com`` is genuinely
    NOT matched by the production patterns (so a future blocklist expansion can't silently
    neutralize the control), then the runtime EFFECT: the proxy intercepts it (leaf issued
    by the proxy CA). Without this, an all-tunnel misconfiguration would pass vacuously."""
    port, ca_subject, patterns = proxy

    assert not any(re.search(p, f"{CONTROL_HOST}:443", re.IGNORECASE) for p in patterns), (
        f"control host {CONTROL_HOST} is matched by an ignore_hosts pattern — pick a "
        f"different control or the interception guard is void"
    )

    try:
        issuer = _proxied_leaf_issuer(port, CONTROL_HOST)
    except _Unreachable as exc:
        pytest.skip(f"network unreachable for control host: {exc}")
    assert _issuer_is(issuer, ca_subject), (
        f"control host {CONTROL_HOST} was NOT intercepted — issuer {issuer.rfc4514_string()!r} "
        f"!= proxy CA {ca_subject.rfc4514_string()!r}; the proxy isn't intercepting, so the "
        f"auth-host tunnel assertions would be vacuous"
    )


@pytest.mark.parametrize("host", sorted(REQUIRED_AUTH_IGNORE_HOSTS))
def test_required_auth_host_is_tunneled(proxy, host):
    """The core proof: each non-overridable auth host is CONNECT-tunneled, so the served
    leaf cert is the REAL upstream cert (issued by a public CA), NOT the proxy CA — meaning
    mitmproxy never TLS-intercepts it and the capture addon can never see the plaintext
    bearer/refresh/ID-token exchange."""
    port, ca_subject, _patterns = proxy
    try:
        issuer = _proxied_leaf_issuer(port, host)
    except _Unreachable as exc:
        pytest.skip(f"network unreachable for {host}: {exc}")
    assert not _issuer_is(issuer, ca_subject), (
        f"auth host {host} was INTERCEPTED (leaf issued by the proxy CA "
        f"{ca_subject.rfc4514_string()!r}) — the proxy is capturing the user's own "
        f"credential exchange; it must be tunneled"
    )

# (Subdomain regex coverage — that ``^(.+\.)?{host}:\d+$`` also tunnels subdomains — is an
# always-on guard in test_blocklist.py::test_auth_hosts_blocked_even_when_default_overridden;
# not duplicated here, where the value is the live runtime EFFECT, not the regex.)
