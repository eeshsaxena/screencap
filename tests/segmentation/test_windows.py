"""Candidate task windows + per-window digests (U1 / SCR-275).

Tests for ``screencap.segmentation.windows``: over-segmented candidate
windows from idle gaps (reusing ``task_manifest._segment_tasks``) plus
app-shift boundaries, a >=60s floor enforced by forward-merge, a hard window
cap resolved by merging the shortest adjacent pairs, and fresh, stripped,
token-budgeted per-window digests — never sliced from the capped whole-day
summary, so late windows on long days do not starve (AE1 input side).

Vision-free and ``@pytest.mark.privacy`` so the CI privacy lane runs them
(CI runs only ``pytest -m privacy``). The strip semantics use a callable
predicate; the fail-closed Path-derivation branch uses a missing recording
dir (no OCR / Apple Vision anywhere).
"""

from __future__ import annotations

import math

import pytest

from screencap.segmentation.windows import (
    BUDGET_HEADROOM,
    LATIN_CHARS_PER_TOKEN,
    NAMING_DIGEST_TOKEN_BUDGET,
    NON_LATIN_CHARS_PER_TOKEN,
    build_candidate_windows,
    build_window_digests,
)
from tests.segmentation import _fixtures

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Event helpers (the same dict shapes ``activity_summary`` reads)
# ---------------------------------------------------------------------------


def _switch(ts: float, bundle: str, title: str, domain: str = "") -> dict:
    evt = {
        "type": "window.switch",
        "timestamp": ts,
        "app_bundle_id": bundle,
        "window_title": title,
    }
    if domain:
        evt["domain"] = domain
    return evt


def _type(ts: float, text: str) -> dict:
    return {"type": "key.type", "timestamp": ts, "text": text}


def _click(ts: float) -> dict:
    return {"type": "mouse.singleclick", "timestamp": ts}


def _never_blocked(_ts: float) -> bool:
    return False


def _spans(windows) -> list[tuple[float, float]]:
    return [(w.start_ts, w.end_ts) for w in windows]


# ---------------------------------------------------------------------------
# Shared happy-path day: two idle gaps + one app shift → 4 candidate windows.
#
# Bursts (rest_threshold=120):
#   A: 1000..1430 with an app shift at 1250 (VSCode → Chrome)
#   B: 2000..2200 (Slack; shift at burst START, so no extra split)
#   C: 2900..3000 (VSCode)
# ---------------------------------------------------------------------------

_VSCODE = "com.microsoft.VSCode"
_CHROME = "com.google.Chrome"
_SLACK = "com.tinyspeck.slackmacgap"


def _happy_day() -> tuple[list[dict], list[dict], _fixtures.InMemorySource]:
    chunk0 = [
        _switch(1000.0, _VSCODE, "main.py — screencap"),
        _type(1090.0, "def foo():"),
        _type(1180.0, "return 1"),
        _switch(1250.0, _CHROME, "Gmail", domain="mail.google.com"),
        _type(1340.0, "Hey team"),
        _click(1430.0),
        {"type": "mouse.move", "timestamp": 1500.0},
        _switch(2000.0, _SLACK, "#dev — Slack"),
        _type(2100.0, "PR is ready"),
    ]
    chunk1 = [
        _click(2200.0),
        _switch(2900.0, _VSCODE, "auth.py — screencap"),
        _type(3000.0, "class Auth:"),
    ]
    manifests = [
        {"format_version": 2, "chunk_index": 0, "chunk_start": 1000.0,
         "chunk_end": 2200.0, "blocked_intervals": []},
        {"format_version": 2, "chunk_index": 1, "chunk_start": 2200.0,
         "chunk_end": 3600.0, "blocked_intervals": []},
    ]
    transcripts = {
        0: {"segments": [
            # abs 1050 → window (1000,1250); abs 2100 → window (2000,2200)
            {"start": 50.0, "end": 55.0, "text": "working in the editor"},
            {"start": 1100.0, "end": 1105.0, "text": "checking slack"},
        ]}
    }
    events = chunk0 + chunk1
    source = _fixtures.InMemorySource(
        events_by_chunk={0: chunk0, 1: chunk1},
        transcripts_by_chunk=transcripts,
    )
    return events, manifests, source


