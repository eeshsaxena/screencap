---
title: "P2: OAuth loopback uses a single-shot handle_request — a favicon/preconnect GET can consume the one callback"
status: resolved
priority: medium
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #10)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P2: Loopback OAuth callback is single-shot

## Problem

`src/screencap/auth.py:280` — `_login_via_browser` services exactly one request:

```python
server.handle_request()  # blocks until ONE callback or timeout
```

The browser may issue a non-callback GET to `http://127.0.0.1:<port>` first (favicon, connection pre-warm, a speculative prefetch). That request consumes the single `handle_request()`, the handler captures empty `code`/`state`, and `_validate_callback` then fails the real sign-in — even though the genuine redirect was about to arrive. Intermittent "Sign-in did not complete" failures with no real cause.

## What's needed

Loop `handle_request()` until a request actually carries `code` or `error` (or the deadline passes). Have `do_GET` respond `404` and **not** record a result for any path lacking an OAuth response, so favicon/preconnect probes can't terminate the wait. Add a test that drives the handler with a non-callback request first, then the real callback, and asserts the code is captured.

## Why this wasn't auto-fixed

Changes the login flow's control structure and needs a test that currently mocks `_login_via_browser` wholesale; `requires_verification: true`.

## Source

Code review of PR #210, finding #10 (P2, adversarial, conf 75). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in fad73466: the loopback handler 404s non-OAuth probes (records nothing) and _login_via_browser loops handle_request() until a real callback or the deadline. Test: test_loopback_handler_ignores_non_oauth_probe_then_accepts_callback.
