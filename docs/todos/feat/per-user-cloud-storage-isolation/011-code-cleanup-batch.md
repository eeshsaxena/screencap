---
title: "P3: Code-cleanup batch — type hints, module imports, handler dedup, demo-list name guard, UserDisabledError"
status: resolved
priority: low
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (findings #14–#20)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P3: Mechanical cleanup batch (mostly safe_auto)

A bundle of low-risk polish items from the PR #210 review. Each is independently small; grouped so they can land in one cleanup pass.

- [ ] **#14 — Type the `request` param on the auth boundary.** `scripts/cloud-function/auth.py:54` — `verify_bearer`/`_extract_bearer` take an untyped `request`. Add a minimal `Protocol` (e.g. `class _HasHeaders(Protocol): headers: Mapping[str, str]`) or annotate, documenting the duck-typed contract.
- [ ] **#15 — Catch `UserDisabledError` in `verify_bearer`.** `scripts/cloud-function/auth.py:90` — add `fb_auth.UserDisabledError` to the `AuthInvalid` except tuple. Can't fire today (`check_revoked=False`) but closes the gap before the deferred abuse-control change enables revocation; otherwise it becomes an unhandled 500.
- [ ] **#16 — Annotate `_authenticate()` return type.** `scripts/cloud-function/main.py:167` — `-> tuple[str, None] | tuple[None, tuple]` so the (uid,None) success vs (None,response) failure contract is type-checked (a handler that forgets `if err: return err` would pass `uid=None` to `resolve_prefix`).
- [ ] **#17 — Dedup the blob-list/sign loops.** `scripts/cloud-function/main.py:186` — extract `_list_blobs_as_recordings(prefix, honor_unlisted)` and `_sign_blobs(prefix, expiration)`; keep the separate `resolve_prefix` calls (namespace separation) and surface the `_unlisted` divergence (see todo #003) as an explicit parameter. `gated_auto` — confirm the demo/user code-path separation intent before consolidating.
- [ ] **#18 — Hoist module-local imports.** `scripts/cloud-function/main.py:194` (`from collections import defaultdict`, repeated in both list handlers) and `src/screencap/network/proxy_runner.py:154` (`from screencap.network.blocklist import missing_required_auth_hosts`) — move to module scope; no startup-cost reason to defer.
- [ ] **#19 — Validate names in `_handle_demo_list`.** `scripts/cloud-function/main.py:207` — apply the same `_NAME_RE` guard `demo-sign-download` enforces, so a demo recording can't be *listable but unplayable* (lists fine, then 400s on sign-download). Alternatively reject/rename such names in U8 promotion.
- [ ] **#20 — `whoami()` → `TypedDict`.** `src/screencap/auth.py:463` — the `_AUTH_SCHEMA_VERSION`-tagged shape is a versioned cross-layer contract (consumed by the SwiftUI shell); use a `TypedDict(total=False)` so the shape is machine-checkable.

## Source

Code review of PR #210, findings #14–#20 (all P3, conf 75; kieran-python / maintainability / reliability / adversarial). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in 40743270 (#14 Protocol, #15 UserDisabledError, #16 _authenticate annotation, #17 helper extraction, #18 defaultdict hoist, #19 demo-list name guard) and fad73466 (#20 whoami TypedDict). #18's proxy_runner import was consolidated into the existing deferred import (2c25821f) rather than hoisted to module scope, per the file's defer-heavy-imports convention.
