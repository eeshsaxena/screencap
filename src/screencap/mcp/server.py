"""FastMCP stdio server exposing Screencap's retrieval surface to an agent.

Each tool forwards to a daemon ``/v0/*`` verb and re-wraps the response as a typed,
POINTER-ONLY result (text snippets + ``(recording, timestamp)`` pointers and, for
``resolve_frame``, a bare on-disk screenshot stem; never frame pixels). The server
holds no query logic of its own (R1).

* Retrieval / search — ``search_screen_content``, ``search_transcript``,
  ``query_timeline``, ``resolve_frame``, ``read_frame``, ``list_recordings``,
  ``whoami``, ``chat_answer`` (R15: the one tool returning model-generated PROSE,
  output-sanitized before it reaches the agent; its sources stay pointer-only).
* Day-first browse (U13, R18) — ``browse_day``, ``query_tasks``, ``create_clip``,
  backed by the same daemon verbs the UI uses (``timeline.day`` / ``tasks.query`` /
  ``clip.create``, KTD-2). Recording identifiers are opaque plumbing; DAY + TIME is
  the user-facing vocabulary (KTD-1). Range deletion and clip deletion are
  deliberately NOT exposed as tools — the human-only guarantee is a tool-surface
  control (R18).

stdio discipline: with the stdio transport the server owns stdout (the JSON-RPC
stream), so NOTHING may be written to stdout — all logging goes to stderr. Heavy
imports (``mcp``/``httpx``) are deferred behind the CLI command body; the daemon
connection + held liveness subscription are opened lazily on the first tool call
so ``initialize`` returns within the client's startup timeout.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from screencap.mcp._client import AsyncDaemonClient, DaemonError, LivenessSubscription

logger = logging.getLogger("screencap.mcp")

# Tool-layer bound on snippet/row counts (the daemon also clamps; this keeps the
# agent-facing payload small regardless of a future daemon default).
_MAX_TOOL_LIMIT = 100


def _clamp_or_none(limit: int | None) -> int | None:
    # Passes ``None`` through (the caller omitted a limit → let the daemon apply
    # its default), unlike the daemon's ``_clamp_limit`` which substitutes a
    # default for ``None``. Both clamp a provided value into ``[1, max]``.
    if limit is None:
        return None
    return max(1, min(int(limit), _MAX_TOOL_LIMIT))


# -- typed, pointer-only result models --------------------------------------


class ContentHit(BaseModel):
    recording: str
    timestamp_ms: int
    snippet: str
    score: float


# SCR-258 U9 (KTD-20): every read-tool result carries the encrypted-store state
# as DATA (``mounted`` / ``locked`` / ``absent`` / ``error``), parsed from the
# daemon's success envelope — NOT surfaced as an opaque ``DaemonError`` tool
# failure. A locked store returns ``ok`` + empty results + ``store_state=locked``,
# so the agent gets a first-class "the vault is locked" signal instead of an
# error traceback. Absent → default ``mounted`` (plaintext install / old daemon).
_DEFAULT_STORE_STATE = "mounted"


class ContentSearchResult(BaseModel):
    hits: list[ContentHit]
    # ok / no_match / not_indexed / index_degraded / store_unavailable — so the
    # agent never mistakes an empty/degraded index for ground truth.
    index_state: str
    # KTD-20: mounted / locked / absent / error — a locked vault is data, not an error.
    store_state: str = _DEFAULT_STORE_STATE


class DiaryBlockHit(BaseModel):
    """A day-diary work-BLOCK match — POINTER ONLY (day-diary U6). A text snippet +
    the ``(recording, block_id)`` pointer + the block's absolute unix-ms span; never
    a media path, image bytes, or a full bullet dump beyond the snippet."""

    recording: str
    block_id: str
    start_ms: int
    end_ms: int
    snippet: str
    score: float


class DiarySearchResult(BaseModel):
    hits: list[DiaryBlockHit]
    # ok / no_match / not_indexed / index_degraded / store_unavailable — so the
    # agent never mistakes an empty/degraded diary index for ground truth.
    index_state: str
    # KTD-20: mounted / locked / absent / error — a locked vault is data, not an error.
    store_state: str = _DEFAULT_STORE_STATE


class TranscriptHit(BaseModel):
    recording: str
    chunk_index: int
    snippet: str
    # SCR-186: chunk-granular wall-clock anchor (null when no chunk manifest).
    # Resolve a representative chunk frame with
    # resolve_frame(recording, timestamp_ms, staleness_cap_ms=chunk_duration_ms).
    # timestamp_granularity is "chunk": the frame is representative of the chunk,
    # NOT the exact frame of the matched word.
    timestamp_ms: int | None = None
    timestamp_granularity: str | None = None
    chunk_duration_ms: int | None = None


class TranscriptSearchResult(BaseModel):
    hits: list[TranscriptHit]
    coverage: str
    store_state: str = _DEFAULT_STORE_STATE


class FrameNearest(BaseModel):
    """A resolved nearest-frame pointer (SCR-186). POINTER-ONLY — a bare on-disk
    stem, never a path or image bytes; both fields null on a miss.

    ``encrypted`` (search U8) is True when the corpus is stored encrypted — read the
    frame's bytes via ``read_frame`` (the daemon decrypts) instead of the ``.jpg``
    path, which won't exist / won't be readable plaintext."""

    stem: str | None
    delta_ms: int | None
    encrypted: bool = False
    # KTD-20: mounted / locked / absent / error — a locked vault is data, not an error.
    store_state: str = _DEFAULT_STORE_STATE


class FrameBytes(BaseModel):
    """Decrypted still bytes for an ALLOW, scrubbed frame (search U8 / frame.read).

    ``image_base64`` is null when the frame is missing, blocked, or in a not-yet-
    scrubbed chunk (fail-closed refusal)."""

    image_base64: str | None
    content_type: str | None


class TimelineRow(BaseModel):
    recording: str
    timestamp_ms: int
    app: str | None
    title: str | None


class TimelineResult(BaseModel):
    rows: list[TimelineRow]
    coverage: str
    store_state: str = _DEFAULT_STORE_STATE


class RecordingSummary(BaseModel):
    name: str
    date: str
    duration: str
    has_audio: bool
    transcribed: bool
    uploaded: bool
    # SCR-148: cloud account-mismatch observability. owner_uid is the Firebase
    # uid that owns the recording (None for legacy/local); compare it against
    # whoami().uid to tell a permanent wrong-account block from a transient
    # upload failure. upload_warning is best-effort deferred-upload text.
    owner_uid: str | None = None
    upload_warning: str | None = None


class RecordingsResult(BaseModel):
    recordings: list[RecordingSummary]
    store_state: str = _DEFAULT_STORE_STATE


class WhoAmIResult(BaseModel):
    """The cloud account currently signed in on the daemon (SCR-148)."""

    signed_in: bool
    uid: str | None = None
    email: str | None = None
    # True when signed in but the token could not be refreshed (offline), so
    # uid/email are unknown.
    stale: bool = False


class ChatSource(BaseModel):
    """A moment the answer drew from — POINTER-ONLY (R15).

    Structurally incapable of carrying a media path or image bytes:
    ``(recording, timestamp_ms)`` is exactly what ``resolve_frame`` resolves to a
    frame stem, so the agent can deep-link the moment itself. ``stream`` records
    which retrieval stream produced it (content / transcript / timeline).
    """

    recording: str
    timestamp_ms: int
    stream: str


class ChatCoverage(BaseModel):
    """The honest coverage descriptor for a chat answer (R12).

    ``state`` is ``ok`` / ``no_matching_moments`` / ``not_indexed`` /
    ``index_degraded`` / ``store_unavailable`` so the agent never mistakes an
    empty/degraded index for ground truth; ``per_stream`` maps each stream to its
    index-state value.
    """

    state: str
    note: str
    per_stream: dict[str, str]


class ChatAnswerResult(BaseModel):
    """A grounded conversational-recall answer + its sources — POINTER-ONLY (R15).

    ``answer`` is model-generated prose (OUTPUT-SANITIZED before it reaches the
    agent — see :func:`chat_answer`); ``sources`` are pointer-only locators (never
    image bytes / paths). ``refusal`` flags a no-evidence turn; ``question_kind``
    is ``point`` / ``aggregate``. ``target`` is the execution target the turn
    resolved to (``on_device`` / ``cloud`` / ``none`` / …), carried through from the
    daemon envelope (which the Swift client also surfaces).
    """

    answer: str
    sources: list[ChatSource]
    coverage: ChatCoverage
    refusal: bool
    question_kind: str
    target: str
    store_state: str = _DEFAULT_STORE_STATE


# -- day-first result models (U13, R18) -------------------------------------
#
# KTD-1: the pointer vocabulary stays ``(recording, timestamp_ms)`` — the recording
# directory name is opaque plumbing; DAY + TIME is the user-facing surface. These
# models expose the day/time structure the UI browses by, each carrying the opaque
# ``recording`` key only so an agent can two-hop a moment to a frame (see the tool
# docstrings). R18: there is NO delete-shaped model here — deletion is human-only.


class DayInterval(BaseModel):
    """An absolute-ms span on the day timeline (a blocked / removed stretch)."""

    start_ms: int
    end_ms: int


class DayPurge(BaseModel):
    """A retroactive-purge span ("removed by your rules") + the disable target it
    was attributed to (identity fields null when the join was ambiguous)."""

    start_ms: int
    end_ms: int
    bundle_id: str | None = None
    app_name: str | None = None
    root_domain: str | None = None


class DayTask(BaseModel):
    """One named task on the day (``start_ts`` / ``end_ts`` are unix SECONDS)."""

    task_index: int
    start_ts: float
    end_ts: float
    name: str
    category: str | None = None
    confidence: str | None = None


class DayRecording(BaseModel):
    """One recording's day-clamped span + honest provenance split (R7).

    ``recording`` is the opaque storage key (KTD-1) — never a browsing label. The
    honest split keeps four distinct states the agent must NOT conflate:
    ``blocked_proven`` (provable capture-time masking), ``unverifiable`` (a
    coverage-gap / ambiguity that is NOT proof of masking), ``deleted`` ("removed
    by you", R8), and ``purged`` ("removed by your rules"). ``start_ms`` / ``end_ms``
    are absolute unix ms; a task's ``start_ts`` is unix seconds.
    """

    recording: str
    recording_id: str | None = None
    state: str
    start_ms: int
    end_ms: int
    blocked_proven: list[DayInterval] = []
    unverifiable: list[DayInterval] = []
    tasks: list[DayTask] = []
    deleted: list[DayInterval] = []
    purged: list[DayPurge] = []
    end_status: str | None = None


class DayResult(BaseModel):
    """A day's spans + tasks + provenance-labeled gaps (from ``/v0/timeline.day``).

    ``store_mounted`` False → the encrypted vault is locked/absent and the day is
    "can't verify", never a confident empty day. ``coverage_complete`` False → a
    recording couldn't be placed, so an empty stretch is not proof nothing is on
    file (R7). Both default to the plaintext-install / complete posture on an older
    daemon shape.
    """

    date: str
    recordings: list[DayRecording]
    store_mounted: bool = True
    coverage_complete: bool = True


class TaskHit(BaseModel):
    """One named task in the cross-day list, carrying its recording pointer.

    ``recording`` is opaque plumbing (KTD-1) retained so the agent can seek into
    the task's day page (``browse_day`` / ``query_timeline`` → ``resolve_frame``);
    ``start_ts`` / ``end_ts`` are unix seconds.
    """

    recording: str
    recording_id: str | None = None
    task_index: int
    start_ts: float
    end_ts: float
    name: str
    category: str | None = None
    confidence: str | None = None


class TaskDay(BaseModel):
    """All tasks mapping to one local calendar day (KTD-11), start-ordered."""

    date: str
    tasks: list[TaskHit]


class TaskRecordingStatus(BaseModel):
    """Per-recording honest-status rollup (R21): a recording that named no task is
    still listed with an honest ``state`` (``nothing_to_name`` / ``mechanical_only``
    / ``couldnt_run`` / ``in_progress`` / …), so "nothing on file" is never misread
    as "you did nothing"."""

    recording: str
    recording_id: str | None = None
    state: str
    reason: str | None = None
    detail: str | None = None


class TasksResult(BaseModel):
    """Cross-day named-task segments grouped by day + the honest-status rollup
    (from ``/v0/tasks.query``). ``days`` is newest-first, matching the UI grouping
    (the daemon owns the single KTD-11 day-mapping rule; this tool never re-buckets).
    """

    start_date: str
    end_date: str
    days: list[TaskDay]
    recordings: list[TaskRecordingStatus]
    store_state: str = _DEFAULT_STORE_STATE


class ClipResult(BaseModel):
    """The outcome of a ``create_clip`` cut (from ``/v0/clip.create``).

    ``ok`` reflects whether the clip was cut; on a clip-domain failure ``ok`` is
    False + ``reason`` (``policy_purged`` — fail-closed over a policy-purged range,
    R17 — / ``not_eligible`` / ``no_frames_in_range`` / ``masked_video_required`` /
    ``clip_busy`` / ``trim_failed``) and ``clip_id`` is null.

    ``creator`` is ``mcp`` for the agent path (R18 attribution).
    ``clip_video_capture_blocked_only`` is the surfaced honesty flag (the clip's
    video is capture-BLOCKED, not post-hoc masked); the full ``honesty_flags`` dict
    rides alongside so a future flag survives without a signature change. POINTER-
    ONLY: no media bytes — ``source_day`` + ``start_ms`` / ``end_ms`` locate it.
    """

    ok: bool
    reason: str | None = None
    clip_id: str | None = None
    source_day: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    creator: str | None = None
    clip_video_capture_blocked_only: bool = False
    honesty_flags: dict = {}
    store_state: str = _DEFAULT_STORE_STATE


# -- lazy daemon runtime (connect + held subscription on first tool call) ---


class _Runtime:
    def __init__(self) -> None:
        self._client: AsyncDaemonClient | None = None
        self._sub: LivenessSubscription | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> AsyncDaemonClient:
        async with self._lock:
            if self._client is None:
                # Auto-spawn the daemon if the socket is cold (F3 install).
                # ensure_daemon_or_spawn is sync + blocking — keep it off the
                # event loop so initialize stays responsive.
                from screencap.cli._autospawn import ensure_daemon_or_spawn

                try:
                    await asyncio.to_thread(
                        ensure_daemon_or_spawn, stderr_emitter=_stderr,
                    )
                except Exception as exc:
                    raise DaemonError(f"could not reach the Screencap daemon: {exc}") from exc
                client = AsyncDaemonClient()
                sub = LivenessSubscription(client.raw)
                try:
                    await sub.open()
                except Exception:
                    # Liveness is best-effort: if the subscription can't open
                    # (older daemon), still serve queries — the daemon may just
                    # idle-shut-down sooner.
                    logger.warning("liveness subscription failed to open", exc_info=True)
                self._client = client
                self._sub = sub
            return self._client

    async def aclose(self) -> None:
        async with self._lock:
            if self._sub is not None:
                await self._sub.aclose()
                self._sub = None
            if self._client is not None:
                await self._client.aclose()
                self._client = None


_RUNTIME = _Runtime()


async def _client() -> AsyncDaemonClient:
    """Return the (lazily opened) daemon client. Patched in tests."""
    return await _RUNTIME.client()


def _stderr(message: str) -> None:
    # MUST stay on stderr: the MCP stdio transport owns stdout for the JSON-RPC
    # stream, so a default (stdout) Console would corrupt it. ``stderr=True``
    # pins it there; ``highlight=False`` keeps the diagnostic text verbatim.
    from rich.console import Console

    Console(stderr=True, highlight=False).print(message)


# -- tools (module-level so they are directly unit-testable) -----------------


async def search_screen_content(
    query: str, recording: str | None = None, limit: int | None = None,
) -> ContentSearchResult:
    """Search on-screen text (OCR'd from local screenshots) by keyword.

    Best-effort recall — check ``index_state`` (``not_indexed`` /
    ``store_unavailable`` mean "no data", not "no match").
    """
    env = await (await _client()).content_search(
        query, recording=recording, limit=_clamp_or_none(limit),
    )
    return ContentSearchResult(
        hits=[ContentHit(**h) for h in env.get("hits", [])],
        index_state=env.get("index_state", "store_unavailable"),
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


async def search_diary(
    query: str, recording: str | None = None, limit: int | None = None,
) -> DiarySearchResult:
    """Search the day diary's named work BLOCKS by name + topic bullets (keyword).

    A diary block is a coherent, named stretch of work carrying short topic
    bullets; this finds one from a fuzzy topic memory ("kick", "design systems")
    across months of history. Each hit is POINTER-ONLY: a text ``snippet`` + the
    block's ``(recording, block_id)`` pointer and its absolute unix-ms span
    (``start_ms``/``end_ms``) — seek into the block's day via ``browse_day`` /
    ``query_timeline`` → ``resolve_frame``. Best-effort recall — check
    ``index_state`` (``not_indexed`` / ``store_unavailable`` mean "no data", not
    "no match"). The day narrative is never indexed, so it never surfaces here.
    """
    env = await (await _client()).diary_search(
        query, recording=recording, limit=_clamp_or_none(limit),
    )
    return DiarySearchResult(
        hits=[DiaryBlockHit(**h) for h in env.get("hits", [])],
        index_state=env.get("index_state", "store_unavailable"),
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


async def search_transcript(
    query: str, recording: str | None = None, limit: int | None = None,
) -> TranscriptSearchResult:
    """Search audio-transcript text by keyword. Pointer is chunk-granular."""
    env = await (await _client()).transcript_search(
        query, recording=recording, limit=_clamp_or_none(limit),
    )
    return TranscriptSearchResult(
        hits=[TranscriptHit(**h) for h in env.get("hits", [])],
        coverage=env.get("coverage", "best_effort"),
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


async def query_timeline(
    start_ms: int | None = None,
    end_ms: int | None = None,
    app: str | None = None,
    recording: str | None = None,
    limit: int | None = None,
) -> TimelineResult:
    """Query the authoritative app/window/time timeline from event tables.

    ``start_ms`` / ``end_ms`` are absolute unix ms. This is the first hop of the
    day+time → frame path: each row is a ``(recording, timestamp_ms)`` pointer —
    where ``recording`` is opaque plumbing (KTD-1), day + time is the vocabulary —
    that ``resolve_frame(recording, timestamp_ms)`` turns into a screenshot stem.
    Use ``browse_day`` for a whole day's spans/tasks/gaps; use this to scan by app
    or a precise window.
    """
    env = await (await _client()).timeline_query(
        start_ms=start_ms, end_ms=end_ms, app=app,
        recording=recording, limit=_clamp_or_none(limit),
    )
    return TimelineResult(
        rows=[TimelineRow(**r) for r in env.get("rows", [])],
        coverage=env.get("coverage", "authoritative"),
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


async def resolve_frame(
    recording: str, timestamp_ms: int, staleness_cap_ms: int | None = None,
) -> FrameNearest:
    """Resolve a (recording, timestamp_ms) pointer to the nearest screenshot stem.

    Returns the on-disk screenshot **stem** (e.g. ``"1719400010.000000"``) of the
    frame nearest ``timestamp_ms`` within ``staleness_cap_ms`` (default 30s) plus
    a signed ``delta_ms`` (``frame_ms - timestamp_ms``). POINTER-ONLY: never image
    bytes — read the file yourself at
    ``~/.screencap/recordings/<recording>/screenshots/<stem>.jpg``.

    The frame is ALLOW-filtered: a frame the privacy pipeline masked or excluded
    is never returned (a masked moment resolves to the nearest unmasked frame, or
    to a null miss). ``stem``/``delta_ms`` are null on a miss — no frame within
    cap, no frames, or indeterminate privacy state (fail-closed).

    For a transcript hit, pass its ``timestamp_ms`` AND
    ``staleness_cap_ms=chunk_duration_ms``: that anchor is chunk-granular
    (``timestamp_granularity="chunk"``), so the resolved frame is representative of
    the whole chunk and ``delta_ms`` is the offset from the chunk start — NOT the
    distance to the matched word. Content and timeline hits carry an exact
    ``timestamp_ms`` and need no special cap.
    """
    env = await (await _client()).frame_nearest(
        recording, timestamp_ms, staleness_cap_ms=staleness_cap_ms,
    )
    return FrameNearest(
        stem=env.get("stem"),
        delta_ms=env.get("delta_ms"),
        encrypted=bool(env.get("encrypted", False)),
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


async def read_frame(recording: str, stem: str) -> FrameBytes:
    """Fetch the decrypted bytes of a screenshot the corpus stores encrypted (U8).

    When ``resolve_frame`` returns ``encrypted: true`` the ``.jpg`` on disk is
    ``.jpg.enc`` (AES-256-GCM) and you cannot read it directly — call this with the
    resolved ``stem`` and the daemon decrypts + serves the JPEG bytes as base64.

    Fail-closed: ``image_base64`` is null when the frame is missing, was
    masked/excluded (ALLOW-only), or is in a chunk not yet secrets-scrubbed. Every
    call is audit-logged by the daemon. Size-capped; a decoded JPEG is returned as
    ``image/jpeg``."""
    env = await (await _client()).frame_read(recording, stem)
    return FrameBytes(
        image_base64=env.get("image_base64"),
        content_type=env.get("content_type"),
    )


async def list_recordings() -> RecordingsResult:
    """List available recordings (name, date, duration, flags).

    Each recording carries ``owner_uid`` (the cloud account that owns it, or
    ``null`` for a local/legacy recording) and a best-effort ``upload_warning``.
    To tell a permanently-blocked recording (wrong account signed in) apart from
    a transient upload failure, compare ``owner_uid`` against ``whoami().uid``.

    Ownership comparison rules:
    - Only treat a recording as account-blocked when ``whoami().signed_in`` is
      True, ``whoami().stale`` is False, ``whoami().uid`` is non-null, the
      recording's ``owner_uid`` is non-null, and ``owner_uid != uid``.
    - When ``owner_uid`` is None (legacy or local recording): ownership is
      UNKNOWN — do NOT block on a mismatch.
    """
    env = await (await _client()).list_recordings()
    keep = set(RecordingSummary.model_fields)
    return RecordingsResult(
        recordings=[
            RecordingSummary(**{k: v for k, v in rec.items() if k in keep})
            for rec in env.get("recordings", [])
        ],
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


async def whoami() -> WhoAmIResult:
    """Report the cloud account currently signed in on this machine.

    Use the returned ``uid`` to interpret each recording's ``owner_uid`` from
    ``list_recordings``: an un-uploaded recording whose ``owner_uid`` differs
    from this ``uid`` is blocked because a different account is signed in (advise
    re-login as the owner), not merely waiting on a retry.

    Guards for safe comparison:
    - Only conclude a wrong-account block when ``signed_in`` is True, ``stale``
      is False, ``uid`` is non-null, the recording's ``owner_uid`` is non-null,
      and ``owner_uid != uid``.
    - When ``stale`` is True (offline; ``uid`` is None though signed in): the
      account is temporarily UNVERIFIABLE — do NOT conclude a mismatch.
    - When ``owner_uid`` is None (legacy or local recording): ownership is
      UNKNOWN, not a block.
    """
    env = await (await _client()).whoami()
    keep = set(WhoAmIResult.model_fields)
    return WhoAmIResult(**{k: v for k, v in env.items() if k in keep})


# Answer prose can be long (a period summary narrates several sources); bound it
# well above any plausible answer while still capping a runaway/hostile model.
_MAX_ANSWER_LEN = 4000


async def chat_answer(
    question: str,
    prior_turns: list[dict] | None = None,
    window_ms: tuple[int, int] | None = None,
    app: str | None = None,
    limit: int | None = None,
) -> ChatAnswerResult:
    """Ask a grounded question about the recorded history; get generated prose + sources.

    Fields both **point lookups** ("what was that error at 2pm?") and **period
    summaries** ("how much time in Salesforce today?"). The answer is drawn ONLY
    from retrieved, privacy-stripped evidence — when nothing matches, ``refusal``
    is true and the answer says so rather than fabricating.

    POINTER-ONLY (R15): ``sources`` are ``(recording, timestamp_ms, stream)``
    locators — never image bytes or file paths. Resolve a source to a frame with
    ``resolve_frame(recording, timestamp_ms)``. Check ``coverage.state`` (``ok`` /
    ``no_matching_moments`` / ``not_indexed`` / …) before trusting completeness.

    ``prior_turns`` carries prior-turn source pointers
    (``[{recording, timestamp_ms, stream}]``) for a multi-turn follow-up (R14) —
    NEVER prose; the daemon re-derives the evidence from the pointers.
    ``window_ms`` is an optional ``(start_ms, end_ms)`` pair for a period-summary
    question; ``app`` narrows it; ``limit`` bounds retrieval.
    """
    env = await (await _client()).chat_answer(
        question,
        prior_turns=prior_turns or None,
        window_ms=window_ms,
        app=app,
        limit=_clamp_or_none(limit),
    )
    # OUTPUT-sanitize the model prose before it reaches the agent. This is the
    # first MCP surface returning model-generated text, and the answer is
    # influenced by on-screen/transcript content (attacker-influenceable), so it
    # can carry injected markup / control sequences aimed at the downstream
    # consumer. Reuse the exact primitive that hardens model-emitted task names.
    from screencap.segmentation.sanitize import _clean_text

    cov = env.get("coverage") or {}
    return ChatAnswerResult(
        answer=_clean_text(env.get("answer", ""), _MAX_ANSWER_LEN),
        sources=[
            ChatSource(
                recording=s["recording"],
                timestamp_ms=s["timestamp_ms"],
                stream=s["stream"],
            )
            for s in env.get("sources", [])
        ],
        coverage=ChatCoverage(
            state=cov.get("state", "store_unavailable"),
            note=cov.get("note", ""),
            per_stream=cov.get("per_stream", {}),
        ),
        refusal=bool(env.get("refusal", False)),
        question_kind=env.get("question_kind", "point"),
        target=env.get("target", "none"),
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


# -- day-first browse tools (U13, R18) --------------------------------------
#
# DAY + TIME is the user-facing vocabulary (KTD-1): browse a day, browse tasks
# across days, cut a clip — all keyed by day + absolute-ms time, with the
# ``recording`` directory name carried only as opaque plumbing. To pinpoint a
# frame, TWO-HOP: (1) window a day with ``browse_day`` (or ``query_timeline``) to
# get a ``(recording, timestamp_ms)`` pointer, then (2) ``resolve_frame`` that
# pointer to an on-disk screenshot stem. R18: there is deliberately NO range-delete
# and NO clip-delete tool — deletion stays human-only, enforced at this surface.


async def browse_day(date: str, tz_offset_seconds: int = 0) -> DayResult:
    """Browse one local calendar day: recordings' spans, named tasks, and honest gaps.

    ``date`` is ``YYYY-MM-DD``; ``tz_offset_seconds`` is seconds EAST of UTC (the
    day is intersected against the local calendar day at that offset — the single
    KTD-11 day rule the UI uses). Returns each recording's day-clamped span plus its
    named ``tasks`` and its gaps split by PROVENANCE so you never conflate them:
    ``blocked_proven`` (provable capture-time masking), ``unverifiable`` (a coverage
    gap that is NOT proof of masking — do not label it "blocked"), ``deleted``
    ("removed by you"), and ``purged`` ("removed by your rules").

    Recording identifiers are opaque plumbing (KTD-1) — day + time is what you
    browse and cite by. To open a moment: pick a time inside a span, then
    ``resolve_frame(recording, timestamp_ms)`` for its screenshot stem (the two-hop
    day+time → frame path). Check ``store_mounted`` / ``coverage_complete`` before
    trusting an empty stretch: a locked vault or an unplaceable recording makes
    "nothing here" unprovable, not a fact.
    """
    env = await (await _client()).timeline_day(
        date=date, tz_offset_seconds=tz_offset_seconds,
    )
    return DayResult(
        date=env.get("date", date),
        recordings=[
            DayRecording(
                recording=rec.get("name", ""),
                recording_id=rec.get("recording_id"),
                state=rec.get("state", "unknown"),
                start_ms=rec.get("start_ms", 0),
                end_ms=rec.get("end_ms", 0),
                blocked_proven=[DayInterval(**i) for i in rec.get("blocked_proven", [])],
                unverifiable=[DayInterval(**i) for i in rec.get("unverifiable", [])],
                tasks=[DayTask(**t) for t in rec.get("tasks", [])],
                deleted=[DayInterval(**i) for i in rec.get("deleted", [])],
                purged=[
                    DayPurge(**{k: p.get(k) for k in DayPurge.model_fields})
                    for p in rec.get("purged", [])
                ],
                end_status=rec.get("end_status"),
            )
            for rec in env.get("recordings", [])
        ],
        store_mounted=bool(env.get("store_mounted", True)),
        coverage_complete=bool(env.get("coverage_complete", True)),
    )


async def query_tasks(
    start_date: str, end_date: str, tz_offset_seconds: int = 0,
) -> TasksResult:
    """List named tasks across a range of days, grouped by day (newest first).

    ``start_date`` / ``end_date`` are ``YYYY-MM-DD``; ``tz_offset_seconds`` is
    seconds EAST of UTC. Each task carries its opaque ``recording`` key (KTD-1
    plumbing) plus its unix-second ``start_ts`` / ``end_ts`` — so you can seek into
    the task's day page (``browse_day`` / ``query_timeline`` → ``resolve_frame``).

    ``recordings`` is an honest per-recording status rollup (R21): a recording that
    named no task is still listed with a state such as ``nothing_to_name`` /
    ``mechanical_only`` / ``couldnt_run`` — so "nothing on file" is never misread as
    "you did nothing". Day grouping is the daemon's single KTD-11 rule (a task maps
    to the local day of its start); this is a browse surface — a locked/absent vault
    returns an empty list with a degraded ``store_state``, not an error.
    """
    env = await (await _client()).tasks_query(
        start_date=start_date, end_date=end_date, tz_offset_seconds=tz_offset_seconds,
    )
    return TasksResult(
        start_date=env.get("start_date", start_date),
        end_date=env.get("end_date", end_date),
        days=[
            TaskDay(
                date=d.get("date", ""),
                tasks=[TaskHit(**t) for t in d.get("tasks", [])],
            )
            for d in env.get("days", [])
        ],
        recordings=[
            TaskRecordingStatus(
                recording=r.get("name", ""),
                recording_id=r.get("recording_id"),
                state=r.get("state", "unknown"),
                reason=r.get("reason"),
                detail=r.get("detail"),
            )
            for r in env.get("recordings", [])
        ],
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


async def create_clip(
    recording: str, start_ms: int, end_ms: int, tz_offset_seconds: int = 0,
) -> ClipResult:
    """Cut an absolute-ms time range of a recording into a durable, kept clip.

    ``start_ms`` / ``end_ms`` are ABSOLUTE unix ms (the timeline units ``browse_day``
    / ``query_timeline`` speak); ``recording`` is the opaque key from one of those
    pointers (KTD-1). Attribution is automatic: the clip records ``creator="mcp"``,
    so a clip the agent cut stays distinguishable from one a human cut (R18).

    Fails CLOSED rather than resurrect removed footage: a range overlapping a
    policy-purged span returns ``ok=false`` + ``reason="policy_purged"`` and writes
    nothing (R17); other clip-domain misses (``no_frames_in_range`` /
    ``not_eligible`` / …) ride ``reason`` the same way — never an exception.
    ``clip_video_capture_blocked_only`` surfaces the honesty signal that the clip's
    video is capture-blocked (not post-hoc masked). There is NO clip-delete tool —
    removing a clip is human-only (R18).
    """
    env = await (await _client()).clip_create(
        recording=recording, start_ms=start_ms, end_ms=end_ms,
        tz_offset_seconds=tz_offset_seconds, creator="mcp",
    )
    clip = env.get("clip") or {}
    flags = clip.get("honesty_flags") or {}
    return ClipResult(
        ok=bool(env.get("ok", False)),
        reason=env.get("reason"),
        clip_id=clip.get("id"),
        source_day=clip.get("source_day"),
        start_ms=clip.get("start_ms"),
        end_ms=clip.get("end_ms"),
        creator=clip.get("creator"),
        clip_video_capture_blocked_only=bool(
            flags.get("clip_video_capture_blocked_only", False)
        ),
        honesty_flags=flags,
        store_state=env.get("store_state", _DEFAULT_STORE_STATE),
    )


# -- server ------------------------------------------------------------------


def build_server() -> FastMCP:
    import contextlib

    @contextlib.asynccontextmanager
    async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield {}
        finally:
            await _RUNTIME.aclose()

    mcp = FastMCP(
        "screencap",
        instructions=(
            "Search a local Screencap screen-recording memory. Returns text "
            "snippets + (recording, timestamp) pointers, never frame images. "
            "On-screen content recall is best-effort (action-gated frames, OCR "
            "and redaction limits); the timeline is authoritative."
        ),
        lifespan=_lifespan,
    )
    for fn in (
        search_screen_content, search_diary, search_transcript, query_timeline,
        resolve_frame, read_frame, list_recordings, whoami, chat_answer,
        # U13 (R18): day-first browse tools. NO delete-shaped tool — deletion is
        # human-only, enforced by its absence from this surface.
        browse_day, query_tasks, create_clip,
    ):
        mcp.tool()(fn)
    return mcp


def run_stdio() -> None:
    """Entrypoint for ``screencap mcp`` — serve over stdio (stderr-only logging)."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(asctime)s screencap.mcp %(levelname)s %(message)s",
    )
    build_server().run(transport="stdio")