_HAPPY_SPANS = [(1000.0, 1250.0), (1250.0, 1430.0), (2000.0, 2200.0), (2900.0, 3000.0)]


# ---------------------------------------------------------------------------
# Candidate windows
# ---------------------------------------------------------------------------


def test_two_gaps_one_app_shift_yield_over_segmented_windows():
    events, _, _ = _happy_day()
    windows = build_candidate_windows(events, rest_threshold=120.0)
    assert _spans(windows) == _HAPPY_SPANS
    assert [w.event_count for w in windows] == [3, 3, 3, 2]


def test_single_window_and_empty_day():
    events = [_switch(1000.0, _VSCODE, "main.py"), _type(1100.0, "x"), _click(1200.0)]
    windows = build_candidate_windows(events, rest_threshold=120.0)
    assert _spans(windows) == [(1000.0, 1200.0)]
    assert windows[0].event_count == 3

    assert build_candidate_windows([], rest_threshold=120.0) == []


def test_sub_60s_fragments_forward_merge():
    # Rapid app flips at the front: 30s fragments forward-merge to the floor.
    events = [
        _switch(1000.0, _VSCODE, "a"),
        _switch(1030.0, _CHROME, "b"),
        _switch(1060.0, _SLACK, "c"),
        _type(1150.0, "x"),
        _type(1250.0, "y"),
        _click(1340.0),
    ]
    windows = build_candidate_windows(events, rest_threshold=120.0)
    assert _spans(windows) == [(1000.0, 1060.0), (1060.0, 1340.0)]

    # A tiny TRAILING fragment has no next window: it merges backward.
    events2 = [
        _switch(1000.0, _VSCODE, "a"),
        _type(1100.0, "x"),
        _type(1200.0, "y"),
        _switch(1310.0, _CHROME, "b"),
        _type(1340.0, "z"),
    ]
    windows2 = build_candidate_windows(events2, rest_threshold=120.0)
    assert _spans(windows2) == [(1000.0, 1340.0)]


def _capped_day() -> list[dict]:
    """Six same-app bursts (durations 240,60,60,240,240,240) split by 400s gaps."""
    events: list[dict] = []
    for start, dur in [(1000.0, 240.0), (1640.0, 60.0), (2100.0, 60.0),
                       (2560.0, 240.0), (3200.0, 240.0), (3840.0, 240.0)]:
        events.append(_switch(start, _VSCODE, "work.py"))
        t = start
        while t < start + dur:
            t += 60.0 if dur == 60.0 else 80.0
            events.append(_click(t))
    return events

def test_window_cap_forward_merges_shortest_adjacent_pairs():
    events = _capped_day()
    uncapped = build_candidate_windows(events, rest_threshold=120.0)
    assert _spans(uncapped) == [
        (1000.0, 1240.0), (1640.0, 1700.0), (2100.0, 2160.0),
        (2560.0, 2800.0), (3200.0, 3440.0), (3840.0, 4080.0),
    ]

    # Cap 5: the shortest adjacent pair (60s + 60s) merges first — across its gap.
    capped5 = build_candidate_windows(events, rest_threshold=120.0, max_windows=5)
    assert _spans(capped5) == [
        (1000.0, 1240.0), (1640.0, 2160.0),
        (2560.0, 2800.0), (3200.0, 3440.0), (3840.0, 4080.0),
    ]

    # Cap 3: keeps merging shortest adjacent pairs until under the cap.
    capped3 = build_candidate_windows(events, rest_threshold=120.0, max_windows=3)
    assert len(capped3) == 3
    assert _spans(capped3)[0] == (1000.0, 2160.0)
    # Event counts survive every merge.
    assert sum(w.event_count for w in capped3) == sum(w.event_count for w in uncapped)


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def test_digests_contain_only_in_span_entries():
    events, manifests, source = _happy_day()
    windows = build_candidate_windows(events, rest_threshold=120.0)
    digests = build_window_digests(
        windows, manifests, source, blocked_source=_never_blocked,
    )
    assert len(digests) == 4
    assert all(d.usable and d.stripped for d in digests)

    titles = [[e["title"] for e in d.payload["timeline"]] for d in digests]
    assert titles == [
        ["main.py — screencap"], ["Gmail"], ["#dev — Slack"], ["auth.py — screencap"],
    ]
    # Typed text stays in its own window.
    assert digests[0].payload["timeline"][0]["typed"] == ["def foo():", "return 1"]
    assert digests[1].payload["timeline"][0]["typed"] == ["Hey team"]
    # Transcript snippets are span-scoped too.
    assert [s["text"] for s in digests[0].payload["transcript"]] == ["working in the editor"]
    assert [s["text"] for s in digests[2].payload["transcript"]] == ["checking slack"]
    assert "transcript" not in digests[1].payload
    assert "transcript" not in digests[3].payload
    # Every digest is under its input budget.
    for d in digests:
        assert d.token_estimate <= d.token_budget


