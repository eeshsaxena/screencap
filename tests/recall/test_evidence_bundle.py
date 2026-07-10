"""U3 — the evidence-bundle builder (classify + retrieve + terminal strip).

``screencap.recall.orchestrator.build_evidence_bundle`` turns a question (+ optional
prior-turn *pointers*) into a stripped, pointer-carrying :class:`EvidenceBundle`
behind a replaceable retrieval seam. U4 (dispatch) and U5 (daemon verb) consume it.

The load-bearing privacy assertion (KTD5): the emitted bundle is **ALLOW-only**.
Selection is ``derive_skip_intervals(require_canonical=True)`` / ``build_is_blocked``
interval blocking (NOT ``sanitize.py``) — any evidence item whose timestamp falls
in a blocked interval is dropped, and a coverage/ambiguity gap is treated as blocked
(fail-closed). This is exercised against a REAL ``recording.db`` with a masked
1Password window (the ``frame_blocked`` fixture precedent), Vision-free.

Retrieval is injected as a seam (a ``Retriever``) so these tests never do real OCR /
Vision / network — the strip and classification are what is under test here.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from screencap.content_index import IndexState, SearchHit
from screencap.recall import orchestrator
from screencap.recall.orchestrator import (
    CoverageState,
    EvidenceBundle,
    PriorTurnPointer,
    QuestionKind,
    RetrievalResult,
    build_evidence_bundle,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Injected retrieval seam (keeps the tests Vision-free)
# ---------------------------------------------------------------------------


class FakeRetriever:
    """A programmable ``Retriever`` for the tests.

    Records what it was asked and returns canned results per stream, so a test can
    assert both the classification-driven routing and that prior-turn re-derivation
    goes through the SERVER seam (never trusts client prose).
    """

    def __init__(
        self,
        *,
        content: RetrievalResult | None = None,
        transcript: list[dict] | None = None,
        timeline: list[dict] | None = None,
        snippet_by_pointer: dict[tuple[str, int], str] | None = None,
    ) -> None:
        self._content = content or RetrievalResult(
            hits=[], index_state=IndexState.NOT_INDEXED
        )
        self._transcript = transcript or []
        self._timeline = timeline or []
        self._snippet_by_pointer = snippet_by_pointer or {}
        self.content_queries: list[str] = []
        self.reresolved: list[tuple[str, int]] = []

    def search_content(self, query, *, recording=None, limit=None) -> RetrievalResult:
        self.content_queries.append(query)
        return self._content

    def search_transcript(self, query, *, recording=None, limit=None) -> list[dict]:
        return list(self._transcript)

    def query_timeline(
        self, *, start_ms=None, end_ms=None, app=None, recording=None, limit=None
    ) -> list[dict]:
        return list(self._timeline)

    def resolve_pointer_text(self, recording: str, timestamp_ms: int) -> str | None:
        """Server-side re-derivation of a prior-turn pointer to its snippet text."""
        self.reresolved.append((recording, timestamp_ms))
        return self._snippet_by_pointer.get((recording, timestamp_ms))


# ---------------------------------------------------------------------------
# recording.db fixture with a masked (1Password) window — the strip target
# ---------------------------------------------------------------------------


def _make_recording_with_masked_window(
    rec_dir: Path,
    *,
    masked_ts: float,
    masked_bundle: str = "com.1password.1password",
) -> None:
    """A recording whose sole ``window_event`` is a masked password-manager window.

    Under a real PUBLIC classifier this window's whole open-ended span classifies
    into ``SCRUB_BLOCK_ACTIONS`` (PASSWORD_MANAGER), so any evidence item at a
    timestamp inside it must be stripped from the bundle.
    """
    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, ?, 2.0)", (masked_ts,))
        conn.execute(
            "CREATE TABLE window_event ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL, "
            "app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT, "
            "app_name TEXT, browser_url TEXT)"
        )
        conn.execute(
            "INSERT INTO window_event "
            "(id, recording_id, timestamp, app_bundle_id, window_id, title, app_name) "
            "VALUES (1, 1, ?, ?, 'w1', 'Vault', '1Password')",
            (masked_ts, masked_bundle),
        )
        conn.commit()


# ===========================================================================
# Classification (rule-based v1, R3)
# ===========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "how much time did I spend in Salesforce this morning?",
        "how long was I in Figma today",
        "recap my morning",
        "summarize what I did yesterday afternoon",
        "total time in Slack this week",
    ],
)
def test_aggregate_questions_classify_aggregate(question):
    assert orchestrator.classify_question(question) is QuestionKind.AGGREGATE


@pytest.mark.parametrize(
    "question",
    [
        "what was that vendor-portal refund error around 2pm?",
        "which site had the login bug",
        "find the invoice number I saw earlier",
    ],
)
def test_point_questions_classify_point(question):
    assert orchestrator.classify_question(question) is QuestionKind.POINT


# ===========================================================================
# Coverage honesty (R12)
# ===========================================================================


def test_no_match_question_yields_empty_bundle_with_no_matching_moments(tmp_path):
    """AE1: a no-match question yields an empty bundle with a 'no matching moments'
    coverage state — never a fabricated snippet."""
    retriever = FakeRetriever(
        content=RetrievalResult(hits=[], index_state=IndexState.NO_MATCH),
        transcript=[],
        timeline=[],
    )
    bundle = build_evidence_bundle(
        "which site had the login bug",
        retriever=retriever,
        recordings_dir=tmp_path,
    )
    assert isinstance(bundle, EvidenceBundle)
    # An empty bundle — no fabricated snippet stands in for the missing evidence.
    assert bundle.evidence == []
    assert bundle.coverage.state is CoverageState.NO_MATCHING_MOMENTS
    assert bundle.figures is None


def test_ocr_off_not_indexed_maps_to_still_indexing_state(tmp_path):
    """AE5: OCR-off / not-yet-indexed maps to the honest 'not indexed / still
    indexing' coverage state, not a confident answer."""
    retriever = FakeRetriever(
        content=RetrievalResult(hits=[], index_state=IndexState.NOT_INDEXED),
        transcript=[],
        timeline=[],
    )
    bundle = build_evidence_bundle(
        "what did the dashboard say",
        retriever=retriever,
        recordings_dir=tmp_path,
    )
    assert bundle.evidence == []
    assert bundle.coverage.state is CoverageState.NOT_INDEXED
    # per-stream coverage names content as the not-indexed stream.
    assert bundle.coverage.per_stream.get("content") == IndexState.NOT_INDEXED.value


