"""Candidate task windows + per-window digests for on-device segmentation.

U1 of the SCR-275 heuristic-first pipeline (R1, R5 input side; KTD-4, KTD-5):
local signals over-segment the day into candidate windows, and each window
gets a fresh, privacy-stripped, token-budgeted digest. Downstream arbitration
is merge-only — it can never split — so over-segmentation is the hedge.

Candidate windows (:func:`build_candidate_windows`)
    Idle-gap segmentation reuses ``task_manifest._segment_tasks`` (gap >
    ``rest_threshold``) over the same event rows ``activity_summary`` reads,
    then splits each activity segment at app-shift boundaries (consecutive
    ``window.switch`` events whose bundle id changed). A >=60s floor is
    enforced by forward-merging tiny fragments, and a hard cap on window
    count is enforced by merging the SHORTEST adjacent candidate pairs.

Per-window digests (:func:`build_window_digests`)
    Each digest is built FRESH from the recording's rows for the window span
    by calling ``build_activity_summary`` over a span-scoped source — reusing
    its entry building (title/typed/shortcut caps, merge-consecutive),
    transcript snippets, AND its R11 privacy strip verbatim. Digests are
    never sliced from the capped 200-entry whole-day summary (which starves
    late windows on long days). A window whose strip cannot be established
    fails closed: the digest is marked unusable and the caller gives the
    window a mechanical name with no model call.

    Input budgets are script-aware (~3 chars/token Latin-dominant, ~1.5 for
    non-Latin-dominant content such as CJK) with >=10% headroom under a hard
    per-digest token budget. Each digest also carries the compressed
    one-line arbitration form (dominant apps, duration, a few title
    keywords), a stable content hash (the naming-cache key), and a
    ``cacheable`` flag driven by caller-supplied settled-coverage info.

The module is deliberately light: heavy work (strip derivation) stays behind
``build_activity_summary``'s deferred imports, matching package convention.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Iterable, Sequence

from screencap.pipeline_state import spans_overlap
from screencap.segmentation.activity_summary import (
    ActivitySource,
    BlockedPredicate,
    BlockedSource,
    _always_blocked,
    _duration_human,
    _resolve_blocked_predicate,
    build_activity_summary,
)
from screencap.task_manifest import _segment_tasks

log = logging.getLogger(__name__)

# --- Candidate-window shape (KTD-4) ---------------------------------------
MIN_WINDOW_SECS = 60.0
# Sizing inequality: cap × per-window arbitration line (≈25 tokens) +
# instructions + response cap must fit the 4096 window with headroom.
MAX_CANDIDATE_WINDOWS = 40

# --- Digest budgets (KTD-5) ------------------------------------------------
NAMING_DIGEST_TOKEN_BUDGET = 700  # hard input budget per naming digest
LATIN_CHARS_PER_TOKEN = 3.0
NON_LATIN_CHARS_PER_TOKEN = 1.5  # CJK runs ~1-1.5 chars/token
NON_LATIN_DOMINANCE_RATIO = 0.30
BUDGET_HEADROOM = 0.10  # always keep >=10% of the budget free

_ARBITRATION_LINE_MAX_CHARS = 100
_ARBITRATION_MAX_KEYWORDS = 4
_ARBITRATION_MAX_APPS = 2

# Per-entry detail fields dropped (all at once, in this order) when a lone
# surviving timeline entry must shrink further. Shared with
# ``ondevice_pipeline._halve_payload`` via :func:`strip_entry_detail` so the
# trim vocabulary cannot drift between the two.
TRIMMABLE_ENTRY_FIELDS = ("typed", "shortcuts", "clicks", "domain")


@dataclass(frozen=True)
class CandidateWindow:
    """One over-segmented candidate task window (a span of activity)."""

    start_ts: float
    end_ts: float
    event_count: int

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts


@dataclass(frozen=True)
class WindowDigest:
    """A window's model-facing digest plus its cache/arbitration metadata.

    ``usable is False`` means the window failed closed (no in-span content
    after the strip, or the strip itself could not be established): the
    caller must give it a mechanical name and make NO model call for it.
    """

    window: CandidateWindow
    usable: bool
    stripped: bool
    payload: dict | None  # naming digest (trimmed); None when unusable
    text: str  # serialized payload, what the budget counted; "" when unusable
    token_estimate: int
    token_budget: int
    digest_hash: str  # stable content hash — the naming-cache key
    cacheable: bool
    arbitration_line: str  # compressed one-line-per-window arbitration form


# ---------------------------------------------------------------------------
# Candidate windows (KTD-4)
# ---------------------------------------------------------------------------


def build_candidate_windows(
    events: Iterable[dict],
    *,
    rest_threshold: float | None = None,
    min_window_secs: float = MIN_WINDOW_SECS,
    max_windows: int = MAX_CANDIDATE_WINDOWS,
) -> list[CandidateWindow]:
    """Over-segment a day of events into candidate task windows.

    ``events`` are the same dict rows ``activity_summary`` reads (an
    ``ActivitySource.iter_events()`` stream, materialized). Idle-gap
    segmentation reuses ``task_manifest._segment_tasks``; app-shift
    boundaries come from consecutive ``window.switch`` events whose
    ``app_bundle_id`` changed. ``rest_threshold`` defaults to
    ``config.get_rest_threshold()``.
    """
    if rest_threshold is None:
        from screencap import config  # deferred: keep import surface light

        rest_threshold = config.get_rest_threshold()

    action_ts: list[float] = []
    switches: list[tuple[float, str]] = []
    for evt in events:
        ts = evt.get("timestamp", 0) or 0
        if ts <= 0:
            continue
        etype = evt.get("type", "")
        if etype == "mouse.move":
            continue
        action_ts.append(ts)
        if etype == "window.switch":
            switches.append((ts, evt.get("app_bundle_id", "") or ""))
    action_ts.sort()
    switches.sort()

    segments = _segment_tasks(
        [{"timestamp": ts} for ts in action_ts], rest_threshold,
    )
    if not segments:
        return []

    # App-shift boundaries: each switch whose bundle differs from the previous.
    shift_bounds: list[float] = []
    prev_bundle: str | None = None
    for ts, bundle in switches:
        if prev_bundle is not None and bundle != prev_bundle:
            shift_bounds.append(ts)
        prev_bundle = bundle

    windows: list[CandidateWindow] = []
    for seg_start, seg_end, _count in segments:
        points = [seg_start]
        points += [b for b in shift_bounds if seg_start < b < seg_end]
        points.append(seg_end)
        for k in range(len(points) - 1):
            lo, hi = points[k], points[k + 1]
            if k == len(points) - 2:  # segment-final: end inclusive
                n = bisect_right(action_ts, hi) - bisect_left(action_ts, lo)
            else:  # boundary event belongs to the next window
                n = bisect_left(action_ts, hi) - bisect_left(action_ts, lo)
            windows.append(CandidateWindow(lo, hi, n))

    windows = _enforce_floor(windows, min_window_secs)
    windows = _enforce_cap(windows, max_windows)
    return windows


def _merge_pair(a: CandidateWindow, b: CandidateWindow) -> CandidateWindow:
    return CandidateWindow(a.start_ts, b.end_ts, a.event_count + b.event_count)


def _enforce_floor(
    windows: list[CandidateWindow], min_window_secs: float,
) -> list[CandidateWindow]:
    """Forward-merge windows shorter than the floor (a lone window is kept)."""
    windows = list(windows)
    i = 0
    while len(windows) > 1 and i < len(windows):
        if windows[i].duration_s >= min_window_secs:
            i += 1
            continue
        if i + 1 < len(windows):
            windows[i:i + 2] = [_merge_pair(windows[i], windows[i + 1])]
            # re-check the merged window at the same index
        else:
            # tiny trailing window: nothing ahead, merge backward
            windows[i - 1:i + 1] = [_merge_pair(windows[i - 1], windows[i])]
            break
    return windows


def _enforce_cap(
    windows: list[CandidateWindow], max_windows: int,
) -> list[CandidateWindow]:
    """Merge the SHORTEST adjacent candidate pairs until under the cap."""
    windows = list(windows)
    while len(windows) > max_windows:
        best = min(
            range(len(windows) - 1),
            key=lambda k: (windows[k].duration_s + windows[k + 1].duration_s, k),
        )
        windows[best:best + 2] = [_merge_pair(windows[best], windows[best + 1])]
    return windows


# ---------------------------------------------------------------------------
# Per-window digests (KTD-5)
# ---------------------------------------------------------------------------


def build_window_digests(
    windows: Sequence[CandidateWindow],
    manifests: list[dict],
    source: ActivitySource,
    *,
    blocked_source: BlockedSource,
    token_budget: int = NAMING_DIGEST_TOKEN_BUDGET,
    settled_intervals: Sequence[tuple[float, float]] | None = None,
) -> list[WindowDigest]:
    """Build a fresh, stripped, token-budgeted digest per candidate window.

    ``blocked_source`` is REQUIRED (the R11 strip contract): pass the
    recording dir (predicate re-derived fail-closed from its local
    ``recording.db``) or an already-built ``is_blocked(ts)`` predicate. A
    window whose strip cannot be established yields an unusable digest.

    ``settled_intervals`` is the caller-supplied coverage info (spans fully
    covered by stage-complete chunks with a settled strip derivation). When
    given, a window is cacheable only if fully covered by their union; when
    ``None``, every fully-past window is cacheable and the trailing window
    is not. This module never reaches into chunk manifests/ledgers itself.
    """
    if blocked_source is None:
        # Fail closed rather than raise: a missing strip must never become a
        # model call. Every window goes mechanical this pass.
        log.error(
            "build_window_digests called without a blocked_source; failing "
            "closed (all window digests unusable)"
        )

    events = sorted(
        (e for e in source.iter_events() if (e.get("timestamp", 0) or 0) > 0),
        key=lambda e: e["timestamp"],
    )
    event_ts = [e["timestamp"] for e in events]
    window_starts = {w.start_ts for w in windows}

    if blocked_source is not None and not callable(blocked_source) and manifests:
        # Resolve the R11 strip predicate ONCE per pass: a recording-dir source
        # re-derives the blocked intervals from its local ``recording.db``
        # (domain index + skip-interval derivation) — far too heavy to repeat
        # for every window. Each per-window ``build_activity_summary`` call
        # below then takes the callable fast path. A resolution failure keeps
        # today's per-window outcome: the predicate fails closed to
        # all-blocked, every window strips empty → summary ``None`` →
        # unusable digest, mechanical name, no model call.
        blocked_source = _resolve_pass_predicate(blocked_source, manifests, source, events)

    # Merge/sort the settled coverage ONCE; containment per window is then a
    # scan of the (small) disjoint union instead of a re-sort per window.
    settled_union = (
        _merged_union(settled_intervals) if settled_intervals is not None else None
    )

    digests: list[WindowDigest] = []
    for i, window in enumerate(windows):
        summary = None
        if blocked_source is not None:
            # An adjacent window owns the shared boundary timestamp; a
            # segment-final window includes its own end event.
            include_end = window.end_ts not in window_starts
            summary = _build_span_summary(
                window, include_end, manifests, events, event_ts, source,
                blocked_source,
            )
        usable = summary is not None and summary.get("stripped") is True

        if usable:
            payload, text, token_estimate = _trim_to_budget(summary, token_budget)
        else:
            payload, text, token_estimate = None, "", 0

        if not usable:
            cacheable = False
        elif settled_union is not None:
            cacheable = _covered(window.start_ts, window.end_ts, settled_union)
        else:
            cacheable = i < len(windows) - 1  # trailing window never caches

        digests.append(
            WindowDigest(
                window=window,
                usable=usable,
                stripped=usable,
                payload=payload,
                text=text,
                token_estimate=token_estimate,
                token_budget=token_budget,
                digest_hash=_digest_hash(window, payload),
                cacheable=cacheable,
                arbitration_line=_arbitration_line(
                    window, summary if usable else None,
                ),
            )
        )
    return digests


class _SpanSource:
    """An ``ActivitySource`` view scoped to one window span.

    Events are pre-filtered to the span (with an optional synthesized
    carry-in ``window.switch`` at the span start — the ``task_manifest``
    ``prev_window`` precedent). Transcript segments are re-read from the
    inner source, filtered to the span by TRUE chunk start, and re-based so
    the clamped manifests the builder sees still place them correctly.
    """

    def __init__(
        self,
        events: list[dict],
        inner: ActivitySource,
        span: tuple[float, float, bool],  # (start, end, include_end)
        true_starts: dict[int, float],
        clamped_starts: dict[int, float],
    ) -> None:
        self._events = events
        self._inner = inner
        self._span = span
        self._true_starts = true_starts
        self._clamped_starts = clamped_starts

    def iter_events(self) -> Iterable[dict]:
        return iter(self._events)

    def read_transcript(self, chunk_index: int) -> dict | None:
        data = self._inner.read_transcript(chunk_index)
        if not isinstance(data, dict):
            return None
        true_start = self._true_starts.get(chunk_index)
        clamped_start = self._clamped_starts.get(chunk_index)
        if true_start is None or clamped_start is None:
            return None
        start, end, include_end = self._span
        segments: list[dict] = []
        for seg in data.get("segments", []):
            if not isinstance(seg, dict):
                continue
            abs_ts = true_start + (seg.get("start", 0) or 0)
            if abs_ts < start or abs_ts > end or (abs_ts == end and not include_end):
                continue
            adjusted = dict(seg)
            adjusted["start"] = abs_ts - clamped_start
            segments.append(adjusted)
        return {"segments": segments} if segments else None


def _resolve_pass_predicate(
    blocked_source: BlockedSource,
    manifests: list[dict],
    source: ActivitySource,
    events: list[dict],
) -> BlockedPredicate:
    """Resolve a recording-dir ``blocked_source`` into a predicate, ONCE per pass.

    Mirrors ``build_activity_summary``'s own resolution over the whole day:
    the window is the manifests' full span, and the forwarded strip timestamps
    are every event timestamp plus every transcript-segment absolute timestamp
    — so the per-window calls (handed the returned callable) keep the same
    fail-closed uncovered-gap protection. ANY failure returns the all-blocked
    sentinel (ERROR logged): every window then strips empty and fails closed —
    unusable digest, mechanical name, no model call — exactly as a per-window
    resolution failure does.
    """
    try:
        session_start = min(m["chunk_start"] for m in manifests)
        session_end = max(m["chunk_end"] for m in manifests)
        strip_tss = [e["timestamp"] for e in events]
        for manifest in sorted(manifests, key=lambda m: m["chunk_index"]):
            transcript = source.read_transcript(manifest["chunk_index"])
            if transcript is None:
                continue
            chunk_start = manifest["chunk_start"]
            for seg in transcript.get("segments", [])[:20]:
                strip_tss.append(chunk_start + seg.get("start", 0))
        return _resolve_blocked_predicate(
            blocked_source, (session_start, session_end), strip_tss,
        )
    except Exception:  # noqa: BLE001 — privacy fail-closed must win over surfacing
        log.error(
            "window-digest privacy strip: pass-level predicate resolution "
            "failed; failing closed (all window digests unusable)",
            exc_info=True,
        )
        return _always_blocked


def _build_span_summary(
    window: CandidateWindow,
    include_end: bool,
    manifests: list[dict],
    events: list[dict],
    event_ts: list[float],
    source: ActivitySource,
    blocked_source: BlockedSource,
) -> dict | None:
    """Run ``build_activity_summary`` scoped to one window span.

    ``events`` are the pass's timestamp-sorted event rows and ``event_ts``
    their parallel timestamp list, so the span slice and the carry-in lookup
    bisect instead of scanning the whole day per window.
    """
    start, end = window.start_ts, window.end_ts

    overlapping = [
        m for m in manifests
        if spans_overlap(m["chunk_start"], m["chunk_end"], start, end)
    ]
    if not overlapping:
        return None
    clamped = [
        {
            "chunk_index": m["chunk_index"],
            "chunk_start": max(m["chunk_start"], start),
            "chunk_end": min(m["chunk_end"], end),
        }
        for m in overlapping
    ]

    # Span slice via bisect over the sorted timestamps: [start, end) — plus the
    # end event itself for a segment-final window (include_end).
    lo = bisect_left(event_ts, start)
    hi = bisect_right(event_ts, end) if include_end else bisect_left(event_ts, end)
    span_events = events[lo:hi]

    # Carry-in: activity with no leading window.switch in-span inherits the
    # last-known window at the span start (task_manifest prev_window
    # precedent). The synthesized timestamp is the span start, so the strip
    # predicate still governs it: if the carried-in app is blocked on screen
    # at that instant, the entry is dropped like any other.
    first_switch = next(
        (e for e in span_events if e.get("type") == "window.switch"), None,
    )
    if first_switch is None or first_switch["timestamp"] > start:
        # The last window.switch strictly before the span start (events[:lo]).
        carry = None
        for j in range(lo - 1, -1, -1):
            if events[j].get("type") == "window.switch":
                carry = events[j]
                break
        if carry is not None:
            synthesized = {
                "type": "window.switch",
                "timestamp": start,
                "app_bundle_id": carry.get("app_bundle_id", ""),
                "window_title": carry.get("window_title", ""),
            }
            if carry.get("domain"):
                synthesized["domain"] = carry["domain"]
            span_events = [synthesized] + span_events

    span_source = _SpanSource(
        span_events,
        source,
        (start, end, include_end),
        {m["chunk_index"]: m["chunk_start"] for m in overlapping},
        {c["chunk_index"]: c["chunk_start"] for c in clamped},
    )
    # Recording-name-free on purpose: the digest payload must not carry the
    # recording name (and the hash must not depend on it).
    return build_activity_summary(
        "", clamped, span_source, blocked_source=blocked_source,
    )


# ---------------------------------------------------------------------------
# Script-aware token budgeting (KTD-5)
# ---------------------------------------------------------------------------


def _chars_per_token(content: str) -> float:
    """Return the chars/token ratio for the dominant script of ``content``.

    ``content`` is the window's CONTENT text (titles, typed, transcript) —
    not the JSON serialization, whose ASCII scaffolding would mask a
    CJK-dominant window.
    """
    total = 0
    non_latin = 0
    for ch in content:
        if ch.isspace():
            continue
        total += 1
        if ord(ch) >= 0x0370:  # beyond Latin (incl. extended) + combining
            non_latin += 1
    if total and non_latin / total >= NON_LATIN_DOMINANCE_RATIO:
        return NON_LATIN_CHARS_PER_TOKEN
    return LATIN_CHARS_PER_TOKEN


def _serialize(payload: dict | None) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _trim_to_budget(
    summary: dict, token_budget: int,
) -> tuple[dict, str, int]:
    """Deterministically trim a span summary's payload under the budget.

    Drop order: transcript snippets (from the end), then the
    shortest-duration timeline entries (ties drop the later entry, so the
    dominant and earliest activity survives), then per-entry detail on the
    last surviving entry. At least one timeline entry always survives.
    """
    timeline = [dict(e) for e in summary["summary"]["timeline"]]
    # summary["entries"] aligns 1:1 with the (capped) timeline — same order.
    durations = [
        max(0.0, e["end_ts"] - e["start_ts"])
        for e in summary["entries"][: len(timeline)]
    ]
    transcript = [dict(s) for s in summary["summary"].get("transcript", [])]

    content_bits: list[str] = []
    for e in timeline:
        content_bits.append(e.get("app", ""))
        content_bits.append(e.get("title", ""))
        content_bits.extend(e.get("typed") or [])
        content_bits.extend(e.get("shortcuts") or [])
    content_bits.extend(s.get("text", "") for s in transcript)
    cpt = _chars_per_token("".join(content_bits))
    char_budget = int(token_budget * cpt * (1.0 - BUDGET_HEADROOM))

    def _payload() -> dict:
        p: dict = {"duration": summary["summary"]["duration"], "timeline": timeline}
        if transcript:
            p["transcript"] = transcript
        return p

    text = _serialize(_payload())
    while len(text) > char_budget:
        if transcript:
            transcript.pop()
        elif len(timeline) > 1:
            k = min(range(len(timeline)), key=lambda j: (durations[j], -j))
            del timeline[k]
            del durations[k]
        else:
            if not strip_entry_detail(timeline[0]):
                break  # minimal single entry; nothing left to trim
        text = _serialize(_payload())

    return _payload(), text, math.ceil(len(text) / cpt)


def strip_entry_detail(entry: dict) -> bool:
    """Trim a lone timeline entry in place; ``True`` iff anything was trimmed.

    The shared last-resort trim vocabulary: drop every present
    :data:`TRIMMABLE_ENTRY_FIELDS` field first; once none remain, truncate an
    over-40-char title. ``False`` means the entry is already minimal. Used by
    ``_trim_to_budget``'s tail and ``ondevice_pipeline._halve_payload`` so the
    drop order cannot drift between them.
    """
    extras = [f for f in TRIMMABLE_ENTRY_FIELDS if f in entry]
    if extras:
        for f in extras:
            del entry[f]
        return True
    if len(entry.get("title", "")) > 40:
        entry["title"] = entry["title"][:40]
        return True
    return False


# ---------------------------------------------------------------------------
# Cache key + cacheability + arbitration form
# ---------------------------------------------------------------------------


def _digest_hash(window: CandidateWindow, payload: dict | None) -> str:
    """Stable content hash over the window span + trimmed payload."""
    blob = _serialize({
        "window": [round(window.start_ts, 3), round(window.end_ts, 3)],
        "payload": payload,
    })
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _merged_union(
    intervals: Sequence[tuple[float, float]],
) -> list[list[float]]:
    """Sort + merge ``intervals`` into a disjoint union (computed once per pass)."""
    merged: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if b > a):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def _covered(start: float, end: float, merged: list[list[float]]) -> bool:
    """True if ``[start, end]`` is fully covered by a :func:`_merged_union` list."""
    return any(a <= start and b >= end for a, b in merged)


def _arbitration_line(window: CandidateWindow, summary: dict | None) -> str:
    """The compressed one-line arbitration form for one window.

    Dominant apps + duration + a few title keywords — much smaller than the
    naming digest (KTD-4's payload sizing depends on it). Built ONLY from
    the stripped summary, so blocked content cannot leak into arbitration.
    """
    duration = _duration_human(max(0.0, window.duration_s))
    if summary is None:
        return f"{duration} | (no data)"

    entries = summary["entries"]
    app_secs: dict[str, float] = {}
    for e in entries:
        secs = max(0.0, e["end_ts"] - e["start_ts"])
        app_secs[e["app"]] = app_secs.get(e["app"], 0.0) + secs
    top_apps = sorted(app_secs.items(), key=lambda kv: (-kv[1], kv[0]))
    apps = "+".join(name for name, _ in top_apps[:_ARBITRATION_MAX_APPS]) or "unknown"

    keywords: list[str] = []
    seen: set[str] = set()
    for e in sorted(entries, key=lambda e: -(e["end_ts"] - e["start_ts"])):
        for word in (e.get("title") or "").split():
            bare = word.strip(".,;:—-–|/\\()[]{}").lower()
            if len(bare) < 3 or bare in seen:
                continue
            seen.add(bare)
            keywords.append(word[:16])
            if len(keywords) >= _ARBITRATION_MAX_KEYWORDS:
                break
        if len(keywords) >= _ARBITRATION_MAX_KEYWORDS:
            break

    line = f"{duration} | {apps}"
    if keywords:
        line += f" | {' '.join(keywords)}"
    return line[:_ARBITRATION_LINE_MAX_CHARS]
