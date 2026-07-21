"""Day narrative — the written, evidence-bound day diary opener (U4, R8/R9).

Composes a short prose narrative of a recording's day from its consolidated
diary BLOCKS ONLY (names + topic bullets + thread rollups) — NEVER raw screen /
transcript evidence. Bounding the input to the block projection is what makes the
narrative evidence-bound (R9): it can assert no more than the blocks already
support, so a thin day reads thin and a bullet-free day yields a names-only
narrative, never invented detail.

Two seams, mirroring U3's prose kind (KTD-4):

1. **Model narrator** — an injected ``narrator`` exposing a
   ``call_day_narrative(digest) -> CallResult[str]`` verb (the DIARY_PROSE
   on-device-class provider from :func:`screencap.segmentation.routing.build_prose_provider`).
   It sees ONLY the block digest (wrapped with the ``stripped`` attestation and
   riding the shared ``_halve_payload`` budget seam), never raw evidence.
2. **Honest heuristic** — when no narrator is available (or it declines), a
   deterministic prose composition over the SAME block projection: block names,
   with their bullets when present. Every fall to the heuristic records an
   observable ``reason`` marker (KTD-10) so a degrade is never silent.

The returned free text is ALWAYS run through
:func:`screencap.segmentation.generation_finish.sanitize_answer` (the prose
analogue of ``sanitize_bullets`` — HTML-escapes injected markup losslessly)
before it leaves this module, so attacker-influenceable block names/bullets can
never carry markup into the day view or the read verb.

**Staleness (KTD-6).** :func:`narrative_fingerprint` keys off the CLOSED block set
ONLY — the live ``is_open`` trailing block is excluded — so the narrative
regenerates at each block-close boundary (a new closed block, a rename, changed
bullets) but NOT on every unchanged 300s tick and NOT merely because the open
block kept growing. This is a SEPARATE fingerprint from U2's consolidation
fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# The narrative-generation reason/marker vocabulary (KTD-10). ``None`` means a
# model narrated (no degrade); any other value marks the honest heuristic
# fallback, so a degrade is always observable in the persisted row.
REASON_NO_MODEL = "no-model"
REASON_NARRATOR_ERROR = "narrator-error"
# ``EMPTY`` is the guard reason for "no closed named block to narrate" — the
# caller uses it to skip persisting a row (there is nothing to say yet).
REASON_EMPTY = "empty"

# Bound the block list a single day can feed the narrator (DoS / budget guard);
# mirrors the fixed-size-digest discipline of the naming / bullet calls.
_MAX_NARRATIVE_BLOCKS = 60


@dataclass(frozen=True)
class NarrativeBlock:
    """A normalized projection of ONE diary block for narrative generation.

    Carries only what the narrative is allowed to assert (KTD-4/R9): the block's
    ``name`` and ``bullets`` (evidence-bound topic summaries), its span (for
    rollups), its ``thread_id`` (same-work linkage), and ``is_open`` (the live
    trailing block, excluded from the closed-set fingerprint). ``key`` is the
    stable identity used in the fingerprint — the block's ``block_id`` when it has
    one, else an index-derived key, so a rename/close changes the fingerprint.
    """

    key: str
    name: str
    bullets: tuple[str, ...] = ()
    start_ts: float = 0.0
    end_ts: float = 0.0
    thread_id: str | None = None
    is_open: bool = False


@dataclass(frozen=True)
class NarrativeResult:
    """A generated narrative + its observable source/fallback marker (KTD-10).

    ``text`` is the sanitized prose (possibly empty on the ``EMPTY`` guard).
    ``reason`` is ``None`` when a model narrated (no degrade) and a marker string
    on the honest heuristic fallback, so the persisted row always records HOW the
    narrative was produced — never a silent degrade.
    """

    text: str
    reason: str | None = None


# ---------------------------------------------------------------------------
# Projection — block rows -> the narrative input (names + bullets + rollups).
# ---------------------------------------------------------------------------


def project_rows(rows) -> list[NarrativeBlock]:
    """Project ``TaskSegmentRow`` rows to :class:`NarrativeBlock`s (pure).

    Parses each row's topic bullets out of its ``metadata`` blob (U1/U3), keeps
    the block's span + thread + open flag, and derives the stable fingerprint
    ``key`` from ``block_id`` (falling back to the row's ``task_index`` for a
    user-curated block created without one). Rows with no usable data are still
    projected — the closed/named filter is applied later so the fingerprint and
    the digest see one consistent set.
    """
    from screencap.pipeline_state import _parse_wire_bullets

    out: list[NarrativeBlock] = []
    for r in rows:
        bullets = _parse_wire_bullets(getattr(r, "metadata", None)) or []
        block_id = getattr(r, "block_id", None)
        key = block_id or f"idx:{getattr(r, 'task_index', len(out))}"
        out.append(
            NarrativeBlock(
                key=str(key),
                name=str(getattr(r, "name", "") or ""),
                bullets=tuple(str(b) for b in bullets if str(b).strip()),
                start_ts=float(getattr(r, "start_ts", 0.0) or 0.0),
                end_ts=float(getattr(r, "end_ts", 0.0) or 0.0),
                thread_id=getattr(r, "thread_id", None),
                is_open=bool(getattr(r, "is_open", False)),
            )
        )
    return out


def closed_named_blocks(blocks) -> list[NarrativeBlock]:
    """The blocks a narrative may cover: CLOSED (not ``is_open``) AND named.

    Excludes the live trailing block (``is_open`` — its evidence is still
    growing, KTD-6) and any block in the honest UNNAMED state (empty name —
    nothing to narrate, R9), start-ordered so the composition reads
    chronologically.
    """
    named = [b for b in blocks if not b.is_open and b.name.strip()]
    named.sort(key=lambda b: (b.start_ts, b.end_ts))
    return named


# ---------------------------------------------------------------------------
# Staleness fingerprint (KTD-6) — CLOSED blocks only.
# ---------------------------------------------------------------------------


def narrative_fingerprint(blocks) -> str | None:
    """A stable change-key over the CLOSED, named block set (KTD-6).

    Hashes the sorted ``(key, name, bullets)`` triples of every closed named
    block, so the narrative regenerates on a new closed block, a rename, or
    changed bullets — but NOT on an unchanged tick and NOT when only the excluded
    live ``is_open`` block grew. Returns ``None`` when there is no closed named
    block yet (nothing to narrate → the caller writes no row).
    """
    named = closed_named_blocks(blocks)
    if not named:
        return None
    items = sorted(
        [b.key, b.name, list(b.bullets)] for b in named
    )
    payload = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Digest — the ONLY thing the narrator sees (names + bullets + rollups).
# ---------------------------------------------------------------------------


def _thread_rollups(blocks) -> tuple[dict[str, float], dict[str, int]]:
    """Per-thread ``(total_seconds, sitting_count)`` over ``blocks`` (read-time)."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for b in blocks:
        if not b.thread_id:
            continue
        totals[b.thread_id] = totals.get(b.thread_id, 0.0) + (b.end_ts - b.start_ts)
        counts[b.thread_id] = counts.get(b.thread_id, 0) + 1
    return totals, counts


