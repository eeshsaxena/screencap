---
title: "P2: logout() returns False (mislabels success) when Keychain deletion fails, while the credential persists"
status: resolved
priority: medium
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #11)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P2: logout() returns False on a Keychain-delete failure

## Problem

`src/screencap/auth.py:456` — `logout`:

```python
try:
    _delete_refresh_token()
except Exception:
    return False
return had_token
```

`_delete_refresh_token` already swallows `PasswordDeleteError` (nothing stored). So this `except` only fires when deletion **fails for some other reason** (Keychain locked, backend error) — yet it returns `False`, which the CLI prints as *"No stored credentials (already signed out)."* The refresh token is still in the Keychain, so the user is told they're signed out when they're not.

## What's needed

Log the deletion failure and return `had_token` regardless, so the caller is told a credential existed even when deletion failed (don't claim "nothing stored"):

```python
try:
    _delete_refresh_token()
except Exception:
    logger.warning("Keychain refresh-token deletion failed; credential may persist")
return had_token
```

Consider surfacing a distinct CLI message for "couldn't fully sign out." Add a test for `_delete_refresh_token` raising a non-`PasswordDeleteError`.

## Why this wasn't auto-fixed

Mechanical (`safe_auto`), but bundled to the branch todos per the review's file-tickets routing rather than auto-applied to your PR.

## Source

Code review of PR #210, finding #11 (P2, kieran-python, conf 75). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in fad73466: logout() logs the deletion failure and returns had_token regardless. Test: test_logout_returns_true_when_delete_fails_but_token_present.
