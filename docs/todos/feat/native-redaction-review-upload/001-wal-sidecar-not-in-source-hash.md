---
title: "P2: Reuse-guard source hash omits the WAL sidecar; upload checkpoint rewrites scrubbed db"
status: open
priority: medium
created: 2026-06-05
source: code-review (ce-code-review autofix, finding adversarial #2 / kieran KP-05)
related_plans:
  - docs/plans/2026-06-03-002-feat-native-redaction-review-upload-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260605-103121-cr/
---

# P2: WAL-mode blind spot in the reuse-guard source hash

## Problem

`src/screencap/scrubber.py` `_compute_source_hash` (`_SOURCE_HASH_GLOBS`) includes `recording.db` but **not** `recording.db-wal` / `recording.db-shm`. Two consequences:

1. **Stale-reuse blind spot.** A source change confined to the WAL (`-wal`) leaves `recording.db` bytes unchanged, so `is_scrubbed_copy_reusable` returns `True` and a scrubbed copy built from the *pre-WAL* DB state ships. (Committed-but-uncheckpointed pages are not yet in `recording.db`.)
2. **Reviewed-db ≠ uploaded-db bytes.** When reuse is correctly rejected and a fresh scrub runs, `upload_recording` calls `_wal_checkpoint(scrubbed_dir)` → `PRAGMA wal_checkpoint(TRUNCATE)` (`upload.py:138`), rewriting the scrubbed copy's `recording.db` *after* review. The committed *content* is identical (same scrubbed rows), but the bytes diverge — so the "byte-identical reviewed == uploaded" claim has a documented exception for `recording.db`.

Both are within the SCR-64 same-EUID trust boundary and content-equivalent, so not a leak — but worth closing for the integrity guarantee.

## What's needed

- Checkpoint the source DB to a stable state before hashing (and before copytree), **or** add `recording.db-wal`/`-shm` to `_SOURCE_HASH_GLOBS` so a WAL-only change invalidates reuse.
- Either way, document `recording.db`'s byte-divergence-but-content-equivalence in the reuse-guard docstring if the upload-time checkpoint stays.
- Add a test: a WAL-mode source DB whose committed bytes are unchanged but `-wal` differs is **not** reused.

## Why deferred

Touches the WAL/checkpoint interaction with the upload path; needs a deliberate decision (checkpoint-before-hash vs hash-the-sidecars) and a WAL-mode test fixture. Not a leak, so out of the critical fix scope.
</content>
