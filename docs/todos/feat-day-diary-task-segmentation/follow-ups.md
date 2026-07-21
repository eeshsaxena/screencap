# Day Diary Task Segmentation — follow-ups

PR-scoped engineering follow-ups discovered while implementing
`docs/plans/2026-07-20-003-feat-day-diary-task-segmentation-plan.md`.
None block the backend landing; all are additive.

## Native on-device prose verbs (unblock real on-device bullets/narrative)

The Python seams are wired, tested, and fail-open, but two native
`IntelligenceHelper` (Apple Intelligence) verbs do not exist yet, so on-device
prose currently degrades to the honest heuristic (app/window-level bullets; a
names+bullets heuristic narrative marked `no-model`). Cloud-consented enrichment
is likewise gated on these seams.

- **`call_block_bullets`** — on-device per-block bullet generation over the block
  digest. Consumed by `segmentation/consolidate.py` via
  `providers/ondevice.py`. Until it lands, `_build_bullet_provider` returns
  `None` and bullets come from the heuristic floor (R6/R15 honest posture).
- **`call_day_narrative`** — on-device day-narrative composition over
  names+bullets+rollups. Consumed by `segmentation/narrative.py` via
  `terminal_stage._build_narrator`. Until it lands, the evidence-bound heuristic
  narrative is composed instead.

Both native verbs must keep the sanitizer + evidence-bound contracts the Python
side already enforces. Note the grounding-prompt duplication rule: any prompt
shared with on-device Chat lives in BOTH `segmentation/generation_finish.py`
and `macos/IntelligenceHelper/main.swift`.

## `diary.search` paywall posture — product decision

`/v0/diary.search` (U6) is currently subscription-gated exactly like
`content.search` (`_check_subscription_for_recall`) — a no-op unless the local
paywall is enforced (default off). The plan-stage doc review (design-lens, P1)
flagged that diary search must NOT be paywalled, since R5 ("findable in under a
minute") is a core, always-on requirement. The write/index side is already
free-by-default (independent of `content_index_enabled`); the open question is
whether the READ verb should also be ungated so search stays free even when the
local paywall is enabled. Resolve before enabling the local paywall.

## Swift compile/test verification (U8, U9)

U8/U9 Swift is code-complete with XCTest coverage but was NOT compiled in the
implementation session: running `xcodebuild` under `~/Documents` TCC-bricks the
session. Verify in the macOS app build environment (the standard app-release
flow). Cross-language JSON contracts were verified field-for-field against
`daemon/schema.py`; the risk surface is pure-Swift compile correctness. Subagent
flags to check: the provisional `is_open` styling vs. the intended
`LiveTaskDraft` look; the literal "today" in the thread chip inside past
day-groups; a couple of minor Swift literal-inference spots noted in the U8/U9
reports.

## Moments merge interplay (cross-plan) — PARTIALLY DONE in the merge

The Moments merge (#429) + dedicated task view (#430) landed on `main` and
DELETED `Views/Tasks/TasksView.swift` (the surface U8 targeted). Resolving the
merge did the KTD-12 re-base:

- **Done:** the diary row rendering (expandable topic bullets, the thread rollup
  chip, the live `is_open` pill) was ported onto the Moments `AutoRow`
  (`Views/Moments/MomentRowViews.swift`). The narrative section (`DayTimelineView`)
  and the morning resume card (`DaysView`) survived the merge unchanged. Diary
  data flows through the shared `TasksModel.TaskRow` the Moments list consumes.
- **Deferred:** (1) the cross-history **diary search UI entry** — `DiarySearchModel`
  + `DaemonClient.diarySearch` + the `/v0/diary.search` verb + FTS index all exist
  and are tested (`DayDiaryTests`), but the model is not yet wired into a live
  surface; `MomentsView` has only its own loaded-rows substring filter, so
  cross-history fuzzy recall (R5) needs a UI entry there. (2) The thread chip is
  informational on the Moments row; the same-day **scroll-to-sibling** tap
  (present in the old `TaskRowView`) was not re-plumbed. (3) `DiarySearchModel`
  currently has no non-test consumer — either wire it into Moments or fold it in
  when the search UI lands.
- All of the above is **compile-unverified Swift** (the port + the survivors) —
  verify in the macOS build.

## Deferred by the plan (not this PR)

Cross-day threads; Recall/Chat grounding on diary blocks (a 4th evidence stream
— must land in both `generation_finish.py` and `IntelligenceHelper/main.swift`);
bulk backfill of historical recordings (must follow the recording-name-free
opaque-ordinal progress contract); exposing the narrative over MCP.

## Code-review follow-ups (deferred; the applied fixes shipped in fix(review))

Findings the multi-agent review surfaced that were NOT applied this PR (the
applied set — cross-process diary lock, name sanitizer, halt budget/stop_event,
CI test markers, narrative comment, thread-chip copy — is in the `fix(review)`
commit):

- **Narrative regeneration after a failed invalidation (P2 defence-in-depth).**
  `scrub_worker._invalidate_day_narrative` clears the narrative fail-open; if the
  clear fails, a bullets-only purge on a protected row does not change the
  consolidation fingerprint, so the next tick may skip regeneration. The primary
  clear->NULL path is privacy-safe. Fix: fold a purge/disable signal into the
  consolidation fingerprint (or reset `_last_consolidation_key`) so a purge forces
  the next tick to re-consolidate. Cross-process (scrub is recorder-subprocess,
  the fingerprint is daemon-side), so it wants its own change + test.
- **block_id reuse on shared-app alone (P3).** `consolidate.py` reuses a prior
  `block_id` when blocks share an app even without name-token overlap; on
  browser-heavy days a stable id can re-point a deep link / morning-card jump onto
  different work. Require positive name-token overlap before reuse.
- **`update_task_segment` sets `EDITED_FIELD_BULLETS` on ANY metadata write.**
  Benign today (`tasks.update` never passes metadata), but a future full-metadata
  edit path would over-protect agent bullets and defeat the AE5 purge. Set the
  bullets bit only when bullets actually changed.
- **`narrative._thread_rollups` groups by `thread_id` alone**, not the composite
  `(recording, thread_id)` used in `tasks_query`. Safe today (per-recording), a
  latent false-merge if ever fed multi-recording blocks — harden to the composite.
- **`supervisor._segmentation_fingerprint` catch** `(OSError, ValueError,
  IndexError)` does not cover `sqlite3.OperationalError` from the widened
  `read_task_segments` SELECT on a legacy pre-U1 `recording.db`; narrow (the active
  ambient DB carries the current schema) but `_consolidation_fingerprint` has the
  broad backstop and this one doesn't.
- **Test hygiene:** the renamed-block bullet-regeneration test never passes the
  renamed row as `prior_rows` (name overpromises); add a model-provider injection
  test for a merged NAME (only bullets are covered today); add a `diary.search`
  FTS-query-injection test and real-daemon round-trip tests for the new MCP tools.
