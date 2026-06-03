---
title: "P2: SECURITY.md not updated for the new cloud-auth/Keychain trust boundary (dead docstring reference)"
status: resolved
priority: medium
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #8)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P2: SECURITY.md missing the cloud-auth trust boundary

## Problem

CLAUDE.md names `SECURITY.md` the source of truth for threat-model questions, and the plan lists a SECURITY.md "cloud-trust-boundary section" as documentation work for this PR. But:

- `SECURITY.md` is **not in the diff** — it still documents only the daemon-socket boundary.
- `src/screencap/auth.py`'s new module docstring says *"Security posture (see SECURITY.md + docs/runbooks/cloud-auth-setup.md)"* — a **dead reference**; the SECURITY.md content it points to doesn't exist.

(The runbook `docs/runbooks/cloud-auth-setup.md` *was* added, so only the SECURITY.md half is missing.)

## What's needed

Add a "Cloud auth trust boundary" section to `SECURITY.md` covering:
1. Keychain ACL posture — default "Always Allow" trusted-binary ACL (not code-signing-pinned), same as `network/crypto.py`; any same-user trusted binary can read the refresh token. State it honestly; don't claim pinning that doesn't exist.
2. The in-code `resolve_prefix` gate is the only boundary (no Cloud Run IAM backstop, by design) — backed by the CI contract test.
3. Signed-URL expiry windows: `users/` 30 min, `demo/` 4 h; a signed GET URL is an unrevokable capability for its lifetime.
4. (When U5 lands) the daemon receives only a short-lived ID token out-of-band, never the refresh token.

## Why this wasn't auto-fixed

Security-document authorship is a human judgment call (owner: human).

## Source

Code review of PR #210, finding #8 (P2, project-standards, conf 100). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in 74b55568: SECURITY.md 'Cloud auth trust boundary' section added (kills the dead auth.py docstring reference).