def build_digest(blocks) -> dict:
    """The fixed-shape narrative input: block names + bullets + thread rollups.

    Rides the ``timeline`` key so the shared
    :func:`screencap.segmentation.ondevice_pipeline._halve_payload` budget seam
    can trim it. Each entry carries ONLY the block ``name``, its ``minutes``, its
    ``bullets`` (when present), and — for a block in a multi-sitting thread — the
    thread ``rollup`` (total minutes + sitting count). NEVER raw evidence, never
    the per-window fragment names, never anything that scales with the day beyond
    the (bounded) block count.
    """
    totals, counts = _thread_rollups(blocks)
    timeline: list[dict] = []
    for b in blocks[:_MAX_NARRATIVE_BLOCKS]:
        entry: dict = {
            "name": b.name,
            "minutes": round((b.end_ts - b.start_ts) / 60.0),
        }
        if b.bullets:
            entry["bullets"] = list(b.bullets)
        if b.thread_id and counts.get(b.thread_id, 0) >= 2:
            entry["thread"] = {
                "total_minutes": round(totals[b.thread_id] / 60.0),
                "sittings": counts[b.thread_id],
            }
        timeline.append(entry)
    return {"kind": "day-narrative", "timeline": timeline}


# ---------------------------------------------------------------------------
# Generation — model narrator (optional) then the honest heuristic.
# ---------------------------------------------------------------------------


