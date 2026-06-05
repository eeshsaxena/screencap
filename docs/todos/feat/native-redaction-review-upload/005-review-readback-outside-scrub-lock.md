---
title: "P1: review-data resolves scrubbed paths after the scrub lock is released (concurrent-rebuild race)"
status: open
priority: high
created: 2026-06-05
source: code-review (ce-code-review, finding adversarial #1 / F2)
related_plans:
  - docs/plans/2026-06-03-002-feat-native-redaction-review-upload-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260605-112111-bcc9fab3/
---

# P1: scrubbed-dir read-back runs outside `recording_scrub_lock`

## Problem

`prepare_review_data` (`src/screencap/review.py`) calls `_prepare_scrubbed_copy` →
`scrub_recording`, which acquires and **releases** `recording_scrub_lock(name)`
internally. The envelope's path resolution then runs *after the lock is gone*:

- `_resolve_scrubbed_event_files(scrubbed_dir)` (review.py ~:265)
- the `screenshots/` glob (review.py ~:277–282)
- `_assert_within_recordings_root` (review.py ~:260)

The review window is a `WindowGroup` (multiple concurrent windows by design), and
`screencap upload` can run alongside it. A second actor's `scrub_recording`
(`rmtree` → `copytree` → scrub → sentinel) can mutate / delete the
`<name>-scrubbed` dir during this read-back window, so the first review either
raises a spurious `ReviewPrepareError` or resolves a half-rebuilt file set.

## What's needed

- Hold `recording_scrub_lock(name)` across the **entire** prepare critical
  section — scrub **and** the path read-back — mirroring the upload path's
  lock span (`src/screencap/cli/__init__.py` ~:2925–2973).
- `scrub_recording` already takes `_already_locked` for nested invocation; the
  cleanest shape is `with recording_scrub_lock(name):` in `prepare_review_data`,
  calling `scrub_recording(..., _already_locked=True)` inside it, then resolving
  paths before releasing.
- Add a test exercising two concurrent `prepare_review_data` calls on one
  recording (or prepare racing an upload re-scrub) asserting no torn read.

## Why deferred

Behavior-changing (lock-scope widening) and needs a concurrency test harness;
the same-EUID window is narrow. Out of the P0 fix scope (F1) already shipped in
this PR.
