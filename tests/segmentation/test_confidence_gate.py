"""Confidence gate — "never mislead" (U3, R9/KTD9, SCR-239).

The gate blanks the name of any local-model task at/below the threshold
confidence (or with a missing/unparseable value — fail-closed), keeping the task
boundary. Covers AE3.
"""

from __future__ import annotations

import pytest

from screencap import config
from screencap.segmentation.confidence_gate import apply_confidence_gate


def _tasks(*confidences: object) -> dict:
    """A validated-shape tasks dict with one task per given confidence value."""
    return {
        "tasks": [
            {
                "start_ts": 1000.0 + i * 60,
                "end_ts": 1000.0 + (i + 1) * 60,
                "name": f"Task {i}",
                "derived_name": f"task-{i}",
                "confidence": c,
            }
            for i, c in enumerate(confidences)
        ],
        "summary": {},
        "tags": [],
    }


@pytest.mark.privacy
class TestGate:
    def test_low_blanked_high_kept_at_default_threshold(self):
        """Covers AE3: a low-confidence task is unnamed; a high one is unchanged."""
        result = apply_confidence_gate(_tasks("low", "high"), "low")
        low, high = result["tasks"]
        assert low["name"] == "" and low["derived_name"] == ""
        # Boundary preserved — the task is kept, not dropped.
        assert low["start_ts"] == 1000.0 and low["end_ts"] == 1060.0
        assert high["name"] == "Task 1"

    def test_medium_kept_at_low_threshold(self):
        result = apply_confidence_gate(_tasks("medium"), "low")
        assert result["tasks"][0]["name"] == "Task 0"

    def test_low_confidence_blanks_description_too(self):
        """A gated task carries no model-written free text either (never mislead)."""
        tasks = _tasks("low")
        tasks["tasks"][0]["description"] = "Refactored the auth module"
        result = apply_confidence_gate(tasks, "low")
        assert result["tasks"][0]["name"] == ""
        assert result["tasks"][0]["description"] == ""

    def test_high_confidence_keeps_description(self):
        tasks = _tasks("high")
        tasks["tasks"][0]["description"] = "kept — model was confident"
        result = apply_confidence_gate(tasks, "low")
        assert result["tasks"][0]["description"] == "kept — model was confident"

    def test_missing_confidence_is_blanked_failclosed(self):
        """Covers AE3: an absent confidence is treated as below every threshold."""
        tasks = _tasks("high")
        del tasks["tasks"][0]["confidence"]
        result = apply_confidence_gate(tasks, "low")
        assert result["tasks"][0]["name"] == ""

    def test_unparseable_confidence_is_blanked(self):
        result = apply_confidence_gate(_tasks("very-sure"), "low")
        assert result["tasks"][0]["name"] == ""

    def test_all_low_yields_all_unnamed_but_kept(self):
        result = apply_confidence_gate(_tasks("low", "low", "low"), "low")
        assert len(result["tasks"]) == 3  # kept, not dropped
        assert all(t["name"] == "" for t in result["tasks"])

    def test_medium_threshold_blanks_low_and_medium(self):
        result = apply_confidence_gate(_tasks("low", "medium", "high"), "medium")
        names = [t["name"] for t in result["tasks"]]
        assert names[0] == "" and names[1] == ""  # low, medium blanked
        assert names[2] == "Task 2"  # high kept

    def test_none_passes_through(self):
        assert apply_confidence_gate(None, "low") is None

    def test_unknown_threshold_falls_back_to_low(self):
        # Unknown threshold behaves like 'low' → only low + missing blanked.
        result = apply_confidence_gate(_tasks("low", "medium"), "bogus")
        assert result["tasks"][0]["name"] == ""
        assert result["tasks"][1]["name"] == "Task 1"


class TestConfigThreshold:
    def test_default_is_low(self, monkeypatch):
        monkeypatch.delenv("SCREENCAP_CONFIDENCE_GATE_THRESHOLD", raising=False)
        assert config.get_confidence_gate_threshold() == "low"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SCREENCAP_CONFIDENCE_GATE_THRESHOLD", "MEDIUM")
        assert config.get_confidence_gate_threshold() == "medium"

    def test_invalid_env_falls_back_to_low(self, monkeypatch):
        monkeypatch.setenv("SCREENCAP_CONFIDENCE_GATE_THRESHOLD", "bogus")
        assert config.get_confidence_gate_threshold() == "low"
