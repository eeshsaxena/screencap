# SCR-186 frame.nearest — engineering follow-ups

Out-of-scope cleanups surfaced by the `/simplify` review of this branch. Each is a
pre-existing pattern the SCR-186 change brushed against; fixing them properly
touches code outside this PR's scope, so they are deferred here.

## 1. Generalize `ValidationError → 400` across the read verbs

`frame.nearest` returns a typed `400 invalid_request` when a request body fails
Pydantic validation (via the shared `_validation_error_response` helper in
`src/screencap/daemon/app.py`). Its siblings still **500** on the same failure:
`content_search` / `transcript_search` / `timeline_query` call `model_validate`
inside the broad `try`, so a `ValidationError` (e.g. an over-length `query`) falls
through to `_internal_error_response` → HTTP 500. That is a client error reported
as an internal error.

**Fix:** route every read verb's `ValidationError` through
`_validation_error_response` (the seam already exists), or register a Starlette
exception handler in `build_app()`. Update the tests that currently assert 500 on
a validation failure (`tests/daemon/test_read_only_verbs.py`). This changes three
verbs' wire contracts, so it wants its own PR.

## 2. Harden the frame.nearest fail-closed path against canonical/coverage divergence (security, P1 / conf-50)

`frame_blocked.build_is_blocked` is fail-closed on a missing/unreadable
`recording.db` and on errors that raise, but it inherits one gap from the shared
derivation: `scrubber.build_scrub_context` returns an **empty** canonical block
set on a *partial* internal read error **without raising** (it "fails gracefully").
Because an empty canonical set is also the legitimate shape for an all-`ALLOW`
recording, the resolution layer cannot distinguish the two — so a narrow
partial-`recording.db`-read (canonical pass empties while the separate raw-SQL
ambiguity read still yields `window_starts`) could classify a genuinely-masked
frame (MASK_WINDOW/EXCLUDE with non-NULL columns) as `ALLOW` and resolve its stem.

Reachability is narrow (requires one of two reads on the same DB file to fail
while the other succeeds) and the trust boundary is same-EUID, so this is conf-50,
not a straightforward exploit. But `frame.nearest` is the documented fail-*closed*
surface, so the inherited fail-open-on-partial-canonical-read residual should be
closed.

**Fix (cross-cutting, shared infra):** plumb a success/failure signal from
`build_scrub_context` (and through `backfill.skip_intervals.derive_skip_intervals`)
so `build_is_blocked` can fall back to the `_always_blocked` sentinel when the
canonical pass failed rather than silently returning empty. A local fix in
`frame_blocked` is not possible (empty canonical == all-ALLOW is genuine). Add a
test injecting canonical-empty-while-window-rows-exist and assert `frame.nearest`
returns a miss. SECURITY.md's SCR-186 section documents this residual.

## 3. Dedup the screenshot-lister and chunk-manifest reader

Both were already duplicated before SCR-186; this change added a third copy of
each:

- **Screenshot glob/parse/sort** — `frame_resolve.load_frames`,
  `index_core.index_range` (lines ~187-194), and
  `backfill/engine._screenshot_timestamps` all run the same
  `glob("*.jpg")` → `parse_screenshot_timestamp` → sort loop. Candidate: a shared
  `iter_screenshot_timestamps(dir)` next to `parse_screenshot_timestamp` in
  `redaction/geometry.py`. (`frame_resolve` additionally needs the away-from-zero
  `ms` + stem, so it would wrap the shared lister.)
- **Chunk-manifest timing reader** — `app._chunk_timing`,
  `backfill/engine._enumerate_chunks`, and `viewer.py` each open
  `chunk_{idx:04d}_manifest.json` and pull `chunk_start`/`chunk_end`. Candidate: a
  shared `read_chunk_timing(rec_dir, idx)` in `task_manifest.py` (which already
  *writes* those keys), keeping the manifest-key contract in one place.

Deferred because consolidating requires migrating `index_core` / `backfill` /
`viewer` (out of this PR's scope) to net-reduce duplication rather than add a
fourth path.
