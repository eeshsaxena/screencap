"""Heuristic-first on-device segmentation orchestrator (SCR-275, U4).

One provider call runs the whole HTD loop: candidate windows → (merge-only)
arbitration → cached per-window naming → assembly → day summary — returning
exactly what ``OnDeviceProvider.segment`` returns today (KTD-1): a tasks dict,
``None``, or :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE`.
The exterior tri-state is preserved so the degrade ladder, the cloud-summary
fallback, and the outcome gates downstream need no changes.

Key behaviors (see the SCR-275 plan for the KTD numbering):

- **KTD-4** — candidate windows over-segment; arbitration is merge-only and
  VALIDATED (contiguous, in-range, non-overlapping groups; anything else →
  the heuristic boundaries stand). A single candidate window skips the call.
- **KTD-6** — per-window naming results are memoized in ``recording.db``
  (``ondevice_window_names``, keyed by span + digest hash) so unchanged
  windows never re-call the model and an interrupted pass loses no naming
  work. On live passes, previously COMMITTED task boundaries are pinned:
  windows entirely before ``pinned_before_ts`` keep the committed boundaries,
  skip arbitration, and go through the cache-naming loop only; merges are
  computed only over the tail. The live day's trailing window is named but
  never cached (mutable until it settles).
- **KTD-7** — every pass has a wall-clock budget (live 240s / finalize 480s)
  plus cooperative ``stop_event`` checks between model calls; exhaustion or a
  stop sends the remaining windows mechanical THIS pass (they retry as cache
  misses next pass). Mirrors ``index_core``'s stop+budget pattern.
- **KTD-3 (caller half)** — a ``context-window`` naming failure halves the
  digest's timeline/transcript content (at most :data:`MAX_DIGEST_HALVINGS`
  times, re-wrapping with the stripped marker) before going mechanical.
- **KTD-9** — model output is untrusted: names stripped + capped (80 chars),
  categories checked against ``validate``'s allowlist (fallback ``other``),
  day-summary tags through its tag regex/count cap — all BEFORE any cache
  write or sink assembly. Zero model names → the unavailable sentinel with
  the dominant failure reason recorded on the provider.
- **KTD-10** — the scrub generation is snapshotted before digest computation,
  re-checked inside EVERY cache write (a mismatched store commits nothing and
  the window stays named-but-uncached this pass), and re-checked once more in
  one commit transaction at the end; a mismatch there (a retroactive scrub
  landed mid-pass) deletes this pass's cache writes and returns the
  unavailable sentinel with :data:`REASON_STALE_SCRUB` recorded on the
  provider (nothing reaches the sinks).

Seam for U6: per-task provenance rides ``task["source"]``
(:data:`SOURCE_MODEL` vs :data:`SOURCE_MECHANICAL`) on the returned dict —
read it BEFORE ``_persist_local_tasks`` rewrites row-level ownership — and
``provider.last_unavailable_reason`` carries ``None`` after a fully-model
pass, the dominant per-window failure reason after a partial one, and the
dominant reason alongside the sentinel when zero windows were model-named.
"""

from __future__ import annotations

import logging
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from screencap.segmentation.provider import PROVIDER_UNAVAILABLE
from screencap.segmentation.validate import _VALID_CATEGORIES, _slugify, _validate_tags
from screencap.segmentation.windows import (
    CandidateWindow,
    WindowDigest,
    build_candidate_windows,
    build_window_digests,
    strip_entry_detail,
)

if TYPE_CHECKING:
    from screencap.pipeline_state import PipelineLedger
    from screencap.segmentation.activity_summary import ActivitySource, BlockedSource

log = logging.getLogger(__name__)

# Pass budgets (KTD-7). The live default sits under the daemon's 300s
# incremental tick; finalize gets more room. Both are far above the per-call
# helper timeout (60s), so the inner timeout is never dead code.
LIVE_PASS_BUDGET_S = 240.0
FINALIZE_PASS_BUDGET_S = 480.0

# KTD-3: bounded caller-side recovery for a context-window naming failure.
MAX_DIGEST_HALVINGS = 3

