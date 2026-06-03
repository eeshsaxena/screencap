---
title: "P1: Cross-process refresh-token lost-rotation can brick a long-lived signed-in process"
status: resolved
priority: high
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #3)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P1: Refresh-token lost-rotation across processes

## Problem

`src/screencap/auth.py:404` — inside `_refresh_lock`, `_ensure_fresh` prefers the in-memory token:

```python
refresh_token = (_cached.refresh_token if _cached else None) or _load_refresh_token()
```

Firebase rotates refresh tokens. If process A refreshes and persists a new refresh token to the Keychain, a long-lived process B that already holds a stale `_cached.refresh_token` will, on its next refresh, send the **old** token instead of re-reading the Keychain. Firebase rejects the superseded token → `_refresh` maps the non-200 to `NotSignedIn` → process B is force-logged-out **while a valid refresh token sits in the Keychain**.

Latent today: nothing long-lived calls `get_id_token()` yet (short-lived CLI processes start with `_cached=None` and always read the Keychain). It **becomes reachable once U5 wires the token into the daemon / app-shell**, which are exactly the long-lived processes.

## What's needed

Inside the lock, after the `expires_at` re-check fails, always reload the Keychain value and prefer it when it differs from the in-memory token (the Keychain is the rotation source of truth). On a `NotSignedIn` from `_refresh`, retry once with the freshly-loaded Keychain token before surfacing `NotSignedIn`. Add a test exercising divergent tokens (in-memory `RT_old` vs Keychain `RT_new`) across `_refresh_lock`.

## Why this wasn't auto-fixed

Changes the refresh control flow and needs a new concurrency test; warrants author review before landing. `requires_verification: true`.

## Source

Code review of PR #210, finding #3 (P1, adversarial conf 75; corroborated as a residual risk by correctness and kieran-python). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in fad73466: _ensure_fresh reloads the Keychain inside the lock and prefers it over a stale in-memory token, then retries once with a freshly-rotated token before forcing a logout.
