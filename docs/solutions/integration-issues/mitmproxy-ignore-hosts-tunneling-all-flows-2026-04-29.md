---
title: mitmproxy `ignore_hosts` IPv4 anchor caused universal CONNECT-tunneling (V1 network capture captured zero events)
date: 2026-04-29
track: bug
module: network/blocklist.py
problem_type: integration_issue
component: python_module
symptoms:
  - "mitmproxy ran but .mitmdump.log showed flows=0; no network events captured despite proxy listening, system proxy set, and addon loaded"
  - "addon hooks (request/response) never fired for any flow"
  - "curl through proxy presented the real upstream cert (e.g., Google Trust Services) instead of the screencap proxy CA, proving every flow was tunneled at the TCP layer"
root_cause: wrong_api
resolution_type: code_fix
severity: high
related_components:
  - tooling
tags:
  - mitmproxy
  - network-proxy
  - ignore-hosts
  - regex
  - tls-interception
  - peername
  - dns-resolution
  - network-capture
---

# mitmproxy `ignore_hosts` IPv4 anchor caused universal CONNECT-tunneling (V1 network capture captured zero events)

## Problem

V1 network capture appeared to work end-to-end — `screencap start --network` flipped the system proxy, the mitmdump child reported `proxy listening on [('127.0.0.1', 8080)], addon installed`, and no errors surfaced in the recorder log — but every recording finished with **zero captured network events**. Because V1 is a system-MITM HTTPS proxy (mitmproxy embedded as `DumpMaster` in a `mp.Process`, with the addon hooks `request` / `response` / `websocket_*` writing to a `mp.Queue` drained by a reader thread + writer process), "no events" is the failure mode that looks like success: the proxy was up, traffic was flowing through it, but mitmproxy was tunneling every flow at the TCP layer and never invoking our addon hooks. The hooks only fire on intercepted (TLS-MITM'd) flows; a tunneled flow is opaque to addons by design.

## Symptoms

- `screencap _network-dump <recording>` reports "No network events captured."
- `sqlite3 .../recording.db "SELECT count(*) FROM network_event"` returns `0`.
- `.mitmdump.log` shows clean startup: `proxy listening on [('127.0.0.1', 8080)], addon installed`.
- `.mitmdump.log` shutdown line shows `flows=0 final_drop_count=0` — the addon's `done()` hook ran but `requestheaders` / `request` never incremented `_flow_count` for any flow.
- No errors, no exceptions, no TLS handshake failure events — the proxy was simply tunneling everything past the addon.
- **Smoking gun:** running `curl -x http://127.0.0.1:8080 -v https://www.google.com/` while the proxy was active showed the certificate issuer was `C=US; O=Google Trust Services; CN=WR2` (the real Google cert), **not** `screencap proxy CA`. If interception had been working, curl would have rejected the cert (no client trust) or shown the screencap CA's cert in the chain. The real cert proved mitmproxy was passing TCP through untouched.

## What Didn't Work

Four hypothesis chains were chased before the actual cause was isolated:

- **VPN routing bypass.** Tailscale + ProtonVPN were both visible in the macOS routing table and in `~/.screencap/recordings/<name>/.proxy_state.json`'s pre-state. Ruled out by `route get default` showing the default route was still on `en0` (Wi-Fi), and Tailscale was idle (no active peers). System proxy on `en0` was correctly set to `127.0.0.1:8080`.
- **Per-app proxy bypass.** Suspected Safari + iCloud Private Relay or Chrome with custom proxy extensions silently bypassing the system proxy. Ruled out by direct `curl -x http://127.0.0.1:8080 https://...` — no browser involved, still passthrough. The bug was not at the system-proxy layer.
- **CA file format issue.** Suspected mitmproxy couldn't load our combined-PEM CA at `~/.screencap/proxy/mitmproxy-ca.pem`. Ruled out by loading it directly: `mitmproxy.certs.CertStore.from_store(Path("~/.screencap/proxy").expanduser(), "mitmproxy", "RSA", 2048)` returned `default_ca: <Cert(cn='screencap proxy CA', altnames=[])>` — the cert loaded fine, just wasn't being USED to sign upstream certs.
- **Mitmproxy version / option mismatch.** Suspected a v11 default change or wrong mode. Ruled out by inspecting the live master: `mitmproxy 11.0.2`, `mode=['regular']`, `ignore_hosts=[<our regex list>]`, `allow_hosts=[]`. Standard interception path. The proxy WAS configured correctly to intercept — except for one regex.

The breakthrough was **isolating with `mitmdump` directly** (skipping screencap entirely):

```bash
mitmdump --listen-port 8080 \
  --set ignore_hosts='^(.+\.)?chase\.com:\d+$' \
  --set ignore_hosts='^\d+\.\d+\.\d+\.\d+:\d+$' \
  --set ignore_hosts='^\[?[0-9a-f:]+\]?:\d+$'
```

Reproduced the passthrough behavior — curl still showed Google's real cert. Then re-ran without the IPv4 anchor → curl showed `screencap proxy CA` issuer → interception worked. The IPv4 regex was the cause. Reading `mitmproxy/addons/next_layer.py:220-241` then explained why.

## Solution

`src/screencap/network/blocklist.py` — drop the IP-literal anchors from the `ignore_hosts` regex list:

**Before:**

```python
def build_ignore_hosts_regex(privacy_config, network_config) -> list[str]:
    # ... build domain-suffix patterns ...
    for entry in sorted(entries):
        # ...
        patterns.append(rf"^(.+\.)?{escaped}:\d+$")

    # IP-literal anchors are always active — even when the user opts
    # out of DEFAULT_BLOCKLIST, we never proxy raw IPs (no SNI = no
    # safe way to gate body capture).
    patterns.append(r"^\d+\.\d+\.\d+\.\d+:\d+$")
    patterns.append(r"^\[?[0-9a-f:]+\]?:\d+$")

    return patterns
```

**After:**

```python
def build_ignore_hosts_regex(privacy_config, network_config) -> list[str]:
    # ... build domain-suffix patterns ...
    for entry in sorted(entries):
        # ...
        patterns.append(rf"^(.+\.)?{escaped}:\d+$")

    # No IP-literal anchors — mitmproxy matches ignore_hosts against
    # server.peername too, so any IPv4 regex tunnels every flow.
    return patterns
```

The intended safety property — "users typing `https://1.2.3.4/` directly are tunneled, never proxied" — was already enforced **at the addon level** in `src/screencap/network/capture_addon.py`'s `requestheaders` hook:

```python
def requestheaders(self, flow: Any) -> None:
    host = flow.request.host or ""
    if is_host_blocked(host, self._privacy_config, self._network_config):
        flow.metadata["screencap_blocked"] = True
        return
```

…where `is_host_blocked` calls `is_ip_literal(host)` first. At this layer `flow.request.host` is the **literal user-supplied host** — the IP only when the user typed one, the hostname for normal CONNECTs. So dropping the proxy-level IP anchor lost no safety; it just stopped causing universal passthrough.

## Why This Works

`mitmproxy/addons/next_layer.py:220-241` (mitmproxy 11.0.2) builds the candidate list of strings that `ignore_hosts` patterns are matched against:

```python
hostnames: list[str] = []
if context.server.peername:
    host, port, *_ = context.server.peername
    hostnames.append(f"{host}:{port}")        # <-- resolved IP, e.g. "142.250.190.46:443"
if context.server.address:
    host, port, *_ = context.server.address
    hostnames.append(f"{host}:{port}")        # <-- original hostname, e.g. "google.com:443"
    # ... + host_header, SNI, client.sni
```

Then:

```python
if ctx.options.ignore_hosts:
    ignored = any(
        re.search(rex, host, re.IGNORECASE)
        for host in hostnames
        for rex in ctx.options.ignore_hosts
    )
    if ignored:
        return True   # tunnel — addon hooks never fire
```

The matcher is `re.search` over a **cross-product** of every regex × every candidate hostname. `server.peername` is the post-DNS resolved socket address — it's populated for **every** flow, because every TCP connection has a resolved peer. The regex `^\d+\.\d+\.\d+\.\d+:\d+$` matches that peername unconditionally. So every flow hit `ignored=True` at the next_layer decision, mitmproxy chose `TCPLayer` instead of `HttpLayer(HTTPMode.transparent)`, and the addon's `request` / `response` hooks were never reached. The proxy faithfully tunneled bytes; curl saw the real upstream cert because there was no MITM in the path.

Why is the **addon layer** the right place to gate IP literals? Because the addon's `flow.request.host` is the host as it appeared in the client's CONNECT or HTTP request line — the **original** host the user (or app) actually requested. If the user typed `https://1.2.3.4/`, that's an IP literal there and `is_ip_literal` blocks. If the user requested `https://google.com/`, that's a hostname there even though `server.peername` will be `142.250.190.46`. Same property the proxy-level anchor was reaching for, but applied to the form of the hostname that has the right semantics for the policy decision. The `ignore_hosts` layer sees the resolved/canonical form, which is the wrong input for "did the user type an IP?".

## Prevention

**1. When wiring an "ignore list" / "passthrough list" / "denylist" into a third-party tool, verify the matcher's input semantics against the LIVE tool, not just the regex in isolation.** Run a positive control end-to-end:

```bash
# Spawn the tool with the exact production options, then check the EFFECT.
mitmdump --listen-port 8080 --set ignore_hosts='<your-pattern>' &
curl -sv -x http://127.0.0.1:8080 https://known-not-on-list.example.com/ 2>&1 \
  | grep -E '^\*  (issuer|subject):'
# Expected when interception works: issuer=YOUR proxy CA
# If you see the real upstream issuer, the pattern is over-matching.
```

A regex compiles fine and unit-tests pass while still being silently catastrophic at runtime if the matcher input isn't what you assumed. End-to-end EFFECT check >> regex unit test.

**2. Adversarial regression test — assert no pattern matches both a normal hostname AND an IPv4-shaped peername.** Already added at `tests/network/test_blocklist.py::TestBuildIgnoreHostsRegex::test_normal_host_does_not_match`:

```python
def test_normal_host_does_not_match(self):
    patterns = build_ignore_hosts_regex(_privacy(), _network())
    for p in patterns:
        assert not re.search(p, "google.com:443", re.IGNORECASE)
        assert not re.search(p, "142.250.190.46:443", re.IGNORECASE), (
            f"pattern matches an IPv4 peername: {p} "
            f"(would cause universal passthrough)"
        )
```

The IPv4-peername assertion is the load-bearing one — it would have caught the original bug at unit-test time.

**3. Pick the enforcement layer whose input form matches the policy intent — and document the choice at the gate.** When a safety property *can* be enforced at two layers (here: TCP-CONNECT regex vs application-level addon hook), pick the layer whose **inputs match the policy's intent**. IP-literal blocking's intent is "user typed an IP"; that information is preserved at `flow.request.host` (addon layer) and lost at `server.peername` (TCP layer, post-DNS). The same principle applies to URL canonicalizers (pre- vs post-normalization), DNS-aware matchers (CNAME-resolved vs original), and any other tool with both an "original input" and a "resolved/canonical" form. Document the layer choice in a comment block right above the matcher, naming the upstream code path (e.g., `mitmproxy next_layer.py:220-241`) so future readers can re-verify if the upstream refactors. A docstring pinning the upstream line range turns "I tested this and it works" into a citation.

## Related Issues

- `docs/plans/2026-04-25-003-feat-network-proxy-logging-plan.md` — V1 network proxy logging plan; this bug was caught during V1 shake-down and fixed in commit `4523eac7`.
- `docs/tickets/2026-04-29-fix-network-admin-prompts-per-recording.md` — Sibling V1 follow-up (admin password prompt count). Same feature surface, distinct issue.
- `docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md` — Adjacent prevention pattern: validate the frozen artifact end-to-end, not just the regex / import / config in isolation. Same "test the actual artifact" lesson, different failure mode.
- `docs/solutions/runtime-errors/sigint-handler-timing-and-recording-stop-methods.md` — Adjacent in the V1 implementation surface; informs how `proxy_runner` mp.Process is wired with signal handlers.
- mitmproxy upstream: `addons/next_layer.py` `_ignore_connection` method, lines 195-265. Behavior verified against mitmproxy 11.0.2.