# Per-task provenance markers (KTD-1/KTD-8). SOURCE_MECHANICAL is deliberately
# shared with ``terminal_stage._heuristic_local_tasks`` (which imports it) so
# every sink and classifier sees ONE mechanical vocabulary.
SOURCE_MODEL = "ondevice_model"
SOURCE_MECHANICAL = "idle_gap_heuristic"

# Pass-level failure pseudo-reasons (recorded like helper reasons).
REASON_BUDGET_EXHAUSTED = "budget-exhausted"
REASON_STOPPED = "stopped"
REASON_DIGEST_UNUSABLE = "digest-unusable"
# KTD-10: the end-of-pass staleness guard tripped (a retroactive scrub landed
# mid-pass). Like REASON_STOPPED this is NOT a real unavailability — the
# terminal stage short-circuits on it (with one finalize retry) and the
# chained provider must not cascade past it.
REASON_STALE_SCRUB = "stale-scrub"

_MAX_NAME_CHARS = 80
_MAX_OVERVIEW_CHARS = 600

# Test seam for the wall-clock budget (KTD-7).
_monotonic = time.monotonic


def compute_pinned_before_ts(recording_dir: Path | str) -> float | None:
    """The last COMMITTED task boundary for live-pass pinning (KTD-6).

    Max ``end_ts`` over the recording's unedited agent task rows (read via
    :func:`_committed_agent_spans`) — the spans a prior pass persisted and the
    user has not curated. ``None`` when there are none (first live pass →
    whole-tail arbitration) or on any read error (fail-open: the spans read
    returns ``[]``; no pinning is the safe direction; the cache still
    stabilizes names).
    """
    spans = _committed_agent_spans(Path(recording_dir))
    return max((end for _start, end in spans), default=None)


