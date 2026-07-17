"""Pure tests for the segmentation outcome reason picker (U2, honest status)."""

from __future__ import annotations

import pytest

from screencap.segmentation.outcome import (
    COULDNT_RUN,
    IN_PROGRESS,
    MECHANICAL_ONLY,
    NOTHING_TO_NAME,
    PRODUCED_TASKS,
    PRODUCED_TASKS_PARTIAL,
    Branch,
    pick_reason,
)

# Pure logic, Vision-free — must run on CI's privacy lane (SCR-275 U6).
pytestmark = pytest.mark.privacy


@pytest.mark.parametrize("is_live", [True, False])
def test_produced_always_produced(is_live):
    assert pick_reason(branch=Branch.PRODUCED, is_live=is_live, prior=None) == PRODUCED_TASKS


@pytest.mark.parametrize("is_live", [True, False])
def test_mechanical_always_mechanical(is_live):
    assert pick_reason(branch=Branch.MECHANICAL, is_live=is_live, prior=None) == MECHANICAL_ONLY


def test_nothing_settles_on_finalize_but_is_provisional_live():
    assert pick_reason(branch=Branch.NOTHING, is_live=False, prior=None) == NOTHING_TO_NAME
    assert pick_reason(branch=Branch.NOTHING, is_live=True, prior=None) == IN_PROGRESS


def test_failed_settles_on_finalize_but_is_provisional_live():
    assert pick_reason(branch=Branch.FAILED, is_live=False, prior=None) == COULDNT_RUN
    assert pick_reason(branch=Branch.FAILED, is_live=True, prior=None) == IN_PROGRESS


@pytest.mark.parametrize("branch", [Branch.MECHANICAL, Branch.NOTHING, Branch.FAILED])
@pytest.mark.parametrize("is_live", [True, False])
def test_monotonic_never_downgrades_from_produced(branch, is_live):
    assert pick_reason(branch=branch, is_live=is_live, prior=PRODUCED_TASKS) == PRODUCED_TASKS


def test_produced_overrides_any_prior():
    assert pick_reason(branch=Branch.PRODUCED, is_live=True, prior=MECHANICAL_ONLY) == PRODUCED_TASKS
    assert pick_reason(branch=Branch.PRODUCED, is_live=False, prior=IN_PROGRESS) == PRODUCED_TASKS


def test_only_produced_is_sticky():
    # A prior mechanical/in_progress is NOT monotonic — only produced is. A finalize
    # NOTHING after a live mechanical still settles to nothing_to_name.
    assert pick_reason(branch=Branch.NOTHING, is_live=False, prior=MECHANICAL_ONLY) == NOTHING_TO_NAME


# ---------------------------------------------------------------------------
# SCR-275 U6 / KTD-8 — produced_tasks_partial: PRODUCED > PARTIAL > MECHANICAL.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_live", [True, False])
def test_partial_records_partial_with_no_prior(is_live):
    assert (
        pick_reason(branch=Branch.PARTIAL, is_live=is_live, prior=None)
        == PRODUCED_TASKS_PARTIAL
    )


@pytest.mark.parametrize("is_live", [True, False])
def test_produced_pass_upgrades_partial(is_live):
    # A later all-model pass upgrades partial → produced (live or finalize).
    assert (
        pick_reason(branch=Branch.PRODUCED, is_live=is_live, prior=PRODUCED_TASKS_PARTIAL)
        == PRODUCED_TASKS
    )


@pytest.mark.parametrize("is_live", [True, False])
def test_mechanical_never_downgrades_partial(is_live):
    assert (
        pick_reason(branch=Branch.MECHANICAL, is_live=is_live, prior=PRODUCED_TASKS_PARTIAL)
        == PRODUCED_TASKS_PARTIAL
    )


def test_finalize_partial_never_downgrades_produced():
    # At finalize the order is monotonic: produced > partial (KTD-8).
    assert (
        pick_reason(branch=Branch.PARTIAL, is_live=False, prior=PRODUCED_TASKS)
        == PRODUCED_TASKS
    )


def test_live_partial_recomputes_over_prior_produced():
    # KTD-8 live recompute: a live pass with a mixed result records partial even
    # if produced was recorded earlier (the window set grew and a new window
    # went mechanical).
    assert (
        pick_reason(branch=Branch.PARTIAL, is_live=True, prior=PRODUCED_TASKS)
        == PRODUCED_TASKS_PARTIAL
    )


@pytest.mark.parametrize("branch", [Branch.NOTHING, Branch.FAILED])
@pytest.mark.parametrize("is_live", [True, False])
def test_nothing_failed_never_downgrade_partial(branch, is_live):
    # Monotonic protection: a provisional/failed pass never drops partial.
    assert (
        pick_reason(branch=branch, is_live=is_live, prior=PRODUCED_TASKS_PARTIAL)
        == PRODUCED_TASKS_PARTIAL
    )


@pytest.mark.parametrize("branch", [Branch.NOTHING, Branch.FAILED])
def test_live_nothing_failed_without_prior_still_in_progress(branch):
    # Unchanged by U6: live NOTHING/FAILED with no sticky prior → in_progress.
    assert pick_reason(branch=branch, is_live=True, prior=None) == IN_PROGRESS
    assert pick_reason(branch=branch, is_live=True, prior=MECHANICAL_ONLY) == IN_PROGRESS
