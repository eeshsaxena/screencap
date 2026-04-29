# Network Capture

## What it does

Captures HTTP/HTTPS request/response **metadata** (URL, method, host, status, headers, body size, body sha256, content-type, WebSocket frames) by running mitmproxy as a managed system proxy. Off by default; opt in with `screencap start --network`. V1 is local-only and metadata-only — body bytes are hashed and discarded.

The network capture pipeline runs in **parallel** to the engine recording pipeline. It does not feed `event_q`, has its own writer process, and stops at the DB row in V1. Cloud upload (events.jsonl emission of network events) is V1.75 work.

## Phasing

V1 ships a deliberately narrow surface so the premise (URL + status + sizes + sha256 + screen events helps the model) can be validated before paying the encryption + cloud-upload tax.

| Phase | Adds | Status |
|---|---|---|
| **V1 (today)** | mitmproxy embedded as `mp.Process`, addon hooks `request`/`response`/`websocket_*`, `recording.db.network_event` table, R10 capture-time redaction, system proxy lifecycle, `screencap network restore` / `uninstall`, `screencap _network-dump` debug helper | Shipped |
| V1.5 | Body capture + AES-256-GCM encryption-at-rest, KEK in Keychain, per-recording DEK, body-allowlist, `NetworkScrubPipeline` for export-time decrypt+scrub | Deferred (gated on V1 trial) |
| V1.75 | `network.*` lines in `events.jsonl` (CLI export, chunk JSONL, recovery), `build_cloud_network_filter` factory, AST guard, `NetworkExportMode` enum, cloud bucket policy | Deferred (gated on V1.5) |

V1's load-bearing safety property: **zero `network.*` lines in `events.jsonl` from any caller** (CLI export, auto-export, chunk processor, recovery). Three explicit scope-guard tests assert this. The reasoning: `_auto_export` and `screencap upload` cannot distinguish local-vs-cloud intent at runtime, so wiring `network_rows` into `unified_export_events` before V1.75's cloud bucket policy is decided would silently leak metadata to cloud.

## How it's wired