def run_heuristic_pipeline(
    provider,
    recording_dir: Path | str,
    source: "ActivitySource",
    manifests: list[dict],
    *,
    session_start: float,
    session_end: float,
    is_live: bool,
    stop_event=None,
    budget_s: float | None = None,
    rest_threshold: float | None = None,
    pinned_before_ts: float | None = None,
    blocked_source: "BlockedSource | None" = None,
):
    """Run one heuristic-first segmentation pass; return the KTD-1 tri-state.

    ``provider`` is the reason-bearing on-device provider (U3): the pipeline
    calls its ``call_arbitrate`` / ``call_name_window`` / ``call_day_summary``
    verbs and records the pass outcome on its ``last_unavailable_reason``.

    ``blocked_source`` defaults to ``recording_dir`` — the R11 fail-closed
    strip re-derived over the local ``recording.db`` — and exists as the same
    injection seam ``build_window_digests`` exposes (tests pass a predicate).

    Returns a ``{"tasks", "summary", "tags"}`` dict (per-task ``source`` marks
    model vs mechanical), ``None`` (nothing to segment), or
    :data:`PROVIDER_UNAVAILABLE` when ZERO windows were model-named (dominant
    reason on the provider) or the KTD-10 staleness guard tripped
    (:data:`REASON_STALE_SCRUB` on the provider — nothing reaches the sinks).
    """
    recording_dir = Path(recording_dir)
    if budget_s is None:
        budget_s = LIVE_PASS_BUDGET_S if is_live else FINALIZE_PASS_BUDGET_S
    deadline = _monotonic() + budget_s
    if blocked_source is None:
        blocked_source = recording_dir

    ledger = _open_cache_ledger(recording_dir)
    # KTD-10: snapshot the scrub generation BEFORE any digest computation.
    generation = ledger.read_scrub_generation() if ledger is not None else None

    events = list(source.iter_events())
    windows = build_candidate_windows(events, rest_threshold=rest_threshold)
    if not windows:
        provider.last_unavailable_reason = None
        return None  # nothing to segment — ran, produced nothing (fail-open)

    action_ts = sorted(
        ts for e in events
        if (ts := (e.get("timestamp", 0) or 0)) > 0
        and e.get("type", "") != "mouse.move"
    )

    # KTD-6 pinning: committed boundaries are final for the pre-pinned prefix.
    if pinned_before_ts is not None:
        prefix, tail = _split_pinned(
            windows, recording_dir, pinned_before_ts, action_ts,
        )
    else:
        prefix, tail = [], list(windows)

    settled = _settled_intervals(ledger, manifests)

    def _digests(ws: list[CandidateWindow]) -> list[WindowDigest]:
        return build_window_digests(
            ws, manifests, source,
            blocked_source=blocked_source, settled_intervals=settled,
        )

    all_windows = prefix + tail
    digests = _digests(all_windows)

    # Arbitration (KTD-4): merge-only, tail-scoped, validated; any failure or
    # invalid response → the heuristic boundaries stand (R6).
    if len(tail) >= 2 and _halt_reason(stop_event, deadline) is None:
        lines = [d.arbitration_line for d in digests[len(prefix):]]
        res = provider.call_arbitrate(lines, stripped=True)
        if not res.ok:
            log.info(
                "ondevice pipeline: arbitration failed (%s); heuristic "
                "boundaries stand", res.reason,
            )
        else:
            groups = _validate_merges(res.value, len(tail))
            if groups is None:
                log.warning(
                    "ondevice pipeline: invalid merge list rejected; "
                    "heuristic boundaries stand"
                )
            elif groups:
                tail = _apply_merges(tail, groups)
                all_windows = prefix + tail
                # A tail-scoped merge leaves the prefix windows untouched, so
                # only the tail digests need rebuilding.
                digests = digests[: len(prefix)] + _digests(tail)

    # Per-window loop: cache lookup → naming call (with context-window
    # halving) → cache write; budget + stop checks between calls (KTD-7).
    n_windows = len(all_windows)
    tasks: list[dict] = []
    failure_reasons: list[str] = []
    written_keys: list[tuple[float, float, str]] = []
    model_named = 0

    for i, digest in enumerate(digests):
        w = digest.window
        # The live day's trailing window is mutable — named, never cached.
        cacheable = digest.cacheable and not (is_live and i == n_windows - 1)

        name: str | None = None
        category: str | None = None
        if digest.usable and ledger is not None:
            hit = ledger.lookup_window_name(
                w.start_ts, w.end_ts, digest.digest_hash,
            )
            if hit is not None:
                name, category = hit
        if name is None and not digest.usable:
            # Strip could not be established / no in-span content: fail closed
            # with a mechanical name and NO model call (KTD-5).
            failure_reasons.append(REASON_DIGEST_UNUSABLE)
        elif name is None:
            halt = _halt_reason(stop_event, deadline)
            if halt is not None:
                failure_reasons.append(halt)
            else:
                log.info(
                    "ondevice pipeline: naming window %d/%d", i + 1, n_windows,
                )
                name, category, reason = _name_window(
                    provider, digest, stop_event, deadline,
                )
                if name is None:
                    failure_reasons.append(reason)
                elif cacheable and ledger is not None:
                    # KTD-10 per-write half: the store re-checks the scrub
                    # generation inside its own transaction; a skipped store
                    # (a scrub landed mid-pass) leaves the window named but
                    # uncached this pass.
                    if ledger.store_window_name(
                        w.start_ts, w.end_ts, digest.digest_hash,
                        name, category,
                        expected_generation=generation,
                    ):
                        written_keys.append(
                            (w.start_ts, w.end_ts, digest.digest_hash)
                        )

        if name is not None:
            model_named += 1
            tasks.append(_model_task(w, digest, name, category))
        else:
            tasks.append(_mechanical_task(w, i))

    if model_named == 0:
        # KTD-1: could not produce a single model name → the sentinel, with
        # the dominant failure reason recorded out-of-band on the provider.
        provider.last_unavailable_reason = _dominant(failure_reasons)
        log.info(
            "ondevice pipeline: zero model-named windows (%s); unavailable",
            provider.last_unavailable_reason,
        )
        return PROVIDER_UNAVAILABLE

    summary, tags = _day_summary(provider, tasks, stop_event, deadline)
    result = {"tasks": tasks, "summary": summary, "tags": tags}

    # KTD-10: re-check the scrub generation inside one commit transaction; on
    # a mismatch delete this pass's cache rows and hand nothing to the sinks.
    # The distinct REASON_STALE_SCRUB rides the sentinel (mirroring how
    # REASON_STOPPED flows) so the terminal stage can retry once at finalize
    # and otherwise record the pass as provisional — never a real
    # unavailability, never a fallback.
    if ledger is not None and generation is not None:
        if not ledger.finalize_window_names(written_keys, generation):
            log.warning(
                "ondevice pipeline: a retroactive scrub landed mid-pass; "
                "discarding this pass (staleness guard)"
            )
            provider.last_unavailable_reason = REASON_STALE_SCRUB
            return PROVIDER_UNAVAILABLE

    # None after a fully-model pass; the dominant per-window failure reason
    # after a partial one (the U6 honesty seam, KTD-8).
    provider.last_unavailable_reason = _dominant(failure_reasons)
    return result


