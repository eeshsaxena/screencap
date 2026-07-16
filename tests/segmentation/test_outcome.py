"""Pure tests for the segmentation outcome reason picker (U2, honest status)."""

from __future__ import annotations

import pytest

from screencap.segmentation.outcome import (
    COULDNT_RUN,
    IN_PROGRESS,
    MECHANICAL_ONLY,
    NOTHING_TO_NAME,
    PRODUCED_TASKS,
    Branch,
    pick_reason,
)


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
