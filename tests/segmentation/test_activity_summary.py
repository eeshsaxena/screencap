"""Tests for the extracted, source-agnostic activity-summary builder.

The headline test is the characterization/golden assertion: the extracted
package must produce byte-identical output to the pre-refactor cloud path on
the shared in-memory fixture (``golden_segmentation.json``, captured from the
original ``scripts/process-recording/main.py`` before extraction).
"""

from __future__ import annotations

import json
from pathlib import Path

from screencap.segmentation import activity_summary as asum
from screencap.segmentation.activity_summary import build_activity_summary
from screencap.segmentation.validate import validate_llm_tasks
from tests.segmentation import _fixtures

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_segmentation.json"


def _load_golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


# ---------------------------------------------------------------------------
# Characterization / golden — the KTD1 behavior-preservation guard
# ---------------------------------------------------------------------------

def test_activity_summary_matches_golden():
    """Extracted builder output == pre-refactor cloud output (byte-identical)."""
    result = build_activity_summary(
        "test-rec", _fixtures.MANIFESTS, _fixtures.default_source(),
    )
    golden = _load_golden()["activity_summary"]
    # Round-trip through JSON so the comparison is on the serialized form (the
    # artifact that actually ships), not float identity nuances.
    assert json.loads(json.dumps(result, sort_keys=True)) == golden


def test_full_pipeline_matches_golden():
    """activity summary → validate, end-to-end, matches the golden."""
    activity = build_activity_summary(
        "test-rec", _fixtures.MANIFESTS, _fixtures.default_source(),
    )
    validated = validate_llm_tasks(
        _fixtures.LLM_RESULT,
        activity["session_start"],
        activity["session_end"],
        activity["time_map"],
    )
    golden = _load_golden()["validated"]
    assert json.loads(json.dumps(validated, sort_keys=True)) == golden


# ---------------------------------------------------------------------------
# Timeline content — apps, titles, action counts
# ---------------------------------------------------------------------------

def test_timeline_entries_apps_titles_counts():
    result = build_activity_summary(
        "test-rec", _fixtures.MANIFESTS, _fixtures.default_source(),
    )
    timeline = result["summary"]["timeline"]
    assert [e["app"] for e in timeline] == ["VS Code", "Chrome", "VS Code", "Slack"]
    assert [e["cat"] for e in timeline] == ["CODE", "BROWSER", "CODE", "CHAT"]
    # VS Code entry has the recorded action counts.
    assert timeline[0]["clicks"] == 1
    assert timeline[0]["shortcuts"] == ["Cmd+S"]
    assert timeline[0]["typed"] == ["def foo():"]


def test_cross_chunk_merge():
    """Same app+title at the chunk boundary merges into one entry."""
    result = build_activity_summary(
        "test-rec", _fixtures.MANIFESTS, _fixtures.default_source(),
    )
    auth = [e for e in result["entries"] if "auth.py" in e.get("title", "")]
    assert len(auth) == 1
    # Merged typed text from both chunks (capped at 5).
    assert auth[0]["typed"] == ["class Auth:", "def token():"]


def test_transcript_snippets_included():
    result = build_activity_summary(
        "test-rec", _fixtures.MANIFESTS, _fixtures.default_source(),
    )
    snippets = result["summary"]["transcript"]
    assert len(snippets) == 2
    assert "fix this bug" in snippets[0]["text"]


# ---------------------------------------------------------------------------
# Source-agnostic: builder reads only the injected source (no GCS)
# ---------------------------------------------------------------------------

def test_builder_reads_only_injected_source():
    """Covers AE1 (builder side): no storage import, only the source is read.

    The strip lands in U3; here we assert the builder is source-agnostic — it
    consults the injected source and nothing else, so a blocked interval never
    reaches it via a hidden GCS path.
    """
    seen_events: list[dict] = []
    seen_chunks: list[int] = []

    class RecordingSource:
        def iter_events(self):
            for evt in _fixtures.EVENTS_0:
                if not evt.get("_meta"):
                    seen_events.append(evt)
                    yield evt

        def read_transcript(self, chunk_index):
            seen_chunks.append(chunk_index)
            return None

    single_manifest = [_fixtures.MANIFESTS[0]]
    build_activity_summary("test-rec", single_manifest, RecordingSource())
    # Builder pulled events + asked for the chunk transcript from OUR source.
    assert seen_events, "builder did not read the injected event source"
    assert seen_chunks == [0]


# ---------------------------------------------------------------------------
# Edge cases: empty, single-event, capped entries
# ---------------------------------------------------------------------------

def test_empty_events_returns_none():
    source = _fixtures.InMemorySource(events_by_chunk={0: [], 1: []})
    assert build_activity_summary("test-rec", _fixtures.MANIFESTS, source) is None


def test_no_window_switch_returns_none():
    """Events present but no window.switch → no entries → None."""
    events = [
        {"type": "key.type", "timestamp": 1100.0, "text": "orphan"},
        {"type": "mouse.singleclick", "timestamp": 1110.0},
    ]
    source = _fixtures.InMemorySource(events_by_chunk={0: events})
    assert build_activity_summary("test-rec", [_fixtures.MANIFESTS[0]], source) is None


def test_single_window_event():
    events = [
        {"type": "window.switch", "timestamp": 1100.0,
         "app_bundle_id": "com.microsoft.VSCode", "window_title": "solo.py"},
    ]
    source = _fixtures.InMemorySource(events_by_chunk={0: events})
    result = build_activity_summary("test-rec", [_fixtures.MANIFESTS[0]], source)
    assert result is not None
    assert len(result["summary"]["timeline"]) == 1
    entry = result["summary"]["timeline"][0]
    assert entry["app"] == "VS Code"
    # end_ts falls back to session_end for the final (only) entry.
    assert result["entries"][0]["end_ts"] == _fixtures.MANIFESTS[0]["chunk_end"]


def test_entries_capped_at_max():
    """More than MAX_ACTIVITY_ENTRIES distinct windows → timeline capped."""
    n = asum.MAX_ACTIVITY_ENTRIES + 25
    events = []
    for i in range(n):
        events.append({
            "type": "window.switch", "timestamp": 1100.0 + i,
            # Distinct bundle_id per entry so none merge.
            "app_bundle_id": f"com.example.app{i}", "window_title": f"win {i}",
        })
    manifest = [{
        "format_version": 2, "chunk_index": 0, "chunk_start": 1000.0,
        "chunk_end": 1000.0 + n + 100.0, "stats": {}, "blocked_intervals": [],
    }]
    source = _fixtures.InMemorySource(events_by_chunk={0: events})
    result = build_activity_summary("test-rec", manifest, source)
    assert result is not None
    # entries retains all merged windows; the LLM timeline is capped.
    assert len(result["entries"]) == n
    assert len(result["summary"]["timeline"]) == asum.MAX_ACTIVITY_ENTRIES


# ---------------------------------------------------------------------------
# Package is cloud-free / importable in the daemon
# ---------------------------------------------------------------------------

def test_package_has_no_cloud_imports():
    """Importing the package pulls in no google/genai/storage modules."""
    import sys

    before = set(sys.modules)
    import screencap.segmentation  # noqa: F401
    import screencap.segmentation.activity_summary  # noqa: F401
    import screencap.segmentation.schema  # noqa: F401
    import screencap.segmentation.validate  # noqa: F401
    new = set(sys.modules) - before
    offenders = [
        m for m in new
        if m.split(".")[0] in {"google", "genai", "storage", "functions_framework"}
    ]
    assert offenders == [], f"segmentation import pulled in cloud modules: {offenders}"