def build_narrative(
    blocks, *, narrator=None, stop_event=None, deadline=None,
) -> NarrativeResult:
    """Compose the day narrative from the block projection (U4, R8/R9).

    Filters to the CLOSED, named blocks (R9 evidence bound + KTD-6 open-block
    exclusion), builds the names+bullets+rollups digest, and tries the injected
    ``narrator`` (the DIARY_PROSE on-device-class seam). On any narrator absence /
    decline / error it falls to the deterministic heuristic over the SAME
    projection and records the observable fallback ``reason`` (KTD-10). The
    returned text is ALWAYS sanitized (``sanitize_answer``) before it leaves here.
    Returns an ``EMPTY``-reason empty result when there is no closed named block —
    the caller persists nothing.
    """
    from screencap.segmentation.generation_finish import sanitize_answer

    named = closed_named_blocks(blocks)
    if not named:
        return NarrativeResult("", REASON_EMPTY)

    digest = build_digest(named)
    text, reason = _call_narrator(digest, narrator, stop_event, deadline)
    if text is None:
        text = _heuristic_narrative(named)
    else:
        reason = None  # a model narrated — no degrade to record.
    return NarrativeResult(sanitize_answer(text), reason)


def _call_narrator(
    digest: dict, narrator, stop_event, deadline,
) -> tuple[str | None, str | None]:
    """One day-narrative model call → ``(text, fallback_reason)`` (pre-sanitize).

    ``text`` is ``None`` on any failure (so the caller falls to the heuristic);
    ``fallback_reason`` is the observable degrade marker in that case, and ``None``
    only when a real model text is returned. Mirrors
    :func:`screencap.segmentation.consolidate._call_block_bullets`: a
    ``context-window`` failure halves the digest (bounded by
    ``MAX_DIGEST_HALVINGS``) before giving up, each iteration re-checks the halt
    (stop / budget) first, and the ``stripped`` attestation is attached HERE (the
    block projection derives from already-stripped per-window evidence, so it
    attests ``True`` honestly). ``narrator is None`` (no model verb) →
    :data:`REASON_NO_MODEL`.
    """
    if narrator is None:
        return None, REASON_NO_MODEL

    from screencap.segmentation.activity_summary import attest_derived_stripped
    from screencap.segmentation.ondevice_pipeline import (
        MAX_DIGEST_HALVINGS,
        _halt_reason,
        _halve_payload,
    )

    payload = digest
    halvings_left = MAX_DIGEST_HALVINGS
    while True:
        if deadline is not None:
            halt = _halt_reason(stop_event, deadline)
            if halt is not None:
                return None, halt
        try:
            res = narrator.call_day_narrative(
                attest_derived_stripped(payload, stripped=True)
            )
        except Exception as exc:  # noqa: BLE001 — a narrator error is unavailability
            log.debug("narrative: narrator raised (%s)", exc)
            return None, REASON_NARRATOR_ERROR
        if res.ok:
            text = str(res.value or "").strip()
            if not text:
                return None, "invalid-result"
            return text, None
        if res.reason == "context-window" and halvings_left > 0:
            halved = _halve_payload(payload)
            if halved is None:
                return None, res.reason
            payload = halved
            halvings_left -= 1
            continue
        return None, res.reason


def _heuristic_narrative(blocks) -> str:
    """The honest heuristic narrative — names + bullets, never invented (R9/AE3).

    One clause per closed named block, in time order: the block name, followed by
    its topic bullets when it has them. A bullet-free day yields a pure names-only
    narrative; nothing is asserted beyond what the block projection carries. The
    caller sanitizes the result (so an injected markup name/bullet is neutralized
    in the returned text).
    """
    parts: list[str] = []
    for b in blocks:
        name = b.name.strip()
        if not name:
            continue
        if b.bullets:
            parts.append(f"{name}: {'; '.join(b.bullets)}.")
        else:
            parts.append(f"{name}.")
    return " ".join(parts)
