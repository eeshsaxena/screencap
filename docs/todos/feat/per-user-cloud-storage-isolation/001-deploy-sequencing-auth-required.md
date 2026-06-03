---
title: "P0: Gate the function deploy — auth is now required and get-index is gone, but clients aren't updated yet"
status: resolved
priority: urgent
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (findings #1, #2)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P0: Gate the function deploy until clients carry tokens

## Problem

This PR makes the signing function require a Firebase Bearer token on `upload`/`list`/`sign-download`, removes the `get-index` action, and reshapes `gcs_prefix` to `users/{uid}/…`. The matching client token-threading (U5 remainder) and website demo-repoint (U7) are **deferred to follow-ups**. So there is a window where the deployed function has no updated callers:

- The current CLI (`upload.py` / `download.py`) and the current website send **no** Bearer token → every real-user action returns **401**.
- `scripts/cloud-function/main.py:153` dispatch now 400s any unknown action; `download.py:147` still POSTs `{"action": "get-index"}` (via `fetch_session_index()` → `list_remote_sessions()`), so `screencap list --remote --sessions` returns an error after deploy (finding #2).
- The deploy command in the `main.py` docstring targets the existing prod function name `get-upload-urls --allow-unauthenticated`; running it as written replaces the live function.

The code itself is correct — this is a **deploy-sequencing hazard**, not a code defect. The PR is mergeable; the deploy is not safe in isolation.

## What's needed

Pick one and document it in `docs/runbooks/cloud-auth-setup.md` as a hard pre-deploy gate:

1. **Sequence:** do not deploy to the live `get-upload-urls` until U5 (token threading in `upload.py`/`download.py`) and U7 (website → `demo-*`) land, OR
2. **New function name:** deploy as a separate function and cut clients over only once they send tokens, OR
3. **Feature flag:** a `SCREENCAP_AUTH_ENABLED` env so the deployed code falls through to the legacy path until clients are ready.

For finding #2 specifically: when U5 removes the `--remote`/`--sessions` surface, also remove `fetch_session_index`/`list_remote_sessions` from `download.py`, or have the function return a 404/empty for `get-index` so the CLI degrades loudly rather than erroring opaquely.

## Why this wasn't auto-fixed

The fix is an operational/release decision (deploy ordering, runbook wording) plus a cross-cutting client change tracked under deferred U5/U7 — not a local code edit.

## Source

Code review of PR #210, findings #1 (P0, api-contract, conf 100) and #2 (P1, api-contract, conf 100). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Deploy-sequencing gate added to docs/runbooks/cloud-auth-setup.md and the main.py deploy header (74b55568). Finding #2's client-side removal of fetch_session_index/list_remote_sessions is deferred to U5 as planned (the function already degrades get-index to an empty index).
