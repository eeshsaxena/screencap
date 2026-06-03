---
title: "P2: PROJECT_ID falls back to ambient GOOGLE_CLOUD_PROJECT — off-default deploy pins the wrong Firebase project"
status: resolved
priority: medium
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #9)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P2: PROJECT_ID ambient fallback footgun

## Problem

`scripts/cloud-function/main.py:76`:

```python
PROJECT_ID = (
    os.environ.get("SCREENCAP_PROJECT_ID")
    or os.environ.get("GOOGLE_CLOUD_PROJECT")   # ambient Cloud Run var
    or "proteus-photos"
)
```

`GOOGLE_CLOUD_PROJECT` is the standard Cloud Run var set to the **hosting** project. If the function is ever deployed in a project other than `proteus-photos` without `SCREENCAP_PROJECT_ID` set (e.g. the dev function — whose deploy command in the docstring sets `SCREENCAP_BUCKET` but **not** `SCREENCAP_PROJECT_ID`), `PROJECT_ID` silently becomes the hosting project. `verify_bearer` then asserts `aud`/`iss` against the wrong project and **rejects all legitimate proteus-photos tokens** → every user is locked out.

The failure direction is fail-closed (lockout, not token-acceptance), which is why this is P2 rather than P1 — but it's a real misconfiguration trap, and the dev function deploy command is already missing the override.

## What's needed

Remove the ambient fallback and make the default explicit:

```python
PROJECT_ID = os.environ.get("SCREENCAP_PROJECT_ID", "proteus-photos")
```

so `SCREENCAP_PROJECT_ID` is the only override. Set `SCREENCAP_PROJECT_ID` explicitly in both deploy commands (prod and dev) in the `main.py` docstring / runbook. Add a test pinning the resolution precedence.

## Why this wasn't auto-fixed

Changes the deploy/env contract; `requires_verification: true` (confirm no deploy relies on the ambient var).

## Source

Code review of PR #210, finding #9 (P2, kieran-python, conf 75; severity adjusted P1→P2 — failure mode is fail-closed lockout). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in 40743270: PROJECT_ID via _resolve_project_id with no ambient GOOGLE_CLOUD_PROJECT fallback; both deploy commands set SCREENCAP_PROJECT_ID. Test: test_resolve_project_id_ignores_ambient_google_cloud_project.