def test_carry_in_app_context_for_window_without_switch():
    # Activity resumes after an idle gap with NO new window.switch: the digest
    # carries the last-known window in (task_manifest prev_window precedent).
    events = [
        _switch(1000.0, _VSCODE, "main.py"),
        _type(1100.0, "x"),
        _type(2000.0, "y"),
        _click(2100.0),
    ]
    manifests = [{"chunk_index": 0, "chunk_start": 1000.0, "chunk_end": 2500.0}]
    source = _fixtures.InMemorySource(events_by_chunk={0: events})
    windows = build_candidate_windows(events, rest_threshold=120.0)
    assert _spans(windows) == [(1000.0, 1100.0), (2000.0, 2100.0)]
    digests = build_window_digests(
        windows, manifests, source, blocked_source=_never_blocked,
    )
    assert digests[1].usable
    assert [e["title"] for e in digests[1].payload["timeline"]] == ["main.py"]


def test_long_day_last_window_digest_fresh_and_under_budget():
    # AE1 input side: 260 timeline entries day-wide (> MAX_ACTIVITY_ENTRIES=200).
    # A digest sliced from the capped whole-day summary would starve the last
    # window; a fresh per-span digest must not.
    events = [
        _switch(1000.0 + 20.0 * i, _VSCODE, f"file{i}.py") for i in range(230)
    ] + [
        _switch(6280.0 + 20.0 * i, "md.obsidian", f"doc{i}") for i in range(30)
    ]
    manifests = [{"chunk_index": 0, "chunk_start": 1000.0, "chunk_end": 7000.0}]
    source = _fixtures.InMemorySource(events_by_chunk={0: events})

    windows = build_candidate_windows(events, rest_threshold=120.0)
    assert _spans(windows) == [(1000.0, 5580.0), (6280.0, 6860.0)]

    digests = build_window_digests(
        windows, manifests, source, blocked_source=_never_blocked,
    )
    last = digests[-1]
    assert last.usable
    assert last.payload["timeline"], "last window digest must not be starved"
    last_titles = {e["title"] for e in last.payload["timeline"]}
    assert last_titles and last_titles <= {f"doc{i}" for i in range(30)}
    # Fresh per-span build: relative times restart at the window start.
    assert last.payload["timeline"][0]["t"] == "0:00:00"

    latin_char_budget = int(
        NAMING_DIGEST_TOKEN_BUDGET * LATIN_CHARS_PER_TOKEN * (1 - BUDGET_HEADROOM)
    )
    for d in digests:
        assert d.usable
        assert d.payload["timeline"]
        assert d.token_estimate <= d.token_budget
        assert len(d.text) <= latin_char_budget


def test_cjk_window_budgets_at_non_latin_ratio():
    events = [
        _switch(1000.0 + 60.0 * i, "com.apple.iWork.Pages",
                f"项目计划书第{i}章 修订稿 汇报准备材料")
        for i in range(40)
    ]
    manifests = [{"chunk_index": 0, "chunk_start": 1000.0, "chunk_end": 4000.0}]
    source = _fixtures.InMemorySource(events_by_chunk={0: events})
    windows = build_candidate_windows(events, rest_threshold=120.0)
    assert len(windows) == 1

    (digest,) = build_window_digests(
        windows, manifests, source, blocked_source=_never_blocked,
    )
    assert digest.usable
    non_latin_char_budget = int(
        NAMING_DIGEST_TOKEN_BUDGET * NON_LATIN_CHARS_PER_TOKEN * (1 - BUDGET_HEADROOM)
    )
    # Budgeted at the non-Latin ratio (the Latin budget would be 2x looser).
    assert len(digest.text) <= non_latin_char_budget
    assert digest.token_estimate == math.ceil(len(digest.text) / NON_LATIN_CHARS_PER_TOKEN)
    assert digest.token_estimate <= digest.token_budget
    assert digest.payload["timeline"]


