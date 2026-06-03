---
title: "P2: _credentials.refresh runs inside the per-file upload loop — partial batch + bare 500 on mid-loop failure"
status: resolved
priority: medium
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #5)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P2: Per-file credential refresh in _handle_upload

## Problem

`scripts/cloud-function/main.py:371` — `_handle_upload` calls `_credentials.refresh(_auth_request)` **inside** the per-file loop, with no error handling:

```python
for f in files:
    ...
    _credentials.refresh(_auth_request)   # inside the loop, per file
    urls[name] = blob.generate_signed_url(...)
```

Two issues:
1. **Partial batch on failure:** if `_credentials.refresh` (or `generate_signed_url`) raises mid-loop, some URLs were already signed and the request returns a bare 500 with no JSON body — the client can't distinguish a transient signing-credential failure from any other 5xx, and has a partial result.
2. **Cost:** a 500-file recording (MAX_FILES) does up to ~500 IAM `signBlob`-token refreshes in one invocation. The read handlers (`_handle_sign_download`, `_handle_demo_sign_download`) already refresh **once before** the loop.

## What's needed

Hoist `_credentials.refresh(_auth_request)` to a single call before the file loop, matching the read handlers. Wrap it in `try/except google.auth.exceptions.GoogleAuthError` and return a `503` with a JSON error body so the client can tell a transient signing failure apart from other errors. Add a test exercising a refresh failure mid-upload (the FakeGCS harness doesn't currently cover the signing-credential path).

## Why this wasn't auto-fixed

Changes the function's error contract (500 → 503 + body) — a behavior change worth author approval. `gated_auto`.

## Source

Code review of PR #210, finding #5 (P2, reliability conf 100 + adversarial conf 75). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in 40743270: _credentials.refresh hoisted to once before the loop; signing failure -> 503 with a JSON body. Tests: test_upload_signing_credential_failure_returns_503, test_upload_refreshes_signing_credential_once_not_per_file.
