---
title: "P2: _unlisted handled inconsistently between demo-list and user-list; user-side suppression untested"
status: resolved
priority: medium
created: 2026-06-03
resolved: 2026-06-03
source: code-review PR #210 (finding #4)
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/
---

# P2: _unlisted divergence between the two list handlers

## Problem

`scripts/cloud-function/main.py` — the `_unlisted` marker is handled two different ways:

- `_handle_demo_list` (line ~208) is **marker-blind**: it skips the `_unlisted` file but still shows the recording.
- `_handle_list` (line ~285) **hides** a recording whose `_unlisted` marker is present (`if not info.get("unlisted")`).

This divergence is intentional (per the plan's Key Technical Decisions) but invisible at the two call sites, and the **user-side suppression has no test** (`test_demo_list_is_marker_blind` covers only the demo side). A future refactor that "unifies" the two handlers would silently change whether a user's `_unlisted` recording is hidden, with no failing test.

## What's needed

1. Add a cross-reference comment in each handler pointing at the other and noting the divergence is deliberate (cite the plan's Key Technical Decisions).
2. Add a test on the user side: a recording carrying `_unlisted` is **absent** from `_handle_list`'s output — anchoring the marker-honoring behavior so the divergence is enforced on both sides.

## Why this wasn't auto-fixed

Needs a new test plus an intent confirmation that the divergence should persist (vs. being part of the `_unlisted` decommission tracked for a later PR).

## Source

Code review of PR #210, finding #4 (P2, maintainability + api-contract, conf 100). Run artifact: `/tmp/compound-engineering/ce-code-review/20260603-114946-c0fa9fb1/`.

## Resolution

Fixed in 40743270: _collect_recordings(honor_unlisted) makes the divergence an explicit parameter + cross-ref comments; user-side hiding test added (test_user_list_hides_unlisted_recording).