# ---------------------------------------------------------------------------
# Cacheability + digest hash
# ---------------------------------------------------------------------------


def test_cacheable_defaults_and_settled_intervals():
    events, manifests, source = _happy_day()
    windows = build_candidate_windows(events, rest_threshold=120.0)

    # Default: fully-past windows cacheable, trailing window not.
    digests = build_window_digests(
        windows, manifests, source, blocked_source=_never_blocked,
    )
    assert [d.cacheable for d in digests] == [True, True, True, False]

    # Caller-supplied settled coverage: only fully-covered windows cache.
    digests = build_window_digests(
        windows, manifests, source, blocked_source=_never_blocked,
        settled_intervals=[(0.0, 2100.0)],
    )
    assert [d.cacheable for d in digests] == [True, True, False, False]


def test_digest_hash_stable_and_content_sensitive():
    events, manifests, source = _happy_day()
    windows = build_candidate_windows(events, rest_threshold=120.0)
    first = build_window_digests(
        windows, manifests, source, blocked_source=_never_blocked,
    )
    again = build_window_digests(
        windows, manifests, source, blocked_source=_never_blocked,
    )
    assert [d.digest_hash for d in first] == [d.digest_hash for d in again]

    # Changing content inside one window changes only that window's hash.
    events2, _, _ = _happy_day()
    events2[0]["window_title"] = "other.py — screencap"
    chunk0 = events2[:9]
    chunk1 = events2[9:]
    source2 = _fixtures.InMemorySource(events_by_chunk={0: chunk0, 1: chunk1})
    changed = build_window_digests(
        windows, manifests, source2, blocked_source=_never_blocked,
    )
    assert changed[0].digest_hash != first[0].digest_hash
    assert changed[3].digest_hash == first[3].digest_hash


# ---------------------------------------------------------------------------
# Privacy: strip + fail-closed
# ---------------------------------------------------------------------------


def test_blocked_overlap_stripped_and_full_block_fails_closed():
    events, manifests, source = _happy_day()
    windows = build_candidate_windows(events, rest_threshold=120.0)

    def blocked(ts: float) -> bool:
        # Partially covers window (1250,1430); fully covers window (2900,3000).
        return 1340.0 <= ts < 1500.0 or 2900.0 <= ts <= 3100.0

    digests = build_window_digests(windows, manifests, source, blocked_source=blocked)

    partial = digests[1]  # (1250, 1430): the switch survives, typed/click blocked
    assert partial.usable and partial.stripped
    assert [e["title"] for e in partial.payload["timeline"]] == ["Gmail"]
    assert all("typed" not in e for e in partial.payload["timeline"])
    assert "Hey team" not in partial.text

    full = digests[3]  # (2900, 3000): everything blocked → fail closed
    assert not full.usable
    assert full.payload is None
    assert not full.cacheable

    # Unblocked windows keep their content.
    assert digests[0].usable
    assert digests[0].payload["timeline"][0]["typed"] == ["def foo():", "return 1"]


def test_unstrippable_window_fails_closed(tmp_path):
    # A recording dir with no recording.db: the strip derivation fails, the
    # predicate fails closed to all-blocked, and every digest is unusable —
    # mechanical name, no model call.
    events, manifests, source = _happy_day()
    windows = build_candidate_windows(events, rest_threshold=120.0)
    digests = build_window_digests(
        windows, manifests, source, blocked_source=tmp_path / "missing-recording",
    )
    assert digests
    for d in digests:
        assert not d.usable
        assert not d.stripped
        assert d.payload is None
        assert d.text == ""
        assert not d.cacheable
        assert d.arbitration_line  # mechanical/no-data line still present