```
┌──────────────── recording worker process ────────────────────────────┐
│                                                                       │
│  engine.record()                                                      │
│   ├─ pre-flight (lifecycle.py): fcntl flock → mitmproxy import →      │
│   │   port nego (8080-8090) → networksetup callable → CA verify       │
│   │   (Keychain prompt on first run) → proxy dir setup                │
│   │                                                                   │
│   ├─ engine layer = SINGLE OWNER of system-proxy lifecycle:           │
│   │   ① snapshot system proxy state to .proxy_state.json              │
│   │      AND durable copy under ~/.screencap/proxy/snapshots/         │
│   │   ② spawn writer process (network_write_q)                        │
│   │   ③ spawn proxy mp.Process via spawn ctx                          │
│   │   ④ wait_for_ready 10s on started_event                           │
│   │   ⑤ write sentinel + .network_child.json handoff (atomic)         │
│   │   ⑥ signal handoff_ready_event for SessionController              │
│   │   ⑦ osascript admin: networksetup -setweb*proxy* (1 prompt)       │
│   │   ⑧ spawn reader thread                                           │
│   │                                                                   │
│   ▼                                                                   │
│  reader thread (network_event_reader)                                 │
│   ├─ drains proxy out_q (mp.Queue maxsize=1000)                       │
│   ├─ routes NetworkPinFailureEvent to console (control-only)          │
│   ├─ regular events → network_write_q (SynchronizedQueue maxsize=100) │
│   └─ on backpressure: NetworkDropBurstEvent(source="reader")          │
└─────────┬─────────────────────────────────────────────────────────────┘
          │
          ▼  (mp.Queue across spawn boundary)
┌─── proxy mp.Process (spawn ctx) ─────────────────────────────────────┐
│                                                                       │
│  proxy_runner.run_proxy()                                             │
│   ├─ spawn-mode assert (PRNG safety for V1.5+ AES-GCM)                │
│   ├─ load mitmproxy.tools.dump.DumpMaster                             │
│   ├─ install NetworkCapture addon                                     │
│   ├─ ignore_hosts regex: PrivacyConfig.mask_domains ∪                 │
│   │     network.extra_blocklist ∪ DEFAULT_BLOCKLIST                   │
│   │     (NO IP-literal anchors — see invariants)                      │
│   └─ asyncio loop running master.run()                                │
│                                                                       │
│  NetworkCapture addon hooks:                                          │
│   ├─ requestheaders / responseheaders → install chunk-hashing         │
│   │     transformer iff body > body_size_cap or chunked               │
│   ├─ request / response → R10 redact (auth headers, query params,     │
│   │     sensitive headers via network/redaction.py constants),        │
│   │     emit NetworkRequestEvent / NetworkResponseEvent               │
│   ├─ websocket_start → NetworkWebSocketUpgradeEvent                   │
│   ├─ websocket_message → NetworkWebSocketFrameEvent                   │
│   ├─ tls_clienthello → opt-out of MITM for runtime-tunnel hosts       │
│   ├─ error → TLS-pinning detect, NetworkPinFailureEvent (1× per host) │
│   └─ done → final NetworkDropBurstEvent flush + summary log           │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                       network_write_q (SynchronizedQueue)
                                   │
                                   ▼
┌─── network_event_writer process (mp.Process) ────────────────────────┐
│  write_network_events()                                               │
│   ├─ batch insert via crud.insert_network_event                       │
│   ├─ NO event.type assertion (handles 5 subkinds in one queue)        │
│   └─ flush on terminate, drain queue before exit                      │
└─────────┬────────────────────────────────────────────────────────────┘
          ▼
   recording.db.network_event   (V1: terminus — DB-only, no JSONL)
```

Network events bypass `event_q` entirely. WebSocket-heavy apps (Slack web, ChatGPT streaming) emit 50+ frames/sec; routing through the central event_q (sized for screen frames, `maxsize=20`) would starve action/window events.

## Lifecycle

### Pre-flight (`network/lifecycle.py:preflight_or_raise`)

Order is load-bearing:

1. **`fcntl.flock`** on `~/.screencap/.network_active.lock` — single-instance gate, races out concurrent `screencap start --network` calls before they trigger any user-visible side effect.
2. **`restore_orphaned_proxy_state`** — clean up any snapshot files orphaned by a prior crashed worker (global sentinel, durable copies under `~/.screencap/proxy/snapshots/`, per-recording-dir scan). Runs BEFORE prompts so users don't see spurious admin dialogs.
3. **mitmproxy import check** (no UI).
4. **Port auto-negotiation** 8080-8090 (or pinned via `[network] proxy_port`).
5. **`networksetup` callable check** (no UI).
6. **CA verify**; install if missing/expired (Keychain prompt — first run only).
7. **`~/.screencap/proxy/` setup** — chmod 700, backup-exclude xattr, iCloud-sync warning if `--output` resolves under iCloud Drive.

The first admin password prompt is at engine setup step ⑦ (`set_proxy_all`), not pre-flight — see invariants.

### Engine setup (`engine/recorder.py:_setup_network_capture`)

Steps ① through ⑧ in the diagram above. Wrapped in try/except with `_cleanup_partial`: any failure terminates the writer + proxy processes, deletes sentinel + handoff + durable snapshot, and restores system proxy if the flip already happened.

### Engine teardown (`engine/recorder.py:_teardown_network_capture`)

Restore-FIRST order. Runs as the FIRST step of the join sequence, before any other writer is joined:

