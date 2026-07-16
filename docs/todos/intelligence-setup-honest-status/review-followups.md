# Review follow-ups — intelligence-setup-honest-status (daemon half, U1–U3)

Deferred findings from the `ce-code-review` pass on the daemon half. Promote to
Linear (Screencap team) when on an authenticated session — Linear was not reachable
in the session that produced this branch.

## 1. Outcome ↔ task-row atomicity crash-window (P2, robustness)

**Source:** adversarial reviewer (confidence 65), corroborated by reliability.

The per-recording `pipeline_recording_outcome.reason` is the authority the monotonic
row guard in `terminal_stage._run_local_segmentation` consults, but it is written in a
**separate transaction, AFTER** `_persist_local_tasks`. A crash/kill (or a swallowed
`BEGIN IMMEDIATE` lock-loss) in the window between persisting AI task rows and writing
`produced_tasks` leaves the rows on disk with a stale/absent outcome. On the next
`mechanical_only` incremental tick the guard reads not-`produced_tasks`, skips the
row-protection, and overwrites the real AI rows with idle-gap-heuristic rows —
`tasks.list` then transiently reports a reason that disagrees with the rows (the exact
dishonesty this feature exists to prevent).

Blast radius is bounded (the supervisor resume/live catches wrap `run_terminal_stage`,
so no lost completion) and the window is crash-only, which is why it is deferred rather
than hot-fixed.

**Note:** the mechanical-vs-AI distinction is *not* derivable from the task rows (both
persist as `source='agent'`), which is precisely why the separate outcome field exists —
so a naive "gate on the rows instead" does not work. The correct fix is a co-transactional
write of the task rows **and** the outcome (single `recording.db` transaction), or a
reason derived atomically with the rows. This needs a considered design, not an
apply-pass reorder (recording produced-before-persist just trades one inconsistency for a
milder "produced but empty" one).

## 2. `/v0/intelligence.status` API hygiene (P2, consistency)

**Source:** api-contract reviewer (confidence 78).

The new verb reuses `_MODELS_API_VERSION` (the `model.*` verb family's constant) and
has no dedicated `IntelligenceStatusResponse` / `VerdictInputs` schema model — so an
`intelligence.status` change would force a `model.*` version bump, and the verb lacks
the documented wire contract every other verb has. Add a dedicated
`_INTELLIGENCE_STATUS_API_VERSION` constant and a registered response schema model
mirroring the existing sub-object pattern. Functional today (follows the `model.status`
precedent), so deferred as a cleanup.

## Deferred test coverage (nice-to-have)

- `set_recording_outcome` against an actual read-only (chmod 444) `recording.db`
  (only the missing-table read path is directly asserted today).
- `intelligence_status` handler error path (`intelligence_verdict_inputs` raising →
  `_internal_error_response`).
- The 5 new test files carry no `@pytest.mark.privacy`, so CI's `pytest -m privacy`
  lane will not run them — consistent with the rest of the non-privacy suite, but worth
  noting for CI coverage of this code.