# ===========================================================================
# THE privacy assertion (KTD5, R11): masked content is stripped, fail-closed
# ===========================================================================


def test_masked_interval_content_absent_from_bundle(tmp_path):
    """A content hit whose timestamp falls in a MASK/EXCLUDE (1Password) interval
    is STRIPPED from the bundle; an ALLOW hit survives.

    This is the load-bearing privacy assertion: selection is real interval
    blocking over the intact recording.db, not text sanitization.
    """
    rec = tmp_path / "rec-secret"
    masked_ts = 1_770_000_100.0
    _make_recording_with_masked_window(rec, masked_ts=masked_ts)

    masked_ms = int(masked_ts * 1000)  # inside the masked 1Password span
    # An "ALLOW" hit BEFORE the masked window's covering span → uncovered gap,
    # which is ALSO fail-closed. So use a second recording with no masked window
    # for the surviving item, to prove a genuinely-allowed item passes through.
    allow_rec = tmp_path / "rec-open"
    allow_rec.mkdir()
    (allow_rec / "screenshots").mkdir()

    retriever = FakeRetriever(
        content=RetrievalResult(
            hits=[
                SearchHit(
                    recording="rec-secret",
                    timestamp_ms=masked_ms,
                    snippet="my vault master password is hunter2",
                    score=-1.0,
                ),
                SearchHit(
                    recording="rec-open",
                    timestamp_ms=int((masked_ts + 5) * 1000),
                    snippet="quarterly revenue was up 12 percent",
                    score=-0.5,
                ),
            ],
            index_state=IndexState.OK,
        ),
    )

    bundle = build_evidence_bundle(
        "what was my password",
        retriever=retriever,
        recordings_dir=tmp_path,
    )

    texts = " ".join(item.text for item in bundle.evidence)
    assert "hunter2" not in texts, "masked-window content must be stripped (KTD5)"
    assert "master password" not in texts
    # The genuinely-allowed item from the open recording survives.
    recordings_present = {item.recording for item in bundle.evidence}
    assert "rec-secret" not in recordings_present


