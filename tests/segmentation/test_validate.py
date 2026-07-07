"""Tests for the extracted LLM-output validator.

Covers: relative→Unix conversion, overlap/zero-duration rejection, schema-drift
repair (missing summary fields, bad category, tag normalization).
"""

from __future__ import annotations

from screencap.segmentation.schema import _RESPONSE_SCHEMA
from screencap.segmentation.validate import validate_llm_tasks


def _result(tasks, summary=None, tags=None):
    return {
        "tasks": tasks,
        "summary": summary if summary is not None else {
            "overview": "Test session.",
            "primary_focus": "development",
            "time_breakdown": {"development": 100},
            "key_accomplishments": ["Did stuff"],
        },
        "tags": tags if tags is not None else [],
    }


def _task(start, end, name="Task", cat="development"):
    return {
        "start_time": start, "end_time": end, "name": name,
        "description": "desc", "category": cat, "apps_used": [], "confidence": "high",
    }


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------

def test_time_map_hit_converts_to_unix():
    time_map = {"0:00:00": 1000.0, "0:10:00": 1600.0, "0:20:00": 2200.0}
    result = _result([
        _task("0:00:00", "0:10:00", "Coding"),
        _task("0:10:00", "0:20:00", "Email", "communication"),
    ])
    out = validate_llm_tasks(result, 1000.0, 2200.0, time_map)
    assert out is not None
    assert out["tasks"][0]["start_ts"] == 1000.0
    assert out["tasks"][0]["end_ts"] == 1600.0
    assert out["tasks"][0]["derived_name"] == "coding"


def test_parse_fallback_when_absent_from_time_map():
    """A timestamp not in time_map falls back to session_start + parsed offset."""
    out = validate_llm_tasks(
        _result([_task("0:00:00", "0:05:00")]),
        session_start=1000.0, session_end=2000.0, time_map={},
    )
    assert out is not None
    # 0:05:00 → 300s → 1000 + 300 = 1300 (clamped within [1000, 2000]).
    assert out["tasks"][0]["end_ts"] == 1300.0


def test_clamps_to_session_bounds():
    """A task end past session_end is clamped, not rejected."""
    out = validate_llm_tasks(
        _result([_task("0:00:00", "9:99:99")]),
        session_start=1000.0, session_end=1500.0, time_map={},
    )
    assert out is not None
    assert out["tasks"][0]["end_ts"] == 1500.0


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

def test_empty_tasks_rejected():
    assert validate_llm_tasks(_result([]), 1000.0, 2000.0, {}) is None


def test_missing_field_rejected():
    bad = {"start_time": "0:00:00", "end_time": "0:10:00",
           "description": "d", "category": "development"}  # no "name"
    assert validate_llm_tasks(_result([bad]), 1000.0, 1600.0, {}) is None


def test_zero_duration_rejected():
    out = validate_llm_tasks(
        _result([_task("0:00:00", "0:00:00")]),
        session_start=1000.0, session_end=2000.0, time_map={},
    )
    assert out is None


def test_overlap_rejected():
    out = validate_llm_tasks(
        _result([_task("0:00:00", "0:12:00", "A"),
                 _task("0:10:00", "0:20:00", "B", "other")]),
        session_start=1000.0, session_end=2200.0, time_map={},
    )
    assert out is None


def test_small_gap_allowed():
    out = validate_llm_tasks(
        _result([_task("0:00:00", "0:10:00", "A"),
                 _task("0:10:05", "0:20:00", "B", "other")]),
        session_start=1000.0, session_end=2200.0, time_map={},
    )
    assert out is not None
    assert len(out["tasks"]) == 2
    # rest_after_s captured on the first task (5s gap).
    assert out["tasks"][0]["rest_after_s"] == 5.0
    assert out["tasks"][-1]["rest_after_s"] == 0.0


# ---------------------------------------------------------------------------
# Schema-drift repair
# ---------------------------------------------------------------------------

def test_invalid_category_normalized_to_other():
    out = validate_llm_tasks(
        _result([_task("0:00:00", "0:10:00", cat="NONSENSE")]),
        session_start=1000.0, session_end=1600.0, time_map={},
    )
    assert out is not None
    assert out["tasks"][0]["category"] == "other"


def test_missing_summary_fields_repaired():
    out = validate_llm_tasks(
        _result([_task("0:00:00", "0:10:00", cat="development")], summary={}),
        session_start=1000.0, session_end=1600.0, time_map={},
    )
    assert out is not None
    s = out["summary"]
    assert s["overview"] == "Recording with 1 tasks."
    assert s["primary_focus"] == "development"  # derived from task categories
    assert s["time_breakdown"] == {}
    assert s["key_accomplishments"] == []


def test_tags_normalized_deduped_and_capped():
    out = validate_llm_tasks(
        _result([_task("0:00:00", "0:10:00")],
                tags=["Python", "python", "code-review", "BAD TAG!!",
                      "a", "b", "c", "d", "e", "f", "g", "h", "i"]),
        session_start=1000.0, session_end=1600.0, time_map={},
    )
    assert out is not None
    # Lowercased, deduped, invalid dropped, capped at 8.
    assert out["tags"] == ["python", "code-review", "a", "b", "c", "d", "e", "f"]


def test_tags_non_string_and_punctuation_filtered():
    out = validate_llm_tasks(
        _result([_task("0:00:00", "0:10:00")],
                tags=[123, None, "Python!", "good-tag", "valid"]),
        session_start=1000.0, session_end=1600.0, time_map={},
    )
    assert out is not None
    # Non-strings dropped; "Python!" rejected (bad char); rest kept lowercased.
    assert out["tags"] == ["good-tag", "valid"]


def test_blank_name_gets_placeholder():
    task = _task("0:00:00", "0:10:00")
    task["name"] = ""
    out = validate_llm_tasks(
        _result([task]), session_start=1000.0, session_end=1600.0, time_map={},
    )
    assert out is not None
    assert out["tasks"][0]["name"] == "Task 1"


# ---------------------------------------------------------------------------
# Schema shape sanity
# ---------------------------------------------------------------------------

def test_response_schema_shape():
    assert _RESPONSE_SCHEMA["type"] == "object"
    assert set(_RESPONSE_SCHEMA["required"]) == {"tasks", "summary", "tags"}
    task_props = _RESPONSE_SCHEMA["properties"]["tasks"]["items"]["properties"]
    assert "start_time" in task_props and "end_time" in task_props
    assert task_props["category"]["enum"] == [
        "development", "communication", "research", "admin", "creative", "other",
    ]
