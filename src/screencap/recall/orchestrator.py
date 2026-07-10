"""Evidence-bundle builder (U3): classify → retrieve → terminal strip.

Turns a chat question (+ optional prior-turn *pointers*) into a stripped,
pointer-carrying :class:`EvidenceBundle` behind a replaceable retrieval seam. U4
(dispatch) resolves consent + generates from this bundle; U5 (the
``/v0/chat.answer`` daemon verb) and U6 (the MCP tool) return it.

The pipeline (mirrors the plan's chat-turn flow):

1. **Classify** point-vs-aggregate (rule-based v1 — R3).
2. **Retrieve** behind an injectable :class:`Retriever` seam — point questions hit
   the content/transcript/timeline primitives; aggregate questions hit U2
   (:func:`screencap.segmentation.aggregate.aggregate_window`). The seam is the
   place the deferred agentic multi-hop loop replaces retrieval later.
3. **Re-derive prior-turn evidence SERVER-SIDE from pointers** (KTD6) — never trust
   client-supplied prior-turn prose. The client sends ``(recording, timestamp_ms,
   stream)`` pointers; this module re-fetches the snippet text from the index and
   **discards** any ``client_snippet``.
4. **Terminal fail-closed strip** (KTD5, the load-bearing privacy mechanism) — the
   emitted bundle is ALLOW-only. Selection is interval blocking via
   :func:`screencap.frame_blocked.build_is_blocked` (which reuses
   ``derive_skip_intervals(require_canonical=True)`` over the intact local
   ``recording.db``, fail-closed on read errors/ambiguity). Any evidence item whose
   timestamp falls in a blocked interval is dropped; a coverage/ambiguity gap is
   treated as blocked. This runs over the WHOLE bundle (fresh + re-derived prior).
5. **Optional per-snippet text redaction** (defense-in-depth) — surviving snippet
   text may additionally pass through the redaction engine seam. Content-index text
   is already post-redaction/ALLOW-only at ingest, so the strip primarily guards
   transcript/timeline/re-derived content; text redaction is injected and OFF by
   default so this stays Vision/NLP-free.

**Architecture constraint:** this module MUST NOT import ``screencap.daemon.app``
— U5 makes the daemon import *this*, so the reverse edge is a circular import. The
:class:`DefaultRetriever` reaches the daemon's underlying primitives directly
(``content_index.ContentIndex`` + light ``recording_db`` readers), never the
handlers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence

from screencap.content_index import IndexState, SearchHit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed vocabulary
# ---------------------------------------------------------------------------


class QuestionKind(str, Enum):
    """A chat question is a point lookup or a period summary (R3)."""

    POINT = "point"
    AGGREGATE = "aggregate"


class Stream(str, Enum):
    """Which retrieval stream an evidence item came from.

    ``timeline`` is authoritative (event tables, no OCR loss); ``content`` and
    ``transcript`` are best-effort (R12).
    """

    CONTENT = "content"
    TRANSCRIPT = "transcript"
    TIMELINE = "timeline"


class CoverageState(str, Enum):
    """Honest coverage states the chat distinguishes (R12), never a confident
    answer over thin evidence.

    Maps from the content index's :class:`~screencap.content_index.IndexState`
    plus per-stream results:

    * ``OK`` — matching evidence was found.
    * ``NO_MATCHING_MOMENTS`` — retrieval ran but nothing matched.
    * ``NOT_INDEXED`` — the content index has no store yet (OCR indexing off /
      not-yet-run); the honest "still indexing / not yet indexed" state (R13).
    * ``INDEX_DEGRADED`` — the FTS5 index is absent (LIKE fallback); results are
      partial.
    * ``STORE_UNAVAILABLE`` — the store is missing/corrupt/symlinked.
    """

    OK = "ok"
    NO_MATCHING_MOMENTS = "no_matching_moments"
    NOT_INDEXED = "not_indexed"
    INDEX_DEGRADED = "index_degraded"
    STORE_UNAVAILABLE = "store_unavailable"


@dataclass(frozen=True)
class EvidencePointer:
    """A pointer-only locator for an evidence item — no bytes, no path.

    ``(recording, timestamp_ms)`` is exactly what ``frame.nearest`` resolves to a
    frame stem, so the sources panel can deep-link the moment. ``stream`` records
    which retrieval stream produced it (for per-stream coverage / UI grouping).
    """

    recording: str
    timestamp_ms: int
    stream: Stream


@dataclass(frozen=True)
class EvidenceItem:
    """One retrieved snippet, tagged **untrusted** so U4 delimits it from the
    guardrail prompt (KTD3 — retrieved on-screen text is a prompt-injection
    surface).

    ``text`` is the post-strip, post-optional-redaction snippet; ``pointer`` locates
    it. ``untrusted`` is always True — the flag exists so downstream code cannot
    forget that evidence text is DATA, never instructions.
    """

    text: str
    pointer: EvidencePointer
    untrusted: bool = True

    @property
    def recording(self) -> str:
        return self.pointer.recording

    @property
    def timestamp_ms(self) -> int:
        return self.pointer.timestamp_ms

    @property
    def stream(self) -> Stream:
        return self.pointer.stream


@dataclass(frozen=True)
class CoverageDescriptor:
    """Honest coverage for the bundle (R12): an overall state, a short note for
    narration, and a per-stream map (``{stream_name: index_state_value}``) so the
    UI can say "content is still indexing; timeline is authoritative"."""

    state: CoverageState
    note: str
    per_stream: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceBundle:
    """The stripped, pointer-carrying evidence bundle U4/U5/U6 consume.

    * ``evidence`` — ALLOW-only, untrusted-tagged snippets + pointers (point flow).
    * ``figures`` — the computed :class:`~screencap.segmentation.aggregate.WindowAggregate`
      for an aggregate question (R5), else ``None``.
    * ``coverage`` — the honest coverage descriptor (R12).
    * ``question_kind`` — how the question was classified.

    The bundle is the *entire* evidence bound: U4 sends the cloud provider exactly
    this (snippets + figures), never frames, never un-retrieved history (R8/R10).
    """

    question_kind: QuestionKind
    evidence: list[EvidenceItem]
    coverage: CoverageDescriptor
    figures: object | None = None  # WindowAggregate | None (kept loose to avoid a hard import)


@dataclass(frozen=True)
class PriorTurnPointer:
    """A prior-turn source pointer the client carries forward (KTD6, R14).

    The client sends the pointer (``recording``, ``timestamp_ms``, ``stream``); the
    orchestrator re-derives the snippet text SERVER-SIDE and **discards**
    ``client_snippet``. Client prose is never trusted or carried into the bundle.
    """

    recording: str
    timestamp_ms: int
    stream: str = Stream.CONTENT.value
    client_snippet: str | None = None  # DISCARDED — server re-derives from the pointer


# ---------------------------------------------------------------------------
# Retrieval seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalResult:
    """A content-index retrieval result: hits + the index state.

    Mirrors :class:`screencap.content_index.SearchResult` but is the seam's own
    type so a caller (the daemon) can inject a retriever without importing the
    content-index internals. ``index_state`` drives the coverage descriptor.
    """

    hits: list[SearchHit] = field(default_factory=list)
    index_state: IndexState = IndexState.NO_MATCH


class Retriever(Protocol):
    """The replaceable retrieval seam (KTD3).

    Point questions hit ``search_content`` / ``search_transcript`` /
    ``query_timeline``; aggregate questions hit U2 directly (not this seam).
    ``resolve_pointer_text`` re-derives a prior-turn pointer's snippet text
    server-side (KTD6). The deferred agentic multi-hop loop swaps this out.
    """

    def search_content(
        self, query: str, *, recording: str | None = None, limit: int | None = None
    ) -> RetrievalResult: ...

    def search_transcript(
        self, query: str, *, recording: str | None = None, limit: int | None = None
    ) -> list[dict]: ...

    def query_timeline(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        app: str | None = None,
        recording: str | None = None,
        limit: int | None = None,
    ) -> list[dict]: ...

    def resolve_pointer_text(
        self, recording: str, timestamp_ms: int
    ) -> str | None: ...


class DefaultRetriever:
    """Default :class:`Retriever` reaching the daemon's underlying primitives
    directly — NO ``screencap.daemon.app`` import (avoids the U5 circular edge).

    * content → :class:`screencap.content_index.ContentIndex` (the same store the
      daemon's ``_run_content_search`` opens).
    * prior-turn re-derivation → a by-timestamp read of that same store.
    * transcript / timeline → light local readers (mirroring the daemon's SQL) so
      the seam is self-contained; the daemon may inject its own retriever wrapping
      its ``_run_*`` helpers if it prefers.
    """

    def __init__(self, recordings_dir: Path | None = None) -> None:
        self._recordings_dir = Path(recordings_dir) if recordings_dir is not None else None

    # -- content ---------------------------------------------------------

    def search_content(
        self, query: str, *, recording: str | None = None, limit: int | None = None
    ) -> RetrievalResult:
        from screencap.content_index import ContentIndex, default_index_path

        path = default_index_path()
        # A read must never spawn an empty PII store — surface not_indexed.
        if not path.exists():
            return RetrievalResult([], IndexState.NOT_INDEXED)
        kwargs: dict = {}
        if limit is not None:
            kwargs["limit"] = limit
        with ContentIndex(path) as store:
            result = store.search(query, recording=recording, **kwargs)
        return RetrievalResult(list(result.hits), result.index_state)

    def resolve_pointer_text(
        self, recording: str, timestamp_ms: int
    ) -> str | None:
        """Re-fetch the stored on-screen text for an exact ``(recording,
        timestamp_ms)`` frame from the content index (KTD6). Pointer → text
        server-side; never trusts client prose. Returns ``None`` when the store is
        absent or the frame has no row."""
        from screencap.content_index import ContentIndex, default_index_path

        path = default_index_path()
        if not path.exists():
            return None
        with ContentIndex(path) as store:
            return _read_frame_text(store, recording, timestamp_ms)

    # -- transcript ------------------------------------------------------

    def search_transcript(
        self, query: str, *, recording: str | None = None, limit: int | None = None
    ) -> list[dict]:
        return _run_transcript_scan(
            query, recording, limit or _DEFAULT_LIMIT, self._recordings_dir
        )

    # -- timeline --------------------------------------------------------

    def query_timeline(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        app: str | None = None,
        recording: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        return _run_timeline_scan(
            start_ms, end_ms, app, recording, limit or _DEFAULT_LIMIT,
            self._recordings_dir,
        )


_DEFAULT_LIMIT = 50


def _read_frame_text(store, recording: str, timestamp_ms: int) -> str | None:
    """Read one frame's stored text from a :class:`ContentIndex`.

    The store has no public by-timestamp getter, so we read its connection with a
    parameterized query against whichever content table is present. Bound
    parameters only (no interpolation); the table name comes from the store's own
    ``_table`` allowlist, never caller input.
    """
    conn = getattr(store, "_conn", None)
    if conn is None or not getattr(store, "available", False):
        return None
    table = getattr(store, "_table", None)
    if not table:
        return None
    try:
        row = conn.execute(
            f"SELECT text FROM {table} WHERE recording = ? AND timestamp_ms = ? LIMIT 1",
            (recording, int(timestamp_ms)),
        ).fetchone()
    except Exception:
        logger.debug("prior-turn frame read failed", exc_info=True)
        return None
    if row and row[0]:
        return str(row[0])
    return None


# ---------------------------------------------------------------------------
# Light transcript / timeline readers (mirror the daemon SQL; no daemon import)
# ---------------------------------------------------------------------------


def _iter_recording_dirs(
    recording: str | None, recordings_dir: Path | None
) -> list[Path]:
    """Candidate recording dirs (validated single, or all, capped). Mirrors the
    daemon's ``_iter_recording_dirs`` but takes an explicit root so tests can pass
    a ``tmp_path`` library."""
    base = recordings_dir
    if base is None:
        from screencap.config import get_recordings_dir

        base = get_recordings_dir()
    base = Path(base)
    if recording is not None:
        d = base / recording
        return [d] if d.is_dir() else []
    if not base.is_dir():
        return []
    dirs = [d for d in sorted(base.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    return dirs[:200]


def _run_transcript_scan(
    query: str, recording: str | None, limit: int, recordings_dir: Path | None
) -> list[dict]:
    """Keyword scan over STRICT scrubbed ``transcript_*.txt`` (never the rich
    per-word JSON, never ``*.scrub_failed``) — the same contract the daemon's
    ``_run_transcript_search`` honors, factored here to avoid a daemon import."""
    from screencap.content_index import like_snippet

    needle = (query or "").strip().lower()
    if not needle:
        return []
    hits: list[dict] = []
    for rec_dir in _iter_recording_dirs(recording, recordings_dir):
        candidates = sorted(rec_dir.glob("transcript_*.txt"))
        bare = rec_dir / "transcript.txt"
        if bare.exists():
            candidates.append(bare)
        for path in candidates:
            if path.suffix != ".txt" or path.name.endswith(".scrub_failed"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle in text.lower():
                ts_ms = _chunk_start_ms(rec_dir, path)
                hits.append({
                    "recording": rec_dir.name,
                    "snippet": like_snippet(text, query, width=120, collapse_newlines=True),
                    "timestamp_ms": ts_ms,
                })
                if len(hits) >= limit:
                    return hits
    return hits


def _chunk_start_ms(rec_dir: Path, path: Path) -> int | None:
    """The chunk-start wall-clock (ms) for a per-chunk transcript, so a transcript
    hit is jumpable (SCR-186). The bare whole-recording ``transcript.txt`` spans
    every chunk → no anchor (None). Best-effort: None when the manifest is
    absent/unreadable."""
    import json

    from screencap.frame_resolve import _round_half_away

    if path.name == "transcript.txt":
        return None
    stem = path.name[: -len(".txt")]
    _, _, tail = stem.partition("_")
    try:
        chunk_index = int(tail)
    except ValueError:
        chunk_index = 0
    manifest = rec_dir / f"chunk_{chunk_index:04d}_manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    start = data.get("chunk_start") if isinstance(data, dict) else None
    if not isinstance(start, (int, float)):
        return None
    return _round_half_away(start * 1000.0)


def _run_timeline_scan(
    start_ms: int | None,
    end_ms: int | None,
    app: str | None,
    recording: str | None,
    limit: int,
    recordings_dir: Path | None,
) -> list[dict]:
    """Structured ``{recording, timestamp_ms, app, title}`` rows from
    ``window_event`` — authoritative, no OCR. ``browser_url`` is read internally as
    a hostname-only filter predicate and never selected into a row (R6), mirroring
    the daemon's ``_run_timeline_query`` contract."""
    import heapq

    from screencap.content_index import escape_like
    from screencap.recording_db import has_column, has_table, open_recording_db

    start_s = start_ms / 1000.0 if start_ms is not None else None
    end_s = end_ms / 1000.0 if end_ms is not None else None
    app_token = app.lower() if app else None
    app_like = f"%{escape_like(app_token)}%" if app_token else None

    rows: list[dict] = []
    for rec_dir in _iter_recording_dirs(recording, recordings_dir):
        db_path = rec_dir / "recording.db"
        if not db_path.is_file():
            continue
        try:
            with open_recording_db(db_path, read_only=True) as conn:
                if not has_table(conn, "window_event") or not has_column(
                    conn, "window_event", "timestamp"
                ):
                    continue
                has_name = has_column(conn, "window_event", "app_name")
                has_bundle = has_column(conn, "window_event", "app_bundle_id")
                has_title = has_column(conn, "window_event", "title")
                name_expr = "app_name" if has_name else "NULL"
                bundle_expr = "app_bundle_id" if has_bundle else "NULL"
                title_expr = "title" if has_title else "NULL"

                params: list = []
                time_clause = ""
                if start_s is not None:
                    time_clause += " AND timestamp >= ?"
                    params.append(start_s)
                if end_s is not None:
                    time_clause += " AND timestamp < ?"
                    params.append(end_s)

                sql = (
                    f"SELECT timestamp, {name_expr}, {bundle_expr}, {title_expr} "
                    "FROM window_event WHERE timestamp IS NOT NULL" + time_clause
                )
                if app_like and (has_name or has_bundle):
                    clauses = []
                    if has_name:
                        clauses.append("lower(app_name) LIKE ? ESCAPE '\\'")
                        params.append(app_like)
                    if has_bundle:
                        clauses.append("lower(app_bundle_id) LIKE ? ESCAPE '\\'")
                        params.append(app_like)
                    sql += " AND (" + " OR ".join(clauses) + ")"
                sql += " ORDER BY timestamp LIMIT ?"
                params.append(limit)
                for ts, app_name, bundle, title in conn.execute(sql, params):
                    rows.append({
                        "recording": rec_dir.name,
                        "timestamp_ms": int(float(ts) * 1000),
                        "app": app_name or bundle,
                        "title": title,
                    })
        except Exception:
            logger.debug("timeline scan skipped %s", rec_dir.name, exc_info=True)
            continue
    return heapq.nsmallest(limit, rows, key=lambda r: r["timestamp_ms"])


# ---------------------------------------------------------------------------
# Classification (rule-based v1 — R3)
# ---------------------------------------------------------------------------

# Aggregate-question cue phrases: "how much/how long time", "recap", "summarize",
# "total". A question tripping any of these is a period summary; else a point
# lookup. Word-boundary matched so "recapitalize" doesn't trip "recap".
_AGGREGATE_CUES = (
    r"how (?:much|long|many)",
    r"\btotal\b",
    r"\btime (?:in|on|spent|using)\b",
    r"\bspen[dt] (?:in|on)\b",
    r"\brecap\b",
    r"\bsummar(?:y|ize|ise)\b",
    r"\boverview of\b",
    r"\bbreakdown\b",
)
_AGGREGATE_RE = re.compile("|".join(_AGGREGATE_CUES), re.IGNORECASE)

# Recap-intent cues (KTD4): an OPEN "what did I do / work on" recap is a period
# summary, not a keyword point lookup over the literal words. The discriminator is
# recap intent, NOT the mere presence of a time reference — "what was that error I
# saw yesterday" carries a time reference but is a specific content lookup and must
# stay POINT, so these patterns match only the open-recap shape ("what did/have I
# do/done/work on / been up to", "walk me through my …"), never "what was that …".
_RECAP_CUES = (
    r"\bwhat (?:did|have) (?:i|we) (?:do|done|been doing|work(?:ed|ing)? on|"
    r"get(?: done)?|been up to|been working on)\b",
    r"\bwhat was (?:i|we) (?:doing|working on|up to)\b",
    r"\bwalk me through (?:my|the|what)\b",
)
_RECAP_RE = re.compile("|".join(_RECAP_CUES), re.IGNORECASE)

# KTD4's "no dominant content keyword" gate: a recap-shaped question that names a
# specific SUBJECT ("… about the login bug", "… to fix that error") is a point
# lookup for that moment, not an open day recap. These high-signal qualifiers are
# chosen NOT to collide with the recap verb phrases themselves (e.g. a bare "on"
# would wrongly fire on "what did I work on this week"), so they only demote a
# genuine content-bearing recap.
_CONTENT_QUALIFIER_CUES = (
    r"\babout\b",
    r"\bregarding\b",
    r"\bto (?:fix|solve|debug|resolve|handle|figure out|deal with)\b",
)
_CONTENT_QUALIFIER_RE = re.compile("|".join(_CONTENT_QUALIFIER_CUES), re.IGNORECASE)


def classify_question(question: str) -> QuestionKind:
    """Rule-based v1 point-vs-aggregate classifier (R3, KTD4).

    Explicit aggregate cues ("how much/long time", "recap", "summarize", "total …")
    always → :attr:`QuestionKind.AGGREGATE`. An open recap-intent question ("what
    did I do …", "what did I work on …") → AGGREGATE **only when it carries no
    dominant content keyword** (KTD4): a recap that names a specific subject ("what
    did I do about the login bug yesterday") is a point lookup for that moment, and a
    specific content lookup that merely carries a time reference ("what was that
    error I saw yesterday") is POINT too. A light on-device intent model is the
    deferred alternative (Outstanding Questions).
    """
    q = question or ""
    if _AGGREGATE_RE.search(q):
        return QuestionKind.AGGREGATE
    if _RECAP_RE.search(q) and not _CONTENT_QUALIFIER_RE.search(q):
        return QuestionKind.AGGREGATE
    return QuestionKind.POINT


# ---------------------------------------------------------------------------
# Coverage descriptor
# ---------------------------------------------------------------------------

_INDEX_STATE_TO_COVERAGE = {
    IndexState.OK: CoverageState.OK,
    IndexState.NO_MATCH: CoverageState.NO_MATCHING_MOMENTS,
    IndexState.NOT_INDEXED: CoverageState.NOT_INDEXED,
    IndexState.INDEX_DEGRADED: CoverageState.INDEX_DEGRADED,
    IndexState.STORE_UNAVAILABLE: CoverageState.STORE_UNAVAILABLE,
}

_COVERAGE_NOTE = {
    CoverageState.OK: "matching moments were found",
    CoverageState.NO_MATCHING_MOMENTS: "no matching moments were found for this question",
    CoverageState.NOT_INDEXED: (
        "on-screen-text search is not indexed yet (indexing off or still running); "
        "answer may be incomplete"
    ),
    CoverageState.INDEX_DEGRADED: (
        "the on-screen-text index is degraded (partial results)"
    ),
    CoverageState.STORE_UNAVAILABLE: "the on-screen-text index is unavailable",
}


def _build_coverage(
    *,
    have_evidence: bool,
    content_state: IndexState,
    transcript_hits: int,
    timeline_hits: int,
) -> CoverageDescriptor:
    """Map per-stream results → an honest overall coverage state (R12).

    Timeline is authoritative; content/transcript are best-effort. The overall
    state prefers OK when any stream produced evidence, else it reflects the
    content index's honest reason (not_indexed / store_unavailable / degraded /
    no_match) so "still indexing" is never mistaken for "nothing there".
    """
    per_stream = {
        Stream.CONTENT.value: content_state.value,
        Stream.TRANSCRIPT.value: (
            IndexState.OK.value if transcript_hits else IndexState.NO_MATCH.value
        ),
        Stream.TIMELINE.value: (
            IndexState.OK.value if timeline_hits else IndexState.NO_MATCH.value
        ),
    }
    if have_evidence:
        state = CoverageState.OK
    elif content_state in (IndexState.NOT_INDEXED, IndexState.STORE_UNAVAILABLE,
                           IndexState.INDEX_DEGRADED):
        # No evidence AND the content index couldn't fully serve — surface WHY.
        state = _INDEX_STATE_TO_COVERAGE[content_state]
    else:
        state = CoverageState.NO_MATCHING_MOMENTS
    return CoverageDescriptor(
        state=state, note=_COVERAGE_NOTE[state], per_stream=per_stream
    )


# ---------------------------------------------------------------------------
# Terminal fail-closed strip (KTD5) — the load-bearing privacy mechanism
# ---------------------------------------------------------------------------


def _strip_blocked(
    candidates: list[EvidenceItem],
    recordings_dir: Path | None,
) -> list[EvidenceItem]:
    """Drop every candidate whose ``timestamp_ms`` falls in a blocked interval.

    ALLOW-only selection via :func:`screencap.frame_blocked.build_is_blocked` with
    ``screenshot_residuals=False``: it re-derives the genuine privacy set (canonical
    ``SCRUB_BLOCK_ACTIONS`` + ambiguity + secure-field) over the intact local
    ``recording.db`` with ``require_canonical=True`` (fail-closed: a missing/partial
    DB or indeterminate geometry flags EVERY item). The screenshot-file residuals
    (orphan-screenshot / uncovered-gap) are deliberately OMITTED here — an evidence
    item's ``timestamp_ms`` is a ``window_event`` time (timeline) or chunk-start time
    (transcript), not an on-disk ``screenshots/*.jpg`` file, so those residuals would
    false-positive every item; the genuine intervals fully cover masked/excluded/
    secure-field windows for every stream. This is terminal over the WHOLE bundle —
    fresh AND re-derived prior-turn items — so no masked/excluded content can reach a
    provider (R11, KTD5). ``sanitize.py`` is NOT used here (it only cleans
    model-emitted task text, does no content redaction).
    """
    if not candidates:
        return []
    from screencap.frame_blocked import build_is_blocked

    # Group by recording so one is_blocked predicate is built per recording (each
    # build re-derives that recording's geometry). Timestamp_ms → seconds for the
    # predicate's native domain.
    by_recording: dict[str, list[EvidenceItem]] = {}
    for item in candidates:
        by_recording.setdefault(item.recording, []).append(item)

    surviving: list[EvidenceItem] = []
    for recording, items in by_recording.items():
        rec_dir = _resolve_recording_dir(recording, recordings_dir)
        if rec_dir is None:
            # A recording whose name doesn't resolve to a dir under the root
            # (traversal / gone) is unprovable → fail-closed, drop all its items.
            logger.debug("recall strip: unresolved recording %r → dropped", recording)
            continue
        frame_tss = [item.timestamp_ms / 1000.0 for item in items]
        try:
            # screenshot_residuals=False: evidence timestamps are window-event
            # (timeline) or chunk-start (transcript) times, NOT on-disk screenshot
            # files, so the orphan-screenshot/uncovered-gap residuals are
            # inapplicable and would flag every item. Test membership against the
            # genuine privacy intervals only (canonical + ambiguity + secure-field);
            # require_canonical still fails closed on a missing/partial DB.
            is_blocked = build_is_blocked(rec_dir, frame_tss, screenshot_residuals=False)
        except Exception:
            # build_is_blocked is itself fail-closed and does not raise, but guard
            # anyway: an unexpected error drops the whole recording's items.
            logger.warning(
                "recall strip: build_is_blocked failed for %s; dropping items",
                rec_dir.name, exc_info=True,
            )
            continue
        for item in items:
            if is_blocked(item.timestamp_ms / 1000.0):
                continue
            surviving.append(item)
    return surviving


def _resolve_recording_dir(
    recording: str, recordings_dir: Path | None
) -> Path | None:
    """Resolve a recording NAME to its dir under the recordings root, traversal-safe.

    Returns ``None`` when the name escapes the root or the dir is absent — the
    caller treats that as fail-closed (drop the item)."""
    base = recordings_dir
    if base is None:
        from screencap.config import get_recordings_dir

        base = get_recordings_dir()
    base = Path(base).resolve()
    candidate = (base / recording).resolve()
    if not candidate.is_relative_to(base):
        return None
    if not candidate.is_dir():
        return None
    return candidate


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# A text-redaction callable seam (defense-in-depth). Default None (identity) so
# the orchestrator stays Vision/NLP-free; U4 / production may inject a
# redaction-engine-backed callable.
TextRedactor = Callable[[str], str]


def build_evidence_bundle(
    question: str,
    *,
    retriever: Retriever | None = None,
    recordings_dir: Path | None = None,
    prior_turns: Sequence[PriorTurnPointer] | None = None,
    window_ms: tuple[int, int] | None = None,
    app: str | None = None,
    limit: int | None = None,
    text_redactor: TextRedactor | None = None,
) -> EvidenceBundle:
    """Build the stripped, pointer-carrying evidence bundle for ``question``.

    Args:
        question: the operator's question (or the follow-up).
        retriever: the retrieval seam (KTD3). Defaults to :class:`DefaultRetriever`
            reaching the content/transcript/timeline primitives directly.
        recordings_dir: recordings root (defaults to the configured dir). Passed by
            tests over a ``tmp_path`` library and used by the terminal strip.
        prior_turns: prior-turn source POINTERS (KTD6). Their snippet text is
            re-derived server-side; any ``client_snippet`` is discarded.
        window_ms: ``(start_ms, end_ms)`` for an aggregate question (R5). Callers
            with a time parser supply this; absent, an aggregate question yields no
            figures (honest coverage).
        app: optional app filter for the aggregate window / timeline (R5).
        limit: retrieval limit (per stream).
        text_redactor: optional per-snippet text redaction (defense-in-depth, KTD5).
            Default identity — keeps this Vision/NLP-free.

    Returns a fail-closed :class:`EvidenceBundle`: ALLOW-only evidence, honest
    coverage, computed figures for aggregate questions.
    """
    retriever = retriever or DefaultRetriever(recordings_dir)
    redact = text_redactor or (lambda t: t)
    kind = classify_question(question)

    if kind is QuestionKind.AGGREGATE:
        return _build_aggregate_bundle(
            question, window_ms=window_ms, app=app, recordings_dir=recordings_dir,
        )
    return _build_point_bundle(
        question,
        retriever=retriever,
        recordings_dir=recordings_dir,
        prior_turns=prior_turns or (),
        limit=limit,
        redact=redact,
        window_ms=window_ms,
        app=app,
    )


def _build_aggregate_bundle(
    question: str,
    *,
    window_ms: tuple[int, int] | None,
    app: str | None,
    recordings_dir: Path | None,
) -> EvidenceBundle:
    """Aggregate flow: compute figures from U2, never estimate (R5)."""
    if window_ms is None:
        # No resolved window → we cannot compute a figure honestly. Report an
        # honest empty aggregate rather than fabricating one.
        return EvidenceBundle(
            question_kind=QuestionKind.AGGREGATE,
            evidence=[],
            coverage=CoverageDescriptor(
                state=CoverageState.NO_MATCHING_MOMENTS,
                note="no time window could be resolved for this summary question",
                per_stream={Stream.TIMELINE.value: IndexState.NO_MATCH.value},
            ),
            figures=None,
        )
    from screencap.segmentation.aggregate import aggregate_window

    start_ms, end_ms = window_ms
    figures = aggregate_window(
        start_ms, end_ms, app=app, recordings_dir=recordings_dir,
    )
    have = bool(figures.apps) or figures.covered_active_ms > 0
    coverage = CoverageDescriptor(
        state=CoverageState.OK if have else CoverageState.NO_MATCHING_MOMENTS,
        note=figures.coverage_note,
        per_stream={Stream.TIMELINE.value: IndexState.OK.value},
    )
    return EvidenceBundle(
        question_kind=QuestionKind.AGGREGATE,
        evidence=[],
        coverage=coverage,
        figures=figures,
    )


def _build_point_bundle(
    question: str,
    *,
    retriever: Retriever,
    recordings_dir: Path | None,
    prior_turns: Sequence[PriorTurnPointer],
    limit: int | None,
    redact: TextRedactor,
    window_ms: tuple[int, int] | None = None,
    app: str | None = None,
) -> EvidenceBundle:
    """Point flow: retrieve fresh + re-derive prior-turn pointers, then strip.

    When a time window is resolved (``window_ms``), the timeline query is scoped to
    it (R8) — a time-referenced point question retrieves the moments in that period,
    not the globally-earliest events. ``app`` narrows the timeline the same way.
    """
    # 1. Fresh retrieval for the new question.
    start_ms, end_ms = window_ms if window_ms else (None, None)
    content = retriever.search_content(question, limit=limit)
    transcript = retriever.search_transcript(question, limit=limit)
    timeline = retriever.query_timeline(
        start_ms=start_ms, end_ms=end_ms, app=app, recording=None, limit=limit,
    )

    candidates: list[EvidenceItem] = []
    for hit in content.hits:
        candidates.append(_item(hit.recording, hit.timestamp_ms, hit.snippet, Stream.CONTENT))
    for row in transcript:
        ts = row.get("timestamp_ms")
        if ts is None:
            continue  # a transcript hit with no resolvable anchor is not pointer-jumpable
        candidates.append(_item(row["recording"], int(ts), row.get("snippet", ""), Stream.TRANSCRIPT))
    for row in timeline:
        title = row.get("title") or row.get("app") or ""
        candidates.append(_item(row["recording"], int(row["timestamp_ms"]), title, Stream.TIMELINE))

    # 2. Prior-turn re-derivation SERVER-SIDE from pointers (KTD6). Client prose is
    # DISCARDED — we never read ``client_snippet``.
    for prior in prior_turns:
        text = retriever.resolve_pointer_text(prior.recording, prior.timestamp_ms)
        if not text:
            continue
        stream = _stream_from_value(prior.stream)
        candidates.append(_item(prior.recording, prior.timestamp_ms, text, stream))

    # 3. Terminal fail-closed strip over the WHOLE candidate set (fresh + prior).
    surviving = _strip_blocked(candidates, recordings_dir)

    # 4. Optional defense-in-depth text redaction on the surviving snippets.
    evidence = [
        EvidenceItem(text=redact(item.text), pointer=item.pointer, untrusted=True)
        for item in surviving
    ]

    coverage = _build_coverage(
        have_evidence=bool(evidence),
        content_state=content.index_state,
        transcript_hits=len(transcript),
        timeline_hits=len(timeline),
    )
    return EvidenceBundle(
        question_kind=QuestionKind.POINT,
        evidence=evidence,
        coverage=coverage,
        figures=None,
    )


def _item(recording: str, timestamp_ms: int, text: str, stream: Stream) -> EvidenceItem:
    return EvidenceItem(
        text=text,
        pointer=EvidencePointer(
            recording=recording, timestamp_ms=int(timestamp_ms), stream=stream
        ),
        untrusted=True,
    )


def _stream_from_value(value: str) -> Stream:
    try:
        return Stream(value)
    except ValueError:
        return Stream.CONTENT