1. `system_proxy.restore_all(snapshot)` — pre-recording proxy state restored via the cached osascript admin auth (within macOS's ~5-minute window the second admin prompt is silent).
2. Write `<recording_dir>/.proxy_restored` marker — SessionController's `_reap_finishing_workers` reads this to skip orphan-cleanup.
3. Terminate proxy mp.Process (5s grace, then kill).
4. Diff `list_active_services()` against the start-time snapshot; write `.network_services_changed.json` coverage marker if any services were added/removed mid-recording (e.g. user enabled a VPN). Surfaced by `screencap _network-dump --verbose`.
5. Delete sentinel + handoff + durable snapshot copy.

The reader thread joins on `terminate_processing` + a drain phase (5 consecutive empty 50ms polls) so events the addon already pushed but the reader hasn't consumed are not silently dropped.

### Force-kill recovery

If the worker process dies before teardown runs:

- `~/.screencap/.network_active` global sentinel persists with `worker_pid` + `worker_create_time` + `cmdline_tail` (F3-v9 PID-reuse defense).
- `<recording_dir>/.proxy_state.json` AND `~/.screencap/proxy/snapshots/<recording_id>.proxy_state.json` (durable copy) persist.
- Next `screencap start --network` runs `restore_orphaned_proxy_state` at pre-flight step (2) and reverts.
- Standalone `screencap network restore` does the same on demand. Idempotent.

## Privacy layers

Three layers compose at decision time:

1. **Layer 1 — `PrivacyConfig.mask_domains`** (suffix match). Inherited from screen capture's privacy config so the same domains are gated at both layers. **`exclude_apps` is intentionally NOT inherited** — it's a bundle-ID filter; the proxy sees flows, not processes.
2. **Layer 2 — `[network] extra_blocklist` + curated `DEFAULT_BLOCKLIST`** (banks, password managers, OAuth providers, payment processors). User-extensible; user can override the curated set with `override_default_blocklist = true`.
3. **Layer 3 — body allowlist** is V1.5 work. V1 hashes-and-discards every body unconditionally.

Layers 1 and 2 produce a single regex passed to mitmproxy's `ignore_hosts`, which tunnels matching hosts at CONNECT time so they're never decrypted. Plain HTTP requests to blocked hosts are forwarded to the upstream server unchanged but skip event emission, so blocking never causes user-visible connection failures.

R10 capture-time redaction (single source of truth in `network/redaction.py`):

- Auth headers — `Authorization`, `Cookie`, `Set-Cookie`, `Proxy-Authorization`, `X-Hub-Signature*`, `X-Webhook-Secret`, `X-AccessKey`, plus `*-api-key` / `*-token` / `*-secret` patterns. Replaced with `[REDACTED:auth-header]`.
- Sensitive non-auth headers — `Referer`, `Origin`, `X-CSRF-Token`, `X-Forwarded-*`, etc. Replaced with `[REDACTED:sensitive-header]`.
- URL query parameters — `access_token`, `code`, `signature`, `state`, etc. Value replaced with `[REDACTED:query-param]`, name preserved.

URL paths are a documented V1 gap — path-embedded auth tokens (OAuth codes, password-reset tokens, JWT in path segments) are stored as captured. V1.5 will add path-template denylists to `network/redaction.py`.

## Data on disk

All network-related files in addition to the recording dir's standard contents:

| File | Owner | Notes |
|---|---|---|
| `recording.db.network_event` | network writer | Per-flow rows; `kind` enum: `request`/`response`/`ws_upgrade`/`ws_frame`/`drop_burst`. V1 terminus — no JSONL emission anywhere. |
| `<recording_dir>/.mitmdump.log` | proxy_runner | Addon's own log: flow count, drop count, runtime tunnel hosts, errors. Diagnostic — read this first when debugging. |
| `<recording_dir>/.proxy_state.json` | engine | Per-service snapshot (web/secure-web proxy + bypass domains) for restore. Atomic-write, chmod 600. |
| `<recording_dir>/.network_child.json` | engine | Handoff: `{proxy_pid, worker_pid, started_at}`. Consumed by SessionController to register the proxy in `recording.pid` children. Atomic-write. |
| `<recording_dir>/.proxy_restored` | engine | Marker file written after restore_all completes; SessionController's `_reap_finishing_workers` reads to skip orphan-cleanup. |
| `<recording_dir>/.network_services_changed.json` | engine | Coverage-gap marker if active services were added/removed mid-recording. Surfaced by `_network-dump --verbose`. |
| `~/.screencap/.network_active` | engine | Global sentinel, single per-machine. Holds PIDs + create_times + port. |
| `~/.screencap/.network_active.lock` | lifecycle | `fcntl.flock` target for single-instance enforcement. |
| `~/.screencap/proxy/mitmproxy-ca.pem` | ca_lifecycle | Combined PEM (private key + cert), 30-day expiry, chmod 600. |
| `~/.screencap/proxy/mitmproxy-ca-cert.pem` | ca_lifecycle | Cert-only PEM (for `security add-trusted-cert`). |
| `~/.screencap/proxy/ca-identity.json` | ca_lifecycle | `{cn, sha256_hex, sha1_hex}` — `security delete-certificate -Z` lookup keys, chmod 600. |
| `~/.screencap/proxy/snapshots/<recording_id>.proxy_state.json` | engine | Durable snapshot copy, survives custom `--output` paths. |

## Load-bearing invariants

- **mitmproxy is embedded as a library, not a subprocess.** PyInstaller-frozen binaries can't ship the addon as `mitmdump -s addon.py` (file discovery breaks in frozen bundles, per the spaCy precedent in `docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md`). Use `mitmproxy.tools.dump.DumpMaster(opts)` programmatic embedding; addons attach via `master.addons.add(...)`.
- **Engine layer is the SINGLE OWNER of system-proxy lifecycle.** `recorder.start_recording` (top-level) does NOT call `set_proxy_all` or `restore_all`. Splitting ownership across layers caused set-twice and set-before-listener-ready bugs in earlier drafts.
- **Network events bypass `event_q`.** Reader thread feeds `network_write_q` directly. WebSocket-heavy load would otherwise starve screen/action events on the size-20 central queue. `process_events()` does NOT see network events; do not add a network branch to it.
- **`PrivacyConfig` must pickle.** `__post_init__` wraps `app_classes` in `MappingProxyType` which is non-picklable by default. Custom `__getstate__`/`__setstate__` round-trip it through a plain `dict` and re-wrap on the receiving side. Required for the proxy mp.Process which receives PrivacyConfig in its args.
- **IP-literal blocking lives at the addon, NOT in mitmproxy `ignore_hosts`.** `mitmproxy/addons/next_layer.py:220-241` matches `ignore_hosts` against BOTH the original hostname AND the DNS-resolved peername. An IPv4 anchor like `^\d+\.\d+\.\d+\.\d+:\d+$` matches the resolved peername of every flow, so every connection gets tunneled. The addon's `is_host_blocked` first-check at `request()` time sees `flow.request.host` (the literal user-supplied host, IP only when the user typed one) — that's the right layer for IP-literal policy. Full diagnostic walkthrough in `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`.
- **`set -e` prefix on all multi-command osascript shell strings.** `do shell script "cmd1; cmd2; cmd3"` only propagates the LAST command's exit code, so a failed mid-script `setwebproxy` is silently masked by a successful trailing `setwebproxystate`. `set -e` makes `/bin/sh` exit non-zero on the first failure.
- **Reader thread has a drain phase after terminate.** Engine teardown runs restore-FIRST, then terminates the proxy. Events the proxy emits between those two steps must reach the writer; the reader's `_DRAIN_EMPTY_THRESHOLD = 5` empty-poll counter bounds drain-phase exit at ~250ms while still draining trailing events.
- **`mp.Queue.empty()` is unreliable across processes.** Use empty-poll counting in drain loops. The same caution applies anywhere the network reader inspects queue state from a different process than the producer.
- **F3-v9 PID-reuse defense.** Sentinel + handoff store `psutil.create_time()` and a cmdline tail alongside the PID. Liveness checks require all three to match — `pid_exists()` alone returns True for reused PIDs and would cause the cleanup path to skip a real orphan.
- **Recording-id keyed durable snapshot deletion.** `restore_orphaned_proxy_state` deletes `~/.screencap/proxy/snapshots/<recording_id>.proxy_state.json` using `recording_id` from the snapshot's `extra` dict, NOT from `path.stem` — per-recording paths' stem is `.proxy_state` (no rec_id), which would leak the durable copy and cause a second admin prompt for the same orphan on the next pre-flight scan.
- **Spawn mode required.** AES-GCM nonce safety (V1.5+) depends on `os.urandom` being independently seeded in the child; fork-mode children inherit the parent's PRNG state. CLI entry pins `multiprocessing.set_start_method("spawn", force=True)` BEFORE any `mp.Queue` is constructed; a redundant in-child assertion at `proxy_runner.run_proxy` head is defense-in-depth.
- **`pidfile._is_screencap_process` cmdline-allowlist widening.** macOS spawn-mode children's cmdline is the Python interpreter path + `--multiprocessing-resource-tracker N` bootstrap with NO "screencap" substring. The widened filter accepts cmdlines containing any name from the recording.pid `children` allowlist (e.g. `mitmproxy`); without the widening, `terminate_processes` would skip the proxy child and `screencap stop --force` would leave it running.

## Before you change it

- Adding a new network event type: extend `EventType` enum, add the Pydantic class to `events.py`, add to the `Event` union and `EVENT_TYPE_MAP`, extend `_KIND_MAP` in `write_network_events`, add a `dict_to_network_event` branch, write a `crud.insert_network_event` test fixture. The `kind` column on `network_event` stores the SHORT form (`request`, `response`, etc.); the Pydantic `type` field stores the dotted form (`network.request`).
- Adding a new addon hook: it runs on the proxy mp.Process's asyncio loop (single-loop invariant in mitmproxy 11.x). Persist nothing in the addon — push events to `out_q` instead. The reader thread is the boundary between mitmproxy state and engine state.
- Changing the ignore_hosts regex: any pattern that matches an IPv4-shaped peername or IPv6-shaped peername tunnels every flow (see invariants). Adversarial regression tests live in `tests/network/test_blocklist.py`.
- Adding a Layer 3 body-allowlist entry: V1.5 work. The default list is gated on training-team sign-off before V1.5 ships.
- Adding a CLI surface: keep the heavy `screencap.network.lifecycle` import inside the Click subcommand body (deferred-import convention). `screencap --help` is a hot path.
- Changing the teardown order: restore-FIRST trades in-flight long-lived connection interruption (Slack WebSocket, gRPC) for a smaller dead-listener window for new connections. Switching to `master.shutdown(graceful=True)` first is a documented V1.5+ option if real V1 trial data shows the in-flight interruption matters more than the dead-listener window.

## See also

- [recording-engine.md](./recording-engine.md) — the parallel pipeline this one runs alongside
- [event-system.md](./event-system.md) — how the 5 new network event types fit into the Pydantic hierarchy
- [database.md](./database.md) — the `network_event` table
- [privacy.md](./privacy.md) — `mask_domains` shared with screen capture, capture-time redaction
- [export-pipeline.md](./export-pipeline.md) — `unified_export_events.network_rows` parameter (V1: always `None`)
- `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md` — the universal-passthrough bug walkthrough
- `docs/tickets/2026-04-29-fix-network-admin-prompts-per-recording.md` — V1.5 admin-prompt-count follow-up
- `docs/plans/2026-04-25-003-feat-network-proxy-logging-plan.md` — V1/V1.5/V1.75 plan with full requirements trace
