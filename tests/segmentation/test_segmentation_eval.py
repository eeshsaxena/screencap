"""Segmentation eval scoring logic (U12, KTD10, SCR-239).

The real-model run needs a downloaded model on real hardware (manual); CI covers
the deterministic scoring: preference over the heuristic, the wrong-name-rate
mislead check, and that the confidence gate protects the "never mislead" bar for
low-confidence AND field-omission cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The eval driver lives under benchmarks/ (not a package) — add it to the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from benchmark_segmentation import (  # noqa: E402
    beats_heuristic,
    heuristic_baseline,
    score_run,
)


def _task(name, confidence="high"):
    t = {"name": name, "start_ts": 0.0, "end_ts": 60.0}
    if confidence is not None:
        t["confidence"] = confidence
    return t


_LABELS = [{"acceptable": ["Fix login", "Login fix"]}, {"acceptable": ["Draft roadmap"]}]


class TestScoring:
    def test_all_correct_high_confidence(self):
        raw = [_task("Fix login"), _task("Draft roadmap")]
        score = score_run(raw, _LABELS, "low")
        assert score.correct == 2 and score.wrong == 0
        assert score.correct_rate == 1.0 and score.wrong_name_rate == 0.0

    def test_better_set_ranks_above_worse_set(self):
        better = score_run([_task("Fix login"), _task("Draft roadmap")], _LABELS, "low")
        worse = score_run([_task("Fix login"), _task("Play games")], _LABELS, "low")
        assert better.correct_rate > worse.correct_rate

    def test_mislead_flagged_by_wrong_name_rate(self):
        # A fabricated wrong name at high confidence is surfaced → counts as wrong.
        raw = [_task("Fix login"), _task("Order pizza")]  # 2nd is wrong
        score = score_run(raw, _LABELS, "low")
        assert score.wrong == 1
        assert score.wrong_name_rate == 0.5


class TestGateProtectsMisleadBar:
    def test_low_confidence_wrong_name_is_blanked_not_wrong(self):
        # A wrong name the model was UNSURE about is gated to unnamed → not a
        # mislead (wrong_name_rate stays 0).
        raw = [_task("Fix login"), _task("Order pizza", confidence="low")]
        score = score_run(raw, _LABELS, "low")
        assert score.wrong == 0 and score.unnamed == 1
        assert score.wrong_name_rate == 0.0

    def test_missing_confidence_wrong_name_is_blanked(self):
        # Field-omission (no confidence) → fail-closed blank → not surfaced/wrong.
        raw = [_task("Fix login"), _task("Order pizza", confidence=None)]
        score = score_run(raw, _LABELS, "low")
        assert score.wrong == 0 and score.unnamed == 1
        assert score.wrong_name_rate == 0.0

    def test_high_confidence_wrong_name_still_counts(self):
        # The gate only protects LOW confidence; a confident wrong name is a real
        # mislead the eval must surface (→ logprob gating promoted if it can't hit 0).
        raw = [_task("Fix login"), _task("Order pizza", confidence="high")]
        assert score_run(raw, _LABELS, "low").wrong_name_rate == 0.5


class TestHeuristicComparison:
    def test_heuristic_baseline_is_deterministic(self):
        base = heuristic_baseline(_LABELS)
        assert base.correct == 0 and base.wrong == 0 and base.correct_rate == 0.0

    def test_beats_heuristic_true_when_correct_and_clean(self):
        model = score_run([_task("Fix login"), _task("Draft roadmap")], _LABELS, "low")
        assert beats_heuristic(model, heuristic_baseline(_LABELS)) is True

    def test_beats_heuristic_false_when_any_wrong(self):
        model = score_run([_task("Fix login"), _task("Order pizza")], _LABELS, "low")
        # A wrong name fails the "never mislead" half even if it beats on coverage.
        assert beats_heuristic(model, heuristic_baseline(_LABELS)) is False

    def test_threshold_calibration_reproducible(self):
        raw = [_task("Fix login", "medium"), _task("Draft roadmap", "medium")]
        # At threshold 'low', medium survives; at 'medium', both are blanked.
        assert score_run(raw, _LABELS, "low").surfaced == 2
        assert score_run(raw, _LABELS, "medium").surfaced == 0
        # Reproducible.
        assert score_run(raw, _LABELS, "low") == score_run(raw, _LABELS, "low")