# ---------------------------------------------------------------------------
# Pinning (KTD-6)
# ---------------------------------------------------------------------------


def _split_pinned(
    windows: list[CandidateWindow],
    recording_dir: Path,
    pinned_before_ts: float,
    action_ts: list[float],
) -> tuple[list[CandidateWindow], list[CandidateWindow]]:
    """Split the pass into a final prefix and an arbitrable tail.

    The tail is every candidate window whose end lies after the pinned
    boundary — including the straddling grown trailing window, which keeps its
    full span (it is the mutable window and may re-name as it grows). The
    prefix is rebuilt from the COMMITTED unedited agent rows (their boundaries
    are final — a boundary an earlier arbitration merged must not be re-split
    by this tick's re-proposed heuristic candidates), plus, defensively, any
    heuristic window fully before the boundary that no committed span covers
    and no tail window overlaps (so footage can never drop out of assembly).
    """
    from screencap.pipeline_state import spans_overlap

    tail = [w for w in windows if w.end_ts > pinned_before_ts]
    tail_start = min((w.start_ts for w in tail), default=pinned_before_ts)

    prefix: list[CandidateWindow] = []
    covered: list[tuple[float, float]] = []
    for s, e in _committed_agent_spans(recording_dir):
        if e > pinned_before_ts or e > tail_start:
            continue
        n = bisect_right(action_ts, e) - bisect_left(action_ts, s)
        prefix.append(CandidateWindow(s, e, n))
        covered.append((s, e))
    for w in windows:
        if (
            w.end_ts <= pinned_before_ts
            and w.end_ts <= tail_start
            and not any(
                spans_overlap(w.start_ts, w.end_ts, s, e) for s, e in covered
            )
        ):
            prefix.append(w)
    prefix.sort(key=lambda w: w.start_ts)
    return prefix, tail


