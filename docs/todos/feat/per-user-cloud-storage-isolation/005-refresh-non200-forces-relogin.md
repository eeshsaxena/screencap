---
title: "P2: Any non-200 from the token-refresh endpoint forces re-login, even transient 429/5xx"
status: resolved
priority: medium
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #6)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P2: Transient refresh failures misclassified as NotSignedIn

## Problem

`src/screencap/auth.py:371` — `_refresh` treats **any** non-200 as a dead credential:

```python
if resp.status_code != 200:
    raise NotSignedIn("Your session has expired. Run `screencap login` again.")
```

A `429` (rate limit) or `5xx` (Google secure-token outage) is transient — the refresh token is still valid — but the user is told to sign in again. The module already has the right taxonomy (`AuthError` = retryable vs `NotSignedIn` = re-auth); this path collapses both into `NotSignedIn`. This matters most once U5 routes `NotSignedIn` vs `AuthError` into the fail-closed live-upload state machine (see `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md`).

## What's needed

Differentiate by status code:

```python
if resp.status_code in (400, 401):
    raise NotSignedIn("Your session has expired. Run `screencap login` again.")
if resp.status_code != 200:
    raise AuthError(f"Token refresh temporarily failed ({resp.status_code}).")
```

Add tests for a 429 and a 5xx asserting `AuthError` (retryable), not `NotSignedIn`.

## Why this wasn't auto-fixed

Changes which exception callers observe (a behavior contract the deferred U5 paths depend on); `requires_verification: true`.

## Source

Code review of PR #210, finding #6 (P2, reliability, conf 100). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in fad73466: _refresh maps 400/401 -> NotSignedIn and 429/5xx -> retryable AuthError. Tests: test_refresh_transient_status_is_autherror_not_notsignedin, test_refresh_4xx_status_is_notsignedin.
