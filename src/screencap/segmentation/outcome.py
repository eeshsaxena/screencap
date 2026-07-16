"""Per-recording segmentation OUTCOME reasons + the pure reason picker (U2, honest status).

The daemon records, per recording, WHY it has (or lacks) AI-named tasks, so the app
can render a distinct honest state instead of an ambiguous empty task list. The picker
is pure and monotonic: once a recording has produced real AI tasks, a later live
(incremental) pass never downgrades it to a mechanical/empty reason.

Reasons — the four the daemon can observe directly at segmentation time. R7's fifth
honest state, ``not_set_up``, plus the ``unknown`` absence state, are derived app-side
(the daemon never probes Apple-Intelligence availability and never invents a reason for
a recording it did not segment):

- ``produced_tasks`` — a provider (on-device or consented cloud fallback) named the day.
- ``mechanical_only`` — degraded to the idle-gap heuristic; names are mechanical, not AI.
- ``nothing_to_name`` — a provider ran and declined (no usable tasks) on a finalized recording.
- ``couldnt_run`` — the attempt failed / was unavailable and no fallback produced anything.
- ``in_progress`` — a live/incremental recording whose current pass produced nothing yet.
"""

from __future__ import annotations

import enum

PRODUCED_TASKS = "produced_tasks"
MECHANICAL_ONLY = "mechanical_only"
NOTHING_TO_NAME = "nothing_to_name"
COULDNT_RUN = "couldnt_run"
IN_PROGRESS = "in_progress"

ALL_REASONS = frozenset(
    {PRODUCED_TASKS, MECHANICAL_ONLY, NOTHING_TO_NAME, COULDNT_RUN, IN_PROGRESS}
)


class Branch(enum.Enum):
    """The raw segmentation outcome observed at a terminal_stage branch point."""

    PRODUCED = "produced"      # real provider tasks, or a consented cloud-fallback naming
    MECHANICAL = "mechanical"  # idle-gap heuristic tasks (``source: idle_gap_heuristic``)
    NOTHING = "nothing"        # a provider ran and declined (``DegradeAction.NONE``)
    FAILED = "failed"          # an exception, or degraded with no fallback output at all


def pick_reason(*, branch: Branch, is_live: bool, prior: str | None) -> str:
    """Map a raw branch outcome to a persisted reason, monotonically.

    Monotonicity (R7 / KTD2): once ``produced_tasks`` is recorded, a later pass that
    is not itself a PRODUCED result keeps ``produced_tasks`` — a live degrade never
    downgrades a recording that already got real AI tasks. On a live recording a
    NOTHING/FAILED pass is provisional (``in_progress``); only on finalize does it
    settle to ``nothing_to_name`` / ``couldnt_run``.
    """
    if prior == PRODUCED_TASKS and branch is not Branch.PRODUCED:
        return PRODUCED_TASKS
    if branch is Branch.PRODUCED:
        return PRODUCED_TASKS
    if branch is Branch.MECHANICAL:
        return MECHANICAL_ONLY
    if is_live:
        return IN_PROGRESS
    return NOTHING_TO_NAME if branch is Branch.NOTHING else COULDNT_RUN