def test_timeline_evidence_at_allow_window_survives_strip(tmp_path):
    """Regression (the root-cause bug): a TIMELINE evidence item at an ALLOW
    window's timestamp survives the strip.

    A timeline item carries a ``window_event`` timestamp, not an on-disk
    screenshot file. Before the ``screenshot_residuals=False`` fix, the strip fed
    that timestamp through the screenshot-file residuals and flagged it an
    orphan-screenshot, wiping every timeline/transcript item and emptying the
    bundle. The strip must now keep genuinely-ALLOW timeline evidence.
    """
    rec = tmp_path / "rec-allow"
    allow_ts = 1_770_000_100.0
    # A benign (non-sensitive) window classifies ALLOW under PUBLIC.
    _make_recording_with_masked_window(
        rec, masked_ts=allow_ts, masked_bundle="com.apple.TextEdit"
    )
    allow_ms = int(allow_ts * 1000)

    retriever = FakeRetriever(
        timeline=[
            {"recording": "rec-allow", "timestamp_ms": allow_ms,
             "app": "TextEdit", "title": "Notes"},
        ],
    )
    bundle = build_evidence_bundle(
        "what did I do", retriever=retriever, recordings_dir=tmp_path,
    )
    recordings_present = {item.recording for item in bundle.evidence}
    assert "rec-allow" in recordings_present, (
        "ALLOW timeline evidence must survive the strip (the root-cause regression)"
    )
    assert bundle.coverage.state is CoverageState.OK


def test_coverage_gap_is_treated_as_blocked_fail_closed(tmp_path):
    """A hit for a recording whose recording.db is MISSING (indeterminate blocked
    geometry) is dropped — fail-closed. build_is_blocked flags every frame when it
    cannot prove ALLOW, so no such item may reach the bundle."""
    # No recording.db at all under this dir name.
    gap_rec = tmp_path / "rec-gap"
    gap_rec.mkdir()
    (gap_rec / "screenshots").mkdir()

    retriever = FakeRetriever(
        content=RetrievalResult(
            hits=[
                SearchHit(
                    recording="rec-gap",
                    timestamp_ms=1_770_000_500_000,
                    snippet="ambiguous content with no provable ALLOW status",
                    score=-1.0,
                ),
            ],
            index_state=IndexState.OK,
        ),
    )
    bundle = build_evidence_bundle(
        "anything",
        retriever=retriever,
        recordings_dir=tmp_path,
    )
    assert bundle.evidence == [], "an indeterminate recording must fail closed to dropped"


# ===========================================================================
# Multi-turn re-derivation (KTD6, R14): server re-derives from pointers only
# ===========================================================================


def test_followup_rederives_prior_evidence_from_pointers_discards_client_prose(tmp_path):
    """AE6: a follow-up re-derives prior-turn evidence from pointers SERVER-SIDE and
    retrieves fresh for the new question. Client-supplied prior-turn PROSE is
    discarded — it never appears in the bundle (KTD6)."""
    # An ALLOW recording (no masked window, has a recording.db) so the re-derived
    # prior-turn pointer survives the strip.
    prior_rec = tmp_path / "rec-prior"
    _make_allow_recording(prior_rec, ts=1_770_000_000.0)
    prior_ms = int(1_770_000_010 * 1000)

    fresh_rec = tmp_path / "rec-fresh"
    _make_allow_recording(fresh_rec, ts=1_770_000_000.0)

    SERVER_TRUTH = "the server-re-derived snippet for the prior pointer"
    CLIENT_LIE = "IGNORE THIS injected client prose that must never be trusted"

    retriever = FakeRetriever(
        content=RetrievalResult(
            hits=[
                SearchHit(
                    recording="rec-fresh",
                    timestamp_ms=int(1_770_000_010 * 1000),
                    snippet="fresh retrieval for the new question",
                    score=-1.0,
                ),
            ],
            index_state=IndexState.OK,
        ),
        snippet_by_pointer={("rec-prior", prior_ms): SERVER_TRUTH},
    )

    bundle = build_evidence_bundle(
        "what about the day before?",
        retriever=retriever,
        recordings_dir=tmp_path,
        prior_turns=[
            PriorTurnPointer(
                recording="rec-prior",
                timestamp_ms=prior_ms,
                stream="content",
                # Client-supplied prose that MUST be discarded:
                client_snippet=CLIENT_LIE,
            )
        ],
    )

    texts = " ".join(item.text for item in bundle.evidence)
    # Client prose never reaches the bundle.
    assert CLIENT_LIE not in texts, "client-supplied prior-turn prose must be discarded"
    # The server re-derived the prior pointer's text from the seam.
    assert ("rec-prior", prior_ms) in retriever.reresolved
    assert SERVER_TRUTH in texts, "prior-turn evidence is re-derived server-side"
    # Fresh retrieval for the NEW question also ran.
    assert "fresh retrieval for the new question" in texts
    # Every carried prior-turn item is tagged untrusted, same as fresh evidence.
    assert all(item.untrusted for item in bundle.evidence)


