---
title: "P2: The proxy fail-closed auth-host gate has no test"
status: resolved
priority: medium
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #7)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P2: Untested fail-closed gate in proxy_runner

## Problem

`src/screencap/network/proxy_runner.py:154` — the gate that refuses to start the proxy when an auth/token host is missing from `ignore_hosts`:

```python
missing = missing_required_auth_hosts(ignore_hosts)
if missing:
    _emit_log(log_path, "FATAL: required auth hosts missing ... refusing to start proxy")
    return
```

This is the load-bearing defense against TLS-intercepting (and capturing) the user's own OAuth/refresh/ID-token traffic during `--network`. `tests/network/test_proxy_runner.py` exists but nothing references `missing_required_auth_hosts` — the gate itself is untested. The `mitmproxy ignore_hosts` learning (`docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`) is precisely "the regex passed unit tests but broke at runtime," so this needs a real behavioral test.

## What's needed

1. Add a test to `tests/network/test_proxy_runner.py` that patches `build_ignore_hosts_regex` to drop one `REQUIRED_AUTH_IGNORE_HOST`, runs `run_proxy`, and asserts the proxy refuses to start (`started_event` never set / early return). Mirror the existing addon-failure test pattern.
2. (Advisory, for U5) add a locally-runnable, CI-skipped integration smoke test that spawns mitmdump with production `ignore_hosts` and verifies the **EFFECT** at the TCP layer: `oauth2.googleapis.com:443` tunnels (real cert) while a non-auth host is intercepted (proxy CA).

## Why this wasn't auto-fixed

The spawn-process test needs the existing harness conventions; straightforward but worth landing deliberately.

## Source

Code review of PR #210, finding #7 (P2, testing, conf 100). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in 2c25821f: TestAuthHostFailClosedGate spawns run_proxy with a dropped auth host and asserts it refuses to start (started_event unset, FATAL logged).
