"""Block consolidation + thread linking (day diary, U2).

Turns the fine per-window task fragments the segmentation pipeline produces into
a small set of coherent, named diary **blocks** with stable identity and same-day
thread links. Runs both live (incremental tick) and at finalize through ONE body
(:func:`consolidate`), so the two paths cannot drift (R10 / KTD-6).

The pure grouping / clamp / thread / trim / identity core is deterministic and
model-free (unit-testable without a provider). The single model touch — one
small naming call per MERGED block over that block's small digest — rides the
:func:`screencap.segmentation.ondevice_pipeline._halve_payload` budget seam and
the ``call_name_window`` verb, behind an injected ``namer`` seam so tests script
it (KTD-1 / KTD-2 / KTD-5 / KTD-6 / KTD-7). The day-level *merge arbitration* the
token ceiling forbids is done HEURISTICALLY here (adjacency + app/topic
affinity), so the model never sees an input that scales with the whole day.

Key behaviors (SCR day-diary plan KTD numbering):

- **KTD-1** — blocks are written as the agent rows of ``pipeline_task_segments``
  by the caller (:func:`consolidate` returns the enriched task dicts; the
  terminal stage persists them via the existing scoped ``replace_task_segments``).
  A block that consolidates a SINGLE fragment carries that fragment's fields
  through verbatim (name/category/confidence/metadata) — the fine per-window name
  already describes the work; only a MERGE of >=2 fragments needs a fresh name.
- **KTD-2** — each block gets a stable ``block_id`` preserved across re-carves by
  span-overlap + evidence similarity against the prior pass's rows, so deep links
  / threads survive ``task_index`` renumbering. Adding one new chunk of the same
  work extends the trailing block WITHOUT changing its id.
- **KTD-5** — blocks clamp at local midnight the way ``day_segments`` clamps
  recordings (split, each part owned by its day); threads never span the split.
- **KTD-7** — the carve-out around a protected (user-curated) span TRIMS agent
  block boundaries to abut it cleanly instead of DROPPING the whole block.
- **KTD-10** — a block whose naming fails (no model / budget) gets the honest
  UNNAMED state (empty name + an observable ``name_fallback`` marker), never a
  mechanical ``task_N`` name and never a raw window-title slug.

``thread_id`` is **recording-scoped only** (never cross-recording — that is
deferred): the day-level rollups are computed at read time keyed on
``(recording, thread_id)``, so tokens minted here for different recordings can
never falsely merge.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables — the heuristic merge-arbitration knobs. Small, explicit, and pure.
# ---------------------------------------------------------------------------

# Max inter-fragment gap (seconds) that a single block may bridge. Beyond it the
# next fragment starts a NEW block even for identical work — a later sitting of
# the same work (AE2) stays its own block and links via ``thread_id`` instead of
# swallowing the intervening hours. 20 min bridges the short breaks WITHIN a
# working session without merging a morning and an afternoon sitting.
GROUP_GAP_S = 1200.0

# Jaccard token overlap above which two names read as the same work. Kept modest
# so "Brainstorming diary schema" / "Diary schema brainstorm" group, while
# "Implement auth module" / "Coordinate PR review" (overlap 0) do not.
_NAME_SIM_GROUP = 0.34
# Threads link across bigger gaps, so the same-work bar is a touch higher.
_NAME_SIM_THREAD = 0.5

_SECONDS_PER_DAY = 86400

# Generic name tokens that carry NO work signal — dropped before affinity so a
# shared "the"/"view"/"untitled" never reads as same-work evidence.
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "and", "in", "on", "for", "with", "view",
    "window", "untitled", "task", "new", "document", "file", "page", "tab",
})

# The honest UNNAMED state (KTD-10): an empty name the wire/app renders as the
# honest "couldn't name" state, plus an observable marker recorded in metadata.
UNNAMED = ""
NAME_FALLBACK_KEY = "name_fallback"

# Topic bullets (U3, R4/R6): 2–4 short evidence-bound bullets per block. The
# ``bullets_fallback`` marker is the KTD-10 observable degrade signal for the
# HONEST app-level heuristic path (model unavailable / no content signal / budget
# exhausted) — a model-sourced bullet set leaves it unset. Both ride the
# ``metadata`` blob under U1's field-scoped protection (``EDITED_FIELD_BULLETS``).
MAX_BULLETS = 4
BULLETS_KEY = "bullets"
BULLETS_FALLBACK_KEY = "bullets_fallback"

# Split description prose into sentence-ish bullets on end-of-sentence marks.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Per-block naming provenance markers surfaced on the returned dict's ``source``.
# Mirror the on-device pipeline's vocabulary so every sink sees ONE set.
SOURCE_MODEL = "ondevice_model"
SOURCE_MECHANICAL = "idle_gap_heuristic"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Fragment:
    """A normalized view of one fine per-window task dict."""

    start_ts: float
    end_ts: float
    name: str
    category: str | None
    apps: tuple[str, ...]
    source: str | None
    confidence: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def tokens(self) -> frozenset[str]:
        return _name_tokens(self.name)

    @property
    def is_model_named(self) -> bool:
        """True iff this fragment carries a real (non-mechanical) work name.

        A cloud/BYO provider emits no ``source`` marker (its names are model
        work-names), so ``None`` counts as model-named; only the explicit
        mechanical idle-gap marker counts as unnamed evidence.
        """
        return self.source != SOURCE_MECHANICAL


@dataclass
class Block:
    """A coherent diary work block — one or more grouped fragments."""

    fragments: list[Fragment]
    name: str = UNNAMED
    category: str | None = None
    confidence: str | None = None
    metadata: dict = field(default_factory=dict)
    block_id: str | None = None
    thread_id: str | None = None
    is_open: bool = False
    name_fallback: str | None = None
    bullets: list[str] = field(default_factory=list)
    bullets_fallback: str | None = None

    @property
    def start_ts(self) -> float:
        return min(f.start_ts for f in self.fragments)

    @property
    def end_ts(self) -> float:
        return max(f.end_ts for f in self.fragments)

    @property
    def apps(self) -> tuple[str, ...]:
        seen: list[str] = []
        for f in self.fragments:
            for a in f.apps:
                if a and a not in seen:
                    seen.append(a)
        return tuple(seen)

    @property
    def is_single(self) -> bool:
        return len(self.fragments) == 1

    def evidence_tokens(self) -> frozenset[str]:
        toks: set[str] = set()
        for f in self.fragments:
            toks |= f.tokens
        return frozenset(toks)


# ---------------------------------------------------------------------------
# Affinity helpers (pure)
# ---------------------------------------------------------------------------


def _name_tokens(name: str | None) -> frozenset[str]:
    if not name:
        return frozenset()
    words = re.findall(r"[a-z0-9]+", str(name).lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 1)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _same_work(
    a_tokens: frozenset[str],
    a_apps: tuple[str, ...],
    b_tokens: frozenset[str],
    b_apps: tuple[str, ...],
    *,
    threshold: float,
) -> bool:
    """POSITIVE same-work evidence between two evidence bundles.

    Requires real name tokens on BOTH sides (mechanical / unnamed fragments have
    none, so they never read as same work). Same work iff the name Jaccard clears
    ``threshold``, OR the two share an app AND have SOME token overlap (a shared
    tool plus a shared topic word). Never merges on adjacency or a shared app
    alone.
    """
    if not a_tokens or not b_tokens:
        return False
    sim = _jaccard(a_tokens, b_tokens)
    if sim >= threshold:
        return True
    shares_app = bool(set(a_apps) & set(b_apps))
    return shares_app and sim > 0.0


# ---------------------------------------------------------------------------
# 1. Grouping core (KTD-1 heuristic merge arbitration)
# ---------------------------------------------------------------------------


def group_fragments(fragments) -> list[Block]:
    """Group time-ordered fine fragments into coherent blocks (pure).

    Greedy left-to-right: a fragment extends the current block when it starts
    within :data:`GROUP_GAP_S` of the block's end AND carries positive same-work
    evidence against the block's accumulated evidence (:func:`_same_work`);
    otherwise it opens a new block. Deterministic and model-free — this is the
    merge arbitration the token ceiling forbids doing in one whole-day model call.
    """
    frags = sorted(
        (_as_fragment(f) for f in fragments if isinstance(f, (dict, Fragment))),
        key=lambda f: (f.start_ts, f.end_ts),
    )
    blocks: list[Block] = []
    for frag in frags:
        if blocks and _extends(blocks[-1], frag):
            blocks[-1].fragments.append(frag)
        else:
            blocks.append(Block(fragments=[frag]))
    return blocks


def _extends(block: Block, frag: Fragment) -> bool:
    gap = frag.start_ts - block.end_ts
    if gap > GROUP_GAP_S:
        return False
    return _same_work(
        block.evidence_tokens(), block.apps, frag.tokens, frag.apps,
        threshold=_NAME_SIM_GROUP,
    )


def _as_fragment(f) -> Fragment:
    if isinstance(f, Fragment):
        return f
    apps = f.get("apps_used") or f.get("apps") or []
    meta = {
        k: f[k]
        for k in ("description", "apps_used", "derived_name")
        if k in f
    }
    return Fragment(
        start_ts=float(f.get("start_ts", 0.0)),
        end_ts=float(f.get("end_ts", 0.0)),
        name=str(f.get("name", "")),
        category=f.get("category"),
        apps=tuple(str(a) for a in apps if a),
        source=f.get("source"),
        confidence=f.get("confidence"),
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# 2. Midnight clamp (KTD-5)
# ---------------------------------------------------------------------------


def clamp_blocks_at_midnight(blocks: list[Block], tz_offset_s: int) -> list[Block]:
    """Split every block that straddles a local midnight (pure, KTD-5).

    A block spanning local midnight becomes two blocks, each owned by its day —
    mirroring how ``day_segments`` clamps a midnight-spanning recording. Local
    midnight epochs are the instants ``t`` with ``(t + tz_offset_s) % 86400 == 0``
    (``tz_offset_s`` = seconds EAST of UTC, matching ``day_bounds``). The pieces
    share the block's name/evidence but are downstream treated as separate blocks
    (distinct ``day_key`` -> never threaded together).
    """
    out: list[Block] = []
    for block in blocks:
        cuts = _midnights_within(block.start_ts, block.end_ts, tz_offset_s)
        if not cuts:
            out.append(block)
            continue
        bounds = [block.start_ts, *cuts, block.end_ts]
        for lo, hi in zip(bounds, bounds[1:]):
            pieces = [
                _clip_fragment(f, lo, hi) for f in block.fragments
                if f.start_ts < hi and f.end_ts > lo
            ]
            pieces = [p for p in pieces if p is not None]
            if not pieces:
                continue
            out.append(Block(
                fragments=pieces, name=block.name, category=block.category,
                confidence=block.confidence, metadata=dict(block.metadata),
                name_fallback=block.name_fallback,
            ))
    return out


def _midnights_within(start: float, end: float, tz_offset_s: int) -> list[float]:
    """Local-midnight epochs strictly inside ``(start, end)``."""
    first_k = int((start + tz_offset_s) // _SECONDS_PER_DAY) + 1
    cuts: list[float] = []
    k = first_k
    while True:
        m = k * _SECONDS_PER_DAY - tz_offset_s
        if m >= end:
            break
        if m > start:
            cuts.append(float(m))
        k += 1
    return cuts


def _clip_fragment(frag: Fragment, lo: float, hi: float) -> Fragment | None:
    s = max(frag.start_ts, lo)
    e = min(frag.end_ts, hi)
    if e <= s:
        return None
    return Fragment(
        start_ts=s, end_ts=e, name=frag.name, category=frag.category,
        apps=frag.apps, source=frag.source, confidence=frag.confidence,
        metadata=dict(frag.metadata),
    )


def _day_key(ts: float, tz_offset_s: int) -> int:
    return int((ts + tz_offset_s) // _SECONDS_PER_DAY)


# ---------------------------------------------------------------------------
# 3. Naming (single fragment -> preserve; merge -> one small model call)
# ---------------------------------------------------------------------------


def name_blocks(blocks: list[Block], *, namer=None, stop_event=None, deadline=None) -> None:
    """Assign each block a work name IN PLACE (KTD-1 / KTD-10).

    * SINGLE-fragment block — carry the fragment through: a model-named fragment
      keeps its work name (+ category/confidence/metadata); a MECHANICAL fragment
      (idle-gap ``task_N``) gets the honest UNNAMED state instead of surfacing a
      mechanical name (R2). The recording-level outcome vocabulary carries the
      honesty signal for a mechanical-only day.
    * MERGED block — one small ``call_name_window`` over the block's digest via
      the injected ``namer`` seam (with the ``_halve_payload`` budget seam). On
      any naming failure (no model / budget / bad result) the block gets the
      honest UNNAMED state + an observable ``name_fallback`` marker — never a
      ``task_N`` and never a raw window-title slug.
    """
    for block in blocks:
        if block.is_single:
            _name_single(block)
        else:
            _name_merged(block, namer, stop_event, deadline)


def _name_single(block: Block) -> None:
    frag = block.fragments[0]
    if frag.is_model_named and _name_tokens(frag.name):
        block.name = frag.name
        block.category = frag.category
        block.confidence = frag.confidence
        block.metadata = dict(frag.metadata)
    else:
        # Mechanical / unnamed fragment: honest unnamed state, NOT task_N (R2).
        block.name = UNNAMED
        block.category = frag.category
        block.name_fallback = "mechanical"
        block.metadata = {}


def _name_merged(block: Block, namer, stop_event, deadline) -> None:
    name, category, reason = _call_block_namer(block, namer, stop_event, deadline)
    if name is not None:
        block.name = name
        block.category = category
        block.confidence = None
        block.metadata = {"apps_used": list(block.apps)}
    else:
        block.name = UNNAMED
        block.category = _dominant_category(block)
        block.name_fallback = reason or "unavailable"
        block.metadata = {}


def _call_block_namer(
    block: Block, namer, stop_event, deadline,
) -> tuple[str | None, str | None, str | None]:
    """One block's naming call(s) -> ``(name, category, failure_reason)``.

    Mirrors ``ondevice_pipeline._name_window``: a ``context-window`` failure
    halves the block digest (bounded by ``MAX_DIGEST_HALVINGS``) before giving
    up, and every iteration re-checks the halt (stop / budget) first. Output
    hygiene (strip + 80-char cap; category allow-list) applies before the name
    leaves this seam. ``namer is None`` (no on-device model) -> unavailable.
    """
    if namer is None:
        return None, None, "no-model"

    from screencap.segmentation.activity_summary import attest_derived_stripped
    from screencap.segmentation.ondevice_pipeline import (
        MAX_DIGEST_HALVINGS,
        _halt_reason,
        _halve_payload,
    )
    from screencap.segmentation.validate import _VALID_CATEGORIES

    payload = _block_digest(block)
    halvings_left = MAX_DIGEST_HALVINGS
    while True:
        if deadline is not None:
            halt = _halt_reason(stop_event, deadline)
            if halt is not None:
                return None, None, halt
        try:
            res = namer.call_name_window(
                attest_derived_stripped(payload, stripped=True)
            )
        except Exception as exc:  # noqa: BLE001 — a namer error is unavailability, never a raise
            log.debug("consolidate: block namer raised (%s)", exc)
            return None, None, "namer-error"
        if res.ok:
            raw_name, raw_category = res.value
            name = str(raw_name).strip()[:80]
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


def _block_digest(block: Block) -> dict:
    """The small, fixed-size naming input for a merged block.

    Fragment names + categories + minutes + apps only — never anything that
    scales with the whole day. The ``stripped`` attestation is attached by
    ``attest_derived_stripped`` (the sanctioned writer); the block is derived
    from already-stripped per-window evidence, so it attests True honestly.
    """
    return {
        "kind": "block",
        "fragments": [
            {
                "name": f.name,
                "category": f.category or "other",
                "minutes": round((f.end_ts - f.start_ts) / 60.0),
            }
            for f in block.fragments
        ],
        "apps": list(block.apps),
        "minutes": round((block.end_ts - block.start_ts) / 60.0),
    }


def _dominant_category(block: Block) -> str | None:
    cats = [f.category for f in block.fragments if f.category]
    if not cats:
        return None
    return max(set(cats), key=cats.count)


# ---------------------------------------------------------------------------
# 3b. Topic bullets (U3, R4/R6/AE3 — the DIARY_PROSE kind)
# ---------------------------------------------------------------------------


def bullets_for_blocks(
    blocks: list[Block], *, bullet_provider=None, stop_event=None, deadline=None,
) -> None:
    """Attach 2–4 evidence-bound topic bullets to each block IN PLACE (U3, KTD-4).

    Per block, in priority order (each source SANITIZED before it lands — the
    single P1 chokepoint, since every source is attacker-influenceable screen /
    transcript content surfaced over MCP + the app):

    1. **Cloud description reuse** — the ``description`` text the cloud prompt
       already produced on the block's fragments (today discarded on merge),
       split into sentence-ish bullets. No new egress; it is already on-box
       evidence, gated when the segmentation ran.
    2. **On-device model call** — one small ``call_block_bullets`` over the
       block's evidence digest via the injected ``bullet_provider`` seam, reusing
       the naming call's budget-halving (``_halve_payload`` / ``_halt_reason`` /
       :data:`MAX_DIGEST_HALVINGS`).
    3. **App/window-level heuristic** — when neither is available (no model / no
       content signal), bullets naming only the apps used (R6/AE3, never invented
       specifics), plus an observable ``bullets_fallback`` marker (KTD-10).

    Bullets DERIVE from the evidence digest (fragment descriptions / names /
    apps), NEVER from the downstream — possibly gate-blanked — block name, so a
    bullet can never assert content a confidence gate rejected (KTD-4).
    """
    from screencap.segmentation.sanitize import (
        _MAX_NAME_LEN,
        _clean_text,
        sanitize_bullets,
    )

    for block in blocks:
        # Sanitize the block NAME at the same per-block chokepoint as bullets: the
        # merged model name (str(raw_name).strip()[:80]) is otherwise unsanitized
        # and reaches diary_fts / diary.search snippets / the MCP task mirrors and
        # the narrative digest verbatim — a prompt-injection sink sanitize.py
        # exists to close. Legitimate names pass through unchanged; only control
        # chars / <...> markup are stripped.
        block.name = _clean_text(block.name, _MAX_NAME_LEN)
        raw, fallback = _block_bullets(block, bullet_provider, stop_event, deadline)
        block.bullets = sanitize_bullets(raw)
        # The marker is recorded whenever the block fell to the honest heuristic,
        # even if sanitizing left the app-level list empty — the degrade must
        # stay observable (KTD-10).
        if fallback is not None:
            block.bullets_fallback = fallback


def _block_bullets(
    block: Block, bullet_provider, stop_event, deadline,
) -> tuple[list[str], str | None]:
    """One block's bullets → ``(raw_bullets, fallback_marker)`` (pre-sanitize).

    ``fallback_marker`` is ``None`` on a model/description source and a reason
    string on the honest app-level heuristic path.
    """
    # 1. Cloud-produced descriptions already in the evidence → reuse.
    desc_bullets = _bullets_from_descriptions(block)
    if desc_bullets:
        return desc_bullets, None
    # 2. On-device per-block model call over the block's evidence digest.
    model_bullets, reason = _call_block_bullets(
        block, bullet_provider, stop_event, deadline,
    )
    if model_bullets:
        return model_bullets, None
    # 3. Honest heuristic: app/window-level bullets, never invented specifics.
    return _heuristic_bullets(block), (reason or "no-content")


def _bullets_from_descriptions(block: Block) -> list[str]:
    """Evidence-bound bullets from the fragments' cloud-produced descriptions.

    Reads each fragment's ``description`` (the cloud prompt's 3–5 sentence output
    carried through on the fragment evidence), splits it into sentence-ish
    bullets, dedupes case-insensitively preserving order, and caps at
    :data:`MAX_BULLETS`. Empty when no fragment carries a description (the
    on-device path, where descriptions are omitted to fit the token window).
    """
    bullets: list[str] = []
    seen: set[str] = set()
    for frag in block.fragments:
        desc = frag.metadata.get("description")
        if not isinstance(desc, str) or not desc.strip():
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(desc.strip()):
            text = sentence.strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            bullets.append(text)
            if len(bullets) >= MAX_BULLETS:
                return bullets
    return bullets


def _call_block_bullets(
    block: Block, bullet_provider, stop_event, deadline,
) -> tuple[list[str] | None, str | None]:
    """One block's on-device bullet call(s) → ``(bullets, failure_reason)``.

    Mirrors :func:`_call_block_namer`: a ``context-window`` failure halves the
    block's bullet digest (bounded by ``MAX_DIGEST_HALVINGS``) before giving up,
    and every iteration re-checks the halt (stop / budget) first.
    ``bullet_provider is None`` (no on-device model) → unavailable.
    """
    if bullet_provider is None:
        return None, "no-model"

    from screencap.segmentation.activity_summary import attest_derived_stripped
    from screencap.segmentation.ondevice_pipeline import (
        MAX_DIGEST_HALVINGS,
        _halt_reason,
        _halve_payload,
    )

    payload = _bullet_digest(block)
    halvings_left = MAX_DIGEST_HALVINGS
    while True:
        if deadline is not None:
            halt = _halt_reason(stop_event, deadline)
            if halt is not None:
                return None, halt
        try:
            res = bullet_provider.call_block_bullets(
                attest_derived_stripped(payload, stripped=True)
            )
        except Exception as exc:  # noqa: BLE001 — a provider error is unavailability
            log.debug("consolidate: block bullet provider raised (%s)", exc)
            return None, "bullets-error"
        if res.ok:
            bullets = [
                str(b).strip() for b in (res.value or []) if str(b).strip()
            ]
            if not bullets:
                return None, "invalid-result"
            return bullets[:MAX_BULLETS], None
        if res.reason == "context-window" and halvings_left > 0:
            halved = _halve_payload(payload)
            if halved is None:
                return None, res.reason
            payload = halved
            halvings_left -= 1
            continue
        return None, res.reason


def _bullet_digest(block: Block) -> dict:
    """The small, fixed-size bullet-generation input for a block.

    A per-fragment ``timeline`` of work-name + category + minutes (+ any carried
    description) plus the apps — evidence only, NEVER the downstream block name
    (KTD-4) and never anything that scales with the whole day. Uses the
    ``timeline`` key so the shared ``_halve_payload`` budget seam can trim it.
    """
    timeline: list[dict] = []
    for frag in block.fragments:
        entry: dict = {
            "name": frag.name,
            "category": frag.category or "other",
            "minutes": round((frag.end_ts - frag.start_ts) / 60.0),
        }
        desc = frag.metadata.get("description")
        if isinstance(desc, str) and desc.strip():
            entry["description"] = desc.strip()
        timeline.append(entry)
    return {
        "kind": "block-bullets",
        "timeline": timeline,
        "apps": list(block.apps),
    }


def _heuristic_bullets(block: Block) -> list[str]:
    """App/window-level bullets — the honest no-model / no-content floor (R6/AE3).

    Names ONLY the apps the block spanned ("Worked in Xcode") — no invented
    specifics, no fragment work-names, no block name. Empty when the block
    carries no app signal (the marker still records the degrade).
    """
    labels: list[str] = []
    for app in block.apps:
        label = _app_label(app)
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= MAX_BULLETS:
            break
    return [f"Worked in {label}" for label in labels]


def _app_label(bundle_id: str) -> str:
    """A human-ish app label from a bundle id — the last dotted segment."""
    if not bundle_id:
        return ""
    return str(bundle_id).rsplit(".", 1)[-1].strip()


# ---------------------------------------------------------------------------
# 4. Thread linking (recording-scoped, day-scoped) — KTD-2 / KTD-5
# ---------------------------------------------------------------------------


def link_threads(blocks: list[Block], *, recording_name: str, tz_offset_s: int) -> None:
    """Link same-work blocks WITHIN a recording+day into threads IN PLACE.

    A thread is minted only for >=2 blocks of the same work on the SAME local day
    (KTD-5: threads never span the midnight split). ``thread_id`` is a
    recording-scoped opaque token (KTD-2) — a lone block, or a block with no
    same-work sibling that day, gets none. Cross-recording linking is deferred:
    day-level rollups are computed at read time keyed on ``(recording, thread)``,
    so per-recording tokens can never falsely merge.
    """
    by_day: dict[int, list[int]] = {}
    for i, b in enumerate(blocks):
        by_day.setdefault(_day_key(b.start_ts, tz_offset_s), []).append(i)

    for members in by_day.values():
        _link_within_day([blocks[i] for i in members], recording_name)


def _link_within_day(day_blocks: list[Block], recording_name: str) -> None:
    # Union-find over same-work affinity so 3 sittings of one work share one id.
    parent = list(range(len(day_blocks)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(day_blocks)):
        for j in range(i + 1, len(day_blocks)):
            if _blocks_same_work(day_blocks[i], day_blocks[j]):
                parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(len(day_blocks)):
        groups.setdefault(find(i), []).append(i)

    for members in groups.values():
        if len(members) < 2:
            continue
        token = f"thr_{recording_name}_{uuid.uuid4().hex[:12]}"
        for i in members:
            day_blocks[i].thread_id = token


def _blocks_same_work(a: Block, b: Block) -> bool:
    if not a.name or not b.name or a.name_fallback or b.name_fallback:
        return False  # unnamed / honest-fallback blocks carry no thread evidence
    return _same_work(
        _name_tokens(a.name), a.apps, _name_tokens(b.name), b.apps,
        threshold=_NAME_SIM_THREAD,
    )


# ---------------------------------------------------------------------------
# 5. Protection TRIM (KTD-7) — trim, don't drop, around a curated span
# ---------------------------------------------------------------------------


def trim_blocks_to_protected(
    blocks: list[Block], protected_spans: list[tuple[float, float]],
) -> list[Block]:
    """Trim agent block boundaries to abut protected spans cleanly (KTD-7, pure).

    Subtracts every protected ``[p_start, p_end)`` from each block's span. A
    protected span at a block's EDGE trims that edge; one in the block's INTERIOR
    splits it into two abutting pieces; a block wholly inside a protected span
    yields nothing (dropped). Pieces inherit the block's name/category/thread —
    identity (:func:`assign_block_ids`) is (re)assigned afterward over the final
    pieces, so a split hands one piece the prior id and mints a fresh one for the
    other (never disturbing untouched neighbors).
    """
    if not protected_spans:
        return blocks
    out: list[Block] = []
    for block in blocks:
        for lo, hi in _subtract_spans(block.start_ts, block.end_ts, protected_spans):
            pieces = [
                _clip_fragment(f, lo, hi) for f in block.fragments
                if f.start_ts < hi and f.end_ts > lo
            ]
            pieces = [p for p in pieces if p is not None]
            if not pieces:
                # The trimmed sub-interval covers a gap between fragments; keep
                # the span as a single synthetic fragment so no footage silently
                # vanishes and the abutting boundary is preserved.
                pieces = [_span_fragment(block, lo, hi)]
            out.append(Block(
                fragments=pieces, name=block.name, category=block.category,
                confidence=block.confidence, metadata=dict(block.metadata),
                thread_id=block.thread_id, name_fallback=block.name_fallback,
            ))
    return out


def _span_fragment(block: Block, lo: float, hi: float) -> Fragment:
    ref = block.fragments[0]
    return Fragment(
        start_ts=lo, end_ts=hi, name=block.name, category=block.category,
        apps=block.apps, source=ref.source, confidence=block.confidence,
        metadata=dict(ref.metadata),
    )


def _subtract_spans(
    start: float, end: float, protected: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """``[start, end)`` minus every protected span -> the surviving sub-intervals."""
    segments = [(start, end)]
    for p_start, p_end in protected:
        nxt: list[tuple[float, float]] = []
        for s, e in segments:
            if p_end <= s or p_start >= e:
                nxt.append((s, e))  # disjoint
                continue
            if s < p_start:
                nxt.append((s, p_start))  # keep the left remainder
            if p_end < e:
                nxt.append((p_end, e))  # keep the right remainder
        segments = nxt
    return [(s, e) for s, e in segments if e > s]


# ---------------------------------------------------------------------------
# 6. Stable identity (KTD-2) — preserve block_id across re-carves
# ---------------------------------------------------------------------------


def assign_block_ids(blocks: list[Block], prior_rows) -> None:
    """Assign each block a stable ``block_id`` IN PLACE (KTD-2).

    A new block reuses a prior row's ``block_id`` when they SUBSTANTIALLY match —
    span overlap >= half the shorter span AND evidence (name-token) similarity /
    dominant-app overlap — so a re-carve on unchanged evidence preserves every id
    and adding a chunk of the same work extends the trailing block under its old
    id. Each prior id is claimed at most once (best match wins); an unmatched
    block mints a fresh token. Matching is heuristic (the plan accepts occasional
    churn on heavily reshaped days; threads self-heal next pass).
    """
    candidates = [
        _PriorBlock(
            block_id=r.block_id,
            start_ts=r.start_ts,
            end_ts=r.end_ts,
            tokens=_name_tokens(r.name),
            apps=_metadata_apps(r.metadata),
        )
        for r in (prior_rows or [])
        if getattr(r, "block_id", None)
    ]
    claimed: set[str] = set()
    # Greedy by descending match score so the strongest overlaps claim first.
    scored: list[tuple[float, int, _PriorBlock]] = []
    for bi, block in enumerate(blocks):
        for prior in candidates:
            score = _identity_score(block, prior)
            if score > 0.0:
                scored.append((score, bi, prior))
    scored.sort(key=lambda t: t[0], reverse=True)
    assigned: dict[int, str] = {}
    for score, bi, prior in scored:
        if bi in assigned or prior.block_id in claimed:
            continue
        assigned[bi] = prior.block_id
        claimed.add(prior.block_id)
    for bi, block in enumerate(blocks):
        block.block_id = assigned.get(bi) or _mint_block_id()


@dataclass
class _PriorBlock:
    block_id: str
    start_ts: float
    end_ts: float
    tokens: frozenset[str]
    apps: tuple[str, ...]


def _identity_score(block: Block, prior: _PriorBlock) -> float:
    overlap = min(block.end_ts, prior.end_ts) - max(block.start_ts, prior.start_ts)
    if overlap <= 0:
        return 0.0
    shorter = min(block.end_ts - block.start_ts, prior.end_ts - prior.start_ts)
    if shorter <= 0:
        return 0.0
    span_ratio = overlap / shorter
    if span_ratio < 0.5:
        return 0.0
    name_sim = _jaccard(block.evidence_tokens(), prior.tokens)
    shares_app = bool(set(block.apps) & set(prior.apps))
    evidence_ok = (
        name_sim > 0.0
        or shares_app
        or (not block.evidence_tokens() and not prior.tokens)  # both unnamed
    )
    if not evidence_ok:
        return 0.0
    return span_ratio + name_sim


def _metadata_apps(metadata: str | None) -> tuple[str, ...]:
    if not metadata:
        return ()
    try:
        import json

        data = json.loads(metadata)
    except (ValueError, TypeError):
        return ()
    apps = data.get("apps_used") if isinstance(data, dict) else None
    if not isinstance(apps, list):
        return ()
    return tuple(str(a) for a in apps if a)


def _mint_block_id() -> str:
    return f"blk_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# 7. is_open (live trailing block) — U1 stored column
# ---------------------------------------------------------------------------


def mark_trailing_open(blocks: list[Block], *, is_live: bool) -> None:
    """Set ``is_open`` on the still-growing trailing block IN PLACE (U1).

    On a LIVE pass the single block with the greatest ``end_ts`` is the one still
    growing -> ``is_open=1``; every other block (and every block at finalize) is
    closed. Stored, never derived at read time.
    """
    for b in blocks:
        b.is_open = False
    if not is_live or not blocks:
        return
    trailing = max(range(len(blocks)), key=lambda i: blocks[i].end_ts)
    blocks[trailing].is_open = True


# ---------------------------------------------------------------------------
# Orchestrator — the ONE shared body (live + finalize parity)
# ---------------------------------------------------------------------------


def consolidate(
    fine_tasks,
    *,
    prior_rows=None,
    protected_spans=None,
    tz_offset_s: int = 0,
    is_live: bool = False,
    recording_name: str = "",
    namer=None,
    bullet_provider=None,
    stop_event=None,
    deadline=None,
) -> list[dict]:
    """Consolidate fine fragments into named diary blocks -> enriched task dicts.

    The single body the live incremental tick and the finalize pass share (R10),
    so blocks cannot drift between them. Pure except for the injected ``namer``
    and ``bullet_provider`` seams (the small per-merged-block naming call and the
    per-block on-device bullet call). Returns a list of task dicts (``task_index``
    0..N-1, ``source='agent'`` provenance kept per block) carrying ``block_id`` /
    ``thread_id`` / ``is_open``, sanitized ``bullets`` (U3), and — when naming or
    bullet generation degraded — a ``name_fallback`` / ``bullets_fallback`` marker
    (KTD-10) — the shape the terminal stage persists via the scoped
    ``replace_task_segments`` + ``tasks.json`` mirror.
    """
    blocks = group_fragments(fine_tasks)
    blocks = clamp_blocks_at_midnight(blocks, tz_offset_s)
    name_blocks(blocks, namer=namer, stop_event=stop_event, deadline=deadline)
    link_threads(blocks, recording_name=recording_name, tz_offset_s=tz_offset_s)
    blocks = trim_blocks_to_protected(blocks, list(protected_spans or []))
    # Bullets are generated over the FINAL (trimmed) blocks so split/trimmed
    # pieces each carry their own evidence-bound bullets (U3, KTD-4).
    bullets_for_blocks(
        blocks, bullet_provider=bullet_provider,
        stop_event=stop_event, deadline=deadline,
    )
    assign_block_ids(blocks, prior_rows)
    mark_trailing_open(blocks, is_live=is_live)
    return [_to_task_dict(b, i) for i, b in enumerate(blocks)]


def _to_task_dict(block: Block, index: int) -> dict:
    from screencap.segmentation.validate import _slugify

    out: dict = {
        "start_ts": float(block.start_ts),
        "end_ts": float(block.end_ts),
        "name": block.name,
        "category": block.category,
        "source": SOURCE_MECHANICAL if block.name_fallback == "mechanical"
        else SOURCE_MODEL,
        "block_id": block.block_id,
    }
    if block.name:
        out["derived_name"] = block.metadata.get("derived_name") or _slugify(block.name)
    if block.confidence is not None:
        out["confidence"] = block.confidence
    # apps_used rides on EVERY block (from the aggregated fragment apps), so the
    # next re-carve's identity match has an app signal even for an unnamed block.
    apps = list(block.apps)
    if apps:
        out["apps_used"] = apps
    if "description" in block.metadata:
        out["description"] = block.metadata["description"]
    if block.thread_id is not None:
        out["thread_id"] = block.thread_id
    if block.is_open:
        out["is_open"] = True
    if block.name_fallback is not None:
        out[NAME_FALLBACK_KEY] = block.name_fallback
    # Topic bullets (U3): ride the ``metadata`` blob (the terminal stage folds
    # them in), under U1's field-scoped ``EDITED_FIELD_BULLETS`` protection. The
    # fallback marker records the honest app-level degrade (KTD-10).
    if block.bullets:
        out[BULLETS_KEY] = list(block.bullets)
    if block.bullets_fallback is not None:
        out[BULLETS_FALLBACK_KEY] = block.bullets_fallback
    return out


# ---------------------------------------------------------------------------
# Cadence — the consolidation-specific staleness fingerprint (KTD-6)
# ---------------------------------------------------------------------------


def consolidation_fingerprint(recording_dir) -> tuple | None:
    """A cheap change-key for the recording's consolidation inputs (KTD-6).

    Distinct from the per-window-naming ``_segmentation_fingerprint``: consolidation
    re-runs when the completed-manifest state OR the kept-curation state changes,
    and skips an unchanged tick. Combines the completed-manifest set (count,
    highest chunk index, max manifest mtime — statted, never re-read) with the
    kept (user/edited) task-row count, plus the dir name so a day roll never
    aliases a prior day's key. Returns ``None`` (fail-open: run the pass) when
    there is no completed manifest yet or anything is unreadable, so a skip only
    happens on positive evidence of an unchanged, non-empty state.
    """
    recording_dir = Path(recording_dir)
    try:
        manifests = list(recording_dir.glob("chunk_*_manifest.json"))
    except OSError:
        return None
    if not manifests:
        return None
    try:
        count = len(manifests)
        highest_index = max(int(m.name.split("_")[1]) for m in manifests)
        max_mtime = max(m.stat().st_mtime_ns for m in manifests)
        kept = _kept_curation_count(recording_dir)
    except (OSError, ValueError, IndexError):
        return None
    return (recording_dir.name, count, highest_index, max_mtime, kept)


def _kept_curation_count(recording_dir: Path) -> int:
    from screencap.pipeline_state import PipelineLedger, task_row_is_protected

    db_path = recording_dir / "recording.db"
    if not db_path.exists():
        return 0
    rows = PipelineLedger(db_path).read_task_segments()
    return sum(1 for r in rows if task_row_is_protected(r))