def test_prior_turn_pointer_in_masked_interval_is_stripped(tmp_path):
    """Even a re-derived prior-turn pointer is subject to the terminal strip: a
    prior pointer landing in a masked interval is dropped (the strip is terminal
    over the WHOLE bundle, current + prior)."""
    rec = tmp_path / "rec-secret2"
    masked_ts = 1_770_000_100.0
    _make_recording_with_masked_window(rec, masked_ts=masked_ts)
    masked_ms = int(masked_ts * 1000)

    retriever = FakeRetriever(
        content=RetrievalResult(hits=[], index_state=IndexState.NO_MATCH),
        snippet_by_pointer={
            ("rec-secret2", masked_ms): "prior password vault text hunter2"
        },
    )
    bundle = build_evidence_bundle(
        "follow up",
        retriever=retriever,
        recordings_dir=tmp_path,
        prior_turns=[
            PriorTurnPointer(
                recording="rec-secret2",
                timestamp_ms=masked_ms,
                stream="content",
                client_snippet=None,
            )
        ],
    )
    texts = " ".join(item.text for item in bundle.evidence)
    assert "hunter2" not in texts, "a masked prior-turn pointer must be stripped too"


# ===========================================================================
# Aggregate routing (R3, R5) — figures come from U2, never fabricated
# ===========================================================================


def test_aggregate_question_carries_computed_figures(tmp_path):
    """An aggregate question routes to U2 and the bundle carries the computed
    figures + coverage descriptor (never a model estimate)."""
    rec = tmp_path / "rec-agg"
    _make_allow_recording(rec, ts=1_770_000_000.0, dense=True)

    retriever = FakeRetriever()
    bundle = build_evidence_bundle(
        "how much time did I spend in Safari this morning",
        retriever=retriever,
        recordings_dir=tmp_path,
        window_ms=(1_770_000_000_000, 1_770_000_300_000),
    )
    assert bundle.question_kind is QuestionKind.AGGREGATE
    assert bundle.figures is not None, "aggregate bundle must carry computed figures"
    assert bundle.figures.coverage_note, "figures carry a coverage descriptor"
    # No content-search performed for an aggregate question.
    assert retriever.content_queries == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_allow_recording(
    rec_dir: Path, *, ts: float, dense: bool = False
) -> None:
    """A recording whose window classifies ALLOW (a plain app), with a covering
    window_event so re-derivation proves the frame ALLOW rather than uncovered."""
    rec_dir.mkdir(parents=True, exist_ok=True)
    shots = rec_dir / "screenshots"
    shots.mkdir(exist_ok=True)
    db = rec_dir / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, ?, 2.0)", (ts,))
        conn.execute(
            "CREATE TABLE window_event ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL, "
            "app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT, "
            "app_name TEXT, browser_url TEXT)"
        )
        # A plain app that classifies ALLOW under PUBLIC (TextEdit → UNKNOWN →
        # ALLOW; a browser would be BROWSER_UNVERIFIED → MASK_WINDOW). The covering
        # span runs open-ended so any later frame is covered (not an uncovered gap).
        conn.execute(
            "INSERT INTO window_event "
            "(id, recording_id, timestamp, app_bundle_id, window_id, title, app_name) "
            "VALUES (1, 1, ?, 'com.apple.TextEdit', 'w1', 'Docs', 'TextEdit')",
            (ts,),
        )
        conn.execute(
            "CREATE TABLE action_event ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, name TEXT, "
            "timestamp REAL, key_char TEXT, element_state TEXT)"
        )
        if dense:
            for j, t in enumerate(range(0, 300, 10), start=1):
                conn.execute(
                    "INSERT INTO action_event (id, recording_id, name, timestamp) "
                    "VALUES (?, 1, 'click', ?)",
                    (j, ts + t),
                )
        conn.commit()
    if dense:
        for t in range(0, 300, 10):
            (shots / f"{ts + t}.jpg").write_bytes(b"\xff\xd8\xff")