def _committed_agent_spans(recording_dir: Path) -> list[tuple[float, float]]:
    """The recording's committed unedited-agent task spans, sorted (KTD-6)."""
    try:
        from screencap.pipeline_state import TASK_SOURCE_AGENT, PipelineLedger

        db_path = Path(recording_dir) / "recording.db"
        if not db_path.exists():
            return []
        rows = PipelineLedger(db_path).read_task_segments()
        return sorted(
            (r.start_ts, r.end_ts)
            for r in rows
            if r.source == TASK_SOURCE_AGENT and not r.edited
        )
    except Exception:  # noqa: BLE001 — pinning must fail open, never gate
        log.debug("_committed_agent_spans failed open", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Arbitration validation + application (KTD-4)
# ---------------------------------------------------------------------------


def _validate_merges(
    merges: list[list[int]] | None, n_windows: int,
) -> list[list[int]] | None:
    """Validate merge groups: contiguous, in-range, non-overlapping.

    Returns the normalized (sorted, >=2-member) groups, ``[]`` for "keep every
    boundary", or ``None`` when the response is invalid — the caller treats
    ``None`` as an arbitration failure (heuristic boundaries stand, R6).
    Singleton groups are no-ops and are dropped, not rejected.
    """
    if merges is None:
        return None
    seen: set[int] = set()
    groups: list[list[int]] = []
    for group in merges:
        idxs = sorted(group)
        if not idxs:
            continue
        if idxs[0] < 0 or idxs[-1] >= n_windows:
            return None
        if idxs != list(range(idxs[0], idxs[-1] + 1)):
            return None  # non-contiguous
        if any(i in seen for i in idxs):
            return None  # overlapping groups
        seen.update(idxs)
        if len(idxs) >= 2:
            groups.append(idxs)
    return groups


def _apply_merges(
    windows: list[CandidateWindow], groups: list[list[int]],
) -> list[CandidateWindow]:
    """Apply validated merge groups to the (tail) window list."""
    first_of = {g[0]: g for g in groups}
    merged_members = {i for g in groups for i in g[1:]}
    out: list[CandidateWindow] = []
    for i, w in enumerate(windows):
        if i in merged_members:
            continue
        g = first_of.get(i)
        if g is None:
            out.append(w)
        else:
            last = windows[g[-1]]
            out.append(CandidateWindow(
                w.start_ts, last.end_ts,
                sum(windows[j].event_count for j in g),
            ))
    return out


# ---------------------------------------------------------------------------
# Naming (KTD-3 halving + KTD-9 hygiene)
# ---------------------------------------------------------------------------


def _name_window(
    provider, digest: WindowDigest, stop_event, deadline: float,
) -> tuple[str | None, str | None, str | None]:
    """One window's naming call(s) → ``(name, category, failure_reason)``.

    Owns the KTD-3 caller half: a ``context-window`` failure halves the digest
    payload's timeline/transcript content (re-wrapped with the stripped
    marker) at most :data:`MAX_DIGEST_HALVINGS` times, then gives up (the
    window goes mechanical). Output hygiene (KTD-9) applies HERE, before the
    result reaches the cache or assembly.

    KTD-7 inside the halving loop: EVERY iteration re-checks
    :func:`_halt_reason` first, so a stop signal (or budget exhaustion) landing
    mid-halving exits promptly with the halt pseudo-reason — the window goes
    mechanical this pass instead of burning further model calls.
    """
    from screencap.segmentation.activity_summary import attest_derived_stripped

    payload = dict(digest.payload or {})
    halvings_left = MAX_DIGEST_HALVINGS
    while True:
        halt = _halt_reason(stop_event, deadline)
        if halt is not None:
            return None, None, halt
        # Re-attest via the builder module's sanctioned helper (the stripped-
        # marker guard pins the write there); ``digest.stripped`` is the
        # derivation chain's own flag, so an unstripped digest attests False
        # and the provider's fail-closed gate refuses it.
        res = provider.call_name_window(
            attest_derived_stripped(payload, stripped=digest.stripped)
        )
        if res.ok:
            raw_name, raw_category = res.value
            name = str(raw_name).strip()[:_MAX_NAME_CHARS]
            if not name:
                return None, None, "invalid-result"
            category = str(raw_category).strip()
            if category not in _VALID_CATEGORIES:
                category = "other"
            return name, category, None
        if res.reason == "context-window" and halvings_left > 0:
            halved = _halve_payload(payload)
            if halved is None:
                return None, None, res.reason
            payload = halved
            halvings_left -= 1
            continue
        return None, None, res.reason


def _halve_payload(payload: dict) -> dict | None:
    """Halve the digest's timeline/transcript content; ``None`` when minimal.

    Drop order mirrors ``windows._trim_to_budget``: transcript first, then
    timeline entries, then per-entry detail on a lone surviving entry (the
    shared ``windows.strip_entry_detail`` vocabulary).
    """
    p = dict(payload)
    transcript = list(p.get("transcript") or [])
    if transcript:
        transcript = transcript[: len(transcript) // 2]
        if transcript:
            p["transcript"] = transcript
        else:
            p.pop("transcript", None)
        return p
    timeline = [dict(e) for e in p.get("timeline") or []]
    if len(timeline) > 1:
        p["timeline"] = timeline[: max(1, len(timeline) // 2)]
        return p
    if timeline and strip_entry_detail(timeline[0]):
        p["timeline"] = timeline
        return p
    return None


# ---------------------------------------------------------------------------
# Assembly + day summary (KTD-9)
# ---------------------------------------------------------------------------


def _model_task(
    w: CandidateWindow, digest: WindowDigest, name: str, category: str | None,
) -> dict:
    """One model-named task, mirroring ``_heuristic_local_tasks``'s shape.

    ``apps_used`` is derived Python-side from the digest (KTD-9); the
    description stays empty on this path and confidence is omitted.
    """
    apps: list[str] = []
    for entry in (digest.payload or {}).get("timeline", []):
        app = entry.get("app")
        if app and app not in apps:
            apps.append(app)
    return {
        "start_ts": float(w.start_ts),
        "end_ts": float(w.end_ts),
        "name": name,
        "derived_name": _slugify(name),
        "category": category,
        "apps_used": apps,
        "event_count": w.event_count,
        "source": SOURCE_MODEL,
    }


def _mechanical_task(w: CandidateWindow, i: int) -> dict:
    """A mechanically-named task (matches ``_heuristic_local_tasks`` naming)."""
    return {
        "start_ts": float(w.start_ts),
        "end_ts": float(w.end_ts),
        "name": f"task_{i + 1}",
        "derived_name": f"task-{i + 1}",
        "event_count": w.event_count,
        "source": SOURCE_MECHANICAL,
    }


def _day_summary(
    provider, tasks: list[dict], stop_event, deadline: float,
) -> tuple[dict, list[str]]:
    """The day-summary call over the model-named tasks, with the synthesized
    fallback (KTD-9): a failure never affects the tasks or the outcome reason.
    """
    overview: str | None = None
    tags: list[str] = []
    model_tasks = [t for t in tasks if t.get("source") == SOURCE_MODEL]
    if model_tasks and _halt_reason(stop_event, deadline) is None:
        rows = [
            {
                "name": t["name"],
                "category": t.get("category") or "other",
                "minutes": round((t["end_ts"] - t["start_ts"]) / 60.0),
            }
            for t in model_tasks
        ]
        res = provider.call_day_summary(rows)
        if res.ok:
            raw = res.value.get("overview", "")
            overview = str(raw).strip()[:_MAX_OVERVIEW_CHARS] or None
            tags = _validate_tags(list(res.value.get("tags", [])))
        else:
            log.info(
                "ondevice pipeline: day summary failed (%s); synthesizing",
                res.reason,
            )
    if overview is None:
        # The synthesized-summary shape ``validate_llm_tasks`` produces.
        overview = f"Recording with {len(tasks)} tasks."
        tags = []
    cats = [t.get("category") for t in tasks if t.get("category")]
    primary = max(set(cats), key=cats.count) if cats else "other"
    summary = {
        "overview": overview,
        "primary_focus": primary,
        "time_breakdown": {},
        "key_accomplishments": [],
    }
    return summary, tags


# ---------------------------------------------------------------------------
# Budget / stop (KTD-7) + cache plumbing
# ---------------------------------------------------------------------------


def _halt_reason(stop_event, deadline: float) -> str | None:
    """``None`` to proceed; else why no further model call may be made."""
    if stop_event is not None and stop_event.is_set():
        return REASON_STOPPED
    if _monotonic() > deadline:
        return REASON_BUDGET_EXHAUSTED
    return None


def _dominant(reasons: list[str]) -> str | None:
    """The most common failure reason of the pass (``None`` when none)."""
    if not reasons:
        return None
    return Counter(reasons).most_common(1)[0][0]


def _open_cache_ledger(recording_dir: Path) -> "PipelineLedger | None":
    """Open the recording's ledger for the naming cache; ``None`` fails open.

    Delegates to the shared :func:`screencap.pipeline_state.open_ledger_or_none`
    (schema ensured first — creates the cache + generation tables on an old
    ``recording.db``). A missing/unreadable DB → no cache and no staleness
    guard this pass — every window is a miss (memo-not-truth).
    """
    from screencap.pipeline_state import open_ledger_or_none

    return open_ledger_or_none(recording_dir)


def _settled_intervals(
    ledger: "PipelineLedger | None", manifests: list[dict],
) -> "Sequence[tuple[float, float]] | None":
    """Spans covered by stage-complete chunks (the KTD-5 determinism gate).

    Only windows fully inside these spans produce cacheable digests — a chunk
    whose stages (transcribe/export/manifest) have not completed may still
    gain a transcript, changing the digest. ``None`` (no ledger) falls back to
    ``build_window_digests``'s past-windows-cacheable default.
    """
    if ledger is None:
        return None
    try:
        from screencap.pipeline_state import StageState

        done = {
            r.chunk_index
            for r in ledger.all_chunks()
            if r.stages_state is StageState.DONE
        }
        return [
            (float(m["chunk_start"]), float(m["chunk_end"]))
            for m in manifests
            if m.get("chunk_index") in done
        ]
    except Exception:  # noqa: BLE001 — an unreadable ledger → nothing cacheable
        log.debug("_settled_intervals failed open", exc_info=True)
        return []
