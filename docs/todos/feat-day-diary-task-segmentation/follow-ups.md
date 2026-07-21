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

## Moments merge interplay (cross-plan)

`docs/plans/2026-07-20-002-feat-moments-merge-tasks-clips-plan.md` will replace
the Tasks/Days surface U8 targeted. Per KTD-12 the diary schema/wire landed
first; when Moments ships, re-base its row model on blocks (block_id/thread_id/
bullets/rollups) and re-target U8's day-view/search entry onto the Moments
surface.

## Deferred by the plan (not this PR)

Cross-day threads; Recall/Chat grounding on diary blocks (a 4th evidence stream
— must land in both `generation_finish.py` and `IntelligenceHelper/main.swift`);
bulk backfill of historical recordings (must follow the recording-name-free
opaque-ordinal progress contract); exposing the narrative over MCP.
