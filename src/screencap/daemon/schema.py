"""Daemon API contract models and response envelope helpers."""

from __future__ import annotations

from functools import cache
from typing import Any

API_SCHEMA_VERSION = 1
_DAEMON_INFO_API_VERSION = 1
_LIST_API_VERSION = 1
_SNAPSHOT_API_VERSION = 1
_EVENTS_API_VERSION = 1
_RECORDING_START_API_VERSION = 1
RECORDING_START_API_VERSION = _RECORDING_START_API_VERSION  # public alias
_RECORDING_STOP_API_VERSION = 1
_PERMISSION_REQUEST_API_VERSION = 1
# SCR-200 identity-scoped decoy/orphan TCC cleanup verb. Additive (new verb) — no
# global API_SCHEMA_VERSION bump (mirrors the permission.request precedent).
_PERMISSION_CLEANUP_API_VERSION = 1
# SCR-118 read-only query verbs.
_CONTENT_SEARCH_API_VERSION = 1
_TRANSCRIPT_SEARCH_API_VERSION = 1
_TIMELINE_QUERY_API_VERSION = 1
_TIMELINE_DAY_API_VERSION = 1
# SCR-186 nearest-frame resolution verb. Additive (new verb); transcript.search
# gains nullable timing fields without an API bump (mirrors the additive
# `daemon.info` permissions precedent — older clients ignore unknown keys).
_FRAME_NEAREST_API_VERSION = 1
# SCR-179 query-parser vocabulary verb. Additive (new verb) — no global
# API_SCHEMA_VERSION bump (mirrors the `permissions`/SCR-148 additive precedent).
_APPS_LIST_API_VERSION = 1
# SCR-148 cloud account-mismatch observability.
_AUTH_WHOAMI_API_VERSION = 1
# SCR-178 content-index backfill lifecycle verbs.
_BACKFILL_API_VERSION = 1
# U10 (local-first intelligence) read verb: the named-task segments a LOCAL
# recording's terminal-stage segmentation persisted (U4). Additive (new verb) —
# no global API_SCHEMA_VERSION bump (mirrors the frame.nearest / apps.list
# additive precedent).
_TASKS_LIST_API_VERSION = 1


@cache
def daemon_version() -> str:
    """Return the installed ScreenCap package version used by this daemon."""
    from screencap import __version__

    return str(__version__)


def envelope(*, schema_version: int, ok: bool = True, **payload: Any) -> dict[str, Any]:
    """Build the uniform daemon API envelope used by every response."""
    return {
        "ok": ok,
        "schema_version": schema_version,
        "daemon_version": daemon_version(),
        "api_schema_version": API_SCHEMA_VERSION,
        **payload,
    }


_MODEL_NAMES = {
    "EnvelopeResponse",
    "DaemonInfoResponse",
    "PermissionGrants",
    "RecordingSummary",
    "ListResponse",
    "SessionSnapshotResponse",
    "RecordingStartRequest",
    "RecordingStartResponse",
    "RecordingStopRequest",
    "RecordingStopResponse",
    "PermissionRequestRequest",
    "PermissionRequestResponse",
    "ContentSearchRequest",
    "ContentHit",
    "ContentSearchResponse",
    "TranscriptSearchRequest",
    "TranscriptHit",
    "TranscriptSearchResponse",
    "TimelineQueryRequest",
    "TimelineRow",
    "TimelineQueryResponse",
    "TimelineDayRequest",
    "DayBlockedInterval",
    "DaySegmentRecording",
    "TimelineDayResponse",
    "FrameNearestRequest",
    "FrameNearestResponse",
    "AppsListResponse",
    "WhoAmIResponse",
    "BackfillStartRequest",
    "BackfillCancelRequest",
    "BackfillStatusResponse",
    "BackfillProgressEvent",
    "TasksListRequest",
    "TaskSegment",
    "TasksListResponse",
}
_MODELS: dict[str, Any] | None = None


def _load_models() -> dict[str, Any]:
    """Construct Pydantic models on first use, keeping helpers Pydantic-free."""
    global _MODELS
    if _MODELS is not None:
        return _MODELS

    from pydantic import BaseModel, ConfigDict, Field

    # Bound query/filter strings so a single request can't drive an unbounded
    # scan (DoS guard at the daemon boundary, before any to_thread work).
    _MAX_QUERY_LEN = 1024

    class _DaemonModel(BaseModel):
        """Shared Pydantic model settings for documented daemon responses."""

        model_config = ConfigDict(extra="ignore")

    class EnvelopeResponse(_DaemonModel):
        ok: bool
        schema_version: int
        daemon_version: str
        api_schema_version: int

    class PermissionGrants(_DaemonModel):
        """Tri-state TCC grant block (U2, additive on ``daemon.info``).

        Each value is one of ``"granted"`` / ``"denied"`` / ``"indeterminate"``
        (see ``screencap.daemon.permission_probe``). Typed as ``str`` rather
        than a ``Literal`` so an unexpected token from a future probe decodes
        tolerantly instead of failing envelope validation; the app maps any
        unknown value (and an absent block from an older daemon) to
        indeterminate.
        """

        screen_recording: str
        accessibility: str
        input_monitoring: str

    class DaemonInfoResponse(EnvelopeResponse):
        build: str | None
        started_at: float
        # Additive (U2): older daemons omit this; the app decodes an absent
        # block as all-indeterminate, so no API version bump is required.
        permissions: PermissionGrants | None = None

    class RecordingSummary(_DaemonModel):
        """Recording summary shape returned by ``catalog.list_recordings()``."""

        name: str
        date: str
        duration: str
        size_mb: str
        has_audio: bool
        transcribed: bool
        uploaded: bool
        drops: dict[str, int] | None = None
        is_stub: bool = False
        chunks_total: int = 0
        chunks_uploaded: int = 0
        # U9: True for chunked recordings (vs legacy single-file). Kept in
        # sync with catalog.RecordingInfo.is_chunked — the recording.list verb
        # asserts the two field sets match (app.py).
        is_chunked: bool = False
        intent: str | None = None
        started_at: float | None = None
        duration_seconds: float | None = None
        # SCR-148: kept in EXACT sync with catalog.RecordingInfo — recording.list
        # (app.py) asserts the two field sets match, so this block must track any
        # change to RecordingInfo's SCR-148 fields. Additive on the wire: older
        # clients ignore unknown keys, so no _LIST_API_VERSION bump is required.
        owner_uid: str | None = None
        upload_warning: str | None = None
        # U2 (prototype UI): additive fields the new SwiftUI surfaces render. Kept
        # in EXACT sync with catalog.RecordingInfo (recording.list asserts field
        # parity). Additive on the wire — no _LIST_API_VERSION bump. Swift decoders
        # must NOT gate readiness on them (nullable-timing contract): decode
        # each with `decodeIfPresent` so an older daemon that omits them still
        # decodes.
        size_bytes: int = 0
        summary: str | None = None
        title: str = ""
        state: str = "ready"
        recording_id: str | None = None

    class ListResponse(EnvelopeResponse):
        recordings: list[RecordingSummary]

    class SessionSnapshotResponse(EnvelopeResponse):
        is_recording: bool | None
        daemon_owned: bool
        recording_name: str | None
        started_at: float | None
        claimant: str | None
        recovering: bool = False
        claimant_pid: int | None = None
        claimant_started_at: float | None = None
        engine_pid: int | None = None
        frames_written: int | None = None
        # Phase 2 U2: server-derived provenance classification.
        # Populated for daemon-owned sessions; None otherwise.
        started_by: str | None = None
        cursor: int

    class RecordingStartRequest(_DaemonModel):
        name: str | None = None
        # Phase 2 U2: ``started_by`` is now server-derived from the
        # peer socket (``LOCAL_PEEREPID`` + ``proc_pidpath`` +
        # ``KERN_PROCARGS2``). The field stays Optional for backwards
        # compatibility — callers that supply it continue to be
        # accepted, but the daemon ignores the value and logs the drop
        # at DEBUG level. No API version bump; existing clients keep
        # working unchanged.
        # NOTE: Do not use Pydantic ``Field(deprecated=...)`` here —
        # that triggers a DeprecationWarning on *every* model_validate
        # call when the field is present, flooding the daemon logs.
        started_by: str | None = None
        description: str | None = None
        audio: bool | None = None
        output_dir: str | None = None
        wifi_metrics: bool | None = None
        app_versions: bool | None = None
        capture_video: bool | None = None
        capture_images: bool | None = None
        capture_window_data: bool | None = None
        verbose: bool = False
        chunk_duration: float | None = None
        live_upload: bool = True
        force_mode: str | None = None
        cloud_intent: bool = False
        keep_local: bool = True
        intent_source: str = "flag"
        segmentation_mode: str = "llm"
        scrub_enabled: bool = True
        show_on_website: bool = True
        network: bool = False

    class RecordingStartResponse(EnvelopeResponse):
        session_id: str
        started_at: float
        # Required in v1: ``supervisor.spawn`` always populates the spawned
        # engine's PID on a successful start, so this is non-optional (contrast
        # ``SessionSnapshotResponse.engine_pid``, which is ``int | None`` because
        # it is absent when no recording is in flight).
        engine_pid: int
        # `cursor` is the bus cursor captured BEFORE the engine spawn — clients
        # subscribe to /v0/events?since=<cursor> after start to receive the
        # `started` event without an extra `session.snapshot` round-trip.
        cursor: int
        # U2 (prototype UI): echo the EFFECTIVE audio state so U6/U7 reflect what
        # the engine actually did. Additive — a STALE daemon omits it, and the app
        # treats a missing/mismatched echo as audio-on (its default). Optional on
        # the wire for exactly that back-compat reason.
        audio: bool | None = None

    class RecordingStopRequest(_DaemonModel):
        force: bool = False
        expected_claimant_pid: int | None = None
        expected_started_at: float | None = None

    class RecordingStopResponse(EnvelopeResponse):
        stopped: bool
        final_state: str

    class PermissionRequestRequest(_DaemonModel):
        """On-demand daemon-driven registration request (U8).

        ``permission`` is one of the canonical three
        (``screen_recording`` / ``accessibility`` / ``input_monitoring``).
        It is validated against the allowlist in the route handler (not via a
        ``Literal`` here) so an out-of-allowlist value returns a typed
        ``invalid_permission`` 4xx rather than a pydantic validation 422 — the
        same gate shape ``recording.start`` uses for recording names.
        """

        permission: str

    class PermissionRequestResponse(EnvelopeResponse):
        # Echo of the permission the daemon ran the registration mechanism for.
        permission: str
        # The request API's immediate return (True == already granted in the
        # daemon's process at call time). ADVISORY ONLY: the daemon's
        # in-process TCC state can be stale, so the app re-probes daemon.info
        # (a fresh subprocess) for the authoritative post-grant state rather
        # than gating on this value.
        already_granted: bool

    class ContentSearchRequest(_DaemonModel):
        """SCR-118 on-screen content search input."""

        query: str = Field(max_length=_MAX_QUERY_LEN)
        recording: str | None = None
        limit: int | None = None

    class ContentHit(_DaemonModel):
        """A single content match — POINTER ONLY.

        Structurally incapable of carrying a media path or image bytes: text
        snippet + ``(recording, timestamp_ms)`` pointer + bm25 score. R8 is a
        property of this shape, enforced at the daemon boundary.
        """

        recording: str
        timestamp_ms: int
        snippet: str
        score: float

    class ContentSearchResponse(EnvelopeResponse):
        hits: list[ContentHit]
        # content_index.IndexState value: ok / no_match / not_indexed /
        # index_degraded / store_unavailable. Typed as str (not Literal) so a
        # future state decodes tolerantly.
        index_state: str

    class TranscriptSearchRequest(_DaemonModel):
        """SCR-118 transcript keyword-search input."""

        query: str = Field(max_length=_MAX_QUERY_LEN)
        recording: str | None = None
        limit: int | None = None

    class TranscriptHit(_DaemonModel):
        """A transcript match. Pointer is chunk-granular (the scrubbed .txt has
        no fine timestamps — the rich per-word .json is an R7 leak we never
        read); an agent correlates precise time via timeline.query.

        SCR-186: additive nullable timing makes a hit resolvable by
        ``frame.nearest``. ``timestamp_ms`` is the chunk's ``chunk_start``
        (epoch ms); ``timestamp_granularity`` is ``"chunk"`` to flag the anchor
        as coarse (chunks default to 15 min and carry no per-word timing — a
        resolved frame is representative of the chunk, NOT the matched word);
        ``chunk_duration_ms`` is the cap an agent should pass to ``frame.nearest``
        so the chunk-coarse anchor resolves instead of missing the 30s default.
        All three are null when the chunk manifest is absent/unreadable.
        """

        recording: str
        chunk_index: int
        snippet: str
        timestamp_ms: int | None = None
        timestamp_granularity: str | None = None
        chunk_duration_ms: int | None = None

    class TranscriptSearchResponse(EnvelopeResponse):
        hits: list[TranscriptHit]
        # 'best_effort' — transcript recall lags transcription. (Coherent
        # interface != coherent recall.)
        coverage: str

    class TimelineQueryRequest(_DaemonModel):
        """SCR-118 timeline query input (absolute unix ms time range)."""

        start_ms: int | None = None
        end_ms: int | None = None
        app: str | None = Field(default=None, max_length=_MAX_QUERY_LEN)
        recording: str | None = None
        limit: int | None = None

    class TimelineRow(_DaemonModel):
        """A structured app/window/time row from the event tables.

        ``browser_url`` is deliberately omitted from the OUTPUT: it is captured
        pre-scrubber and can carry OAuth codes / session tokens, so the
        lowest-risk timeline shape is app + window title + time. Since SCR-179 it
        is read internally as a *domain filter predicate* — the ``app`` token may
        match a hostname derived from ``browser_url`` — but only the hostname
        (never the path/query) is inspected and the URL never enters a row.
        """

        recording: str
        timestamp_ms: int
        app: str | None
        title: str | None

    class TimelineQueryResponse(EnvelopeResponse):
        rows: list[TimelineRow]
        # 'authoritative' — event tables, no OCR/redaction recall loss.
        coverage: str

    class TimelineDayRequest(_DaemonModel):
        """U3 day-timeline input: a local calendar day + its UTC offset.

        ``date`` is ``YYYY-MM-DD`` (format validated in the handler → typed 400).
        ``tz_offset_seconds`` is seconds EAST of UTC, bounded to ±14h (the widest
        real-world offset) as a boundary abuse guard.
        """

        date: str = Field(max_length=32)
        tz_offset_seconds: int = Field(default=0, ge=-50_400, le=50_400)

    class DayBlockedInterval(_DaemonModel):
        """A blocked span on the day timeline, in absolute unix ms.

        Named distinctly from ``scrubber.BlockedInterval`` (a different, seconds-
        based dataclass) — this is the ms wire shape the UI hatches.
        """

        start_ms: int
        end_ms: int

    class DaySegmentRecording(_DaemonModel):
        """One recording's day-clamped span + honest blocked-interval split (U3).

        ``blocked_proven`` is provable MASK/EXCLUDE masking (safe to label
        "blocked"); ``unverifiable`` is fail-closed coverage-gap / null-column
        ambiguity the UI must render as a neutral gap, never "blocked" (R7).
        """

        name: str
        recording_id: str | None = None
        state: str
        start_ms: int
        end_ms: int
        blocked_proven: list[DayBlockedInterval]
        unverifiable: list[DayBlockedInterval]

    class TimelineDayResponse(EnvelopeResponse):
        date: str
        recordings: list[DaySegmentRecording]

    # SCR-186 frame.nearest input bounds. ``timestamp_ms`` is bounded to a
    # realistic epoch ceiling (year 9999) and ``staleness_cap_ms`` to 24h —
    # comfortably above any chunk duration — so a direct UDS caller cannot drive
    # an unbounded cap or pass a nonsensical anchor (DoS / abuse guard at the
    # boundary, mirroring the SCR-118 query caps).
    _MAX_EPOCH_MS = 253_402_300_800_000  # year 9999 in unix ms
    _MAX_STALENESS_CAP_MS = 86_400_000   # 24 hours in ms

    class FrameNearestRequest(_DaemonModel):
        """SCR-186 nearest-frame resolution input.

        Maps ``(recording, timestamp_ms)`` to the nearest on-disk ALLOW frame
        within ``staleness_cap_ms``. ``recording`` is validated by the canonical
        name validator in the handler (traversal-safe), not via a ``Literal``.
        """

        recording: str
        timestamp_ms: int = Field(ge=0, le=_MAX_EPOCH_MS)
        staleness_cap_ms: int = Field(default=30_000, ge=0, le=_MAX_STALENESS_CAP_MS)

    class FrameNearestResponse(EnvelopeResponse):
        """Nearest-frame result — POINTER ONLY.

        ``stem`` is the on-disk screenshot stem (e.g. ``"1719400010.000000"``),
        never a path or image bytes (priv-R8); the agent expands it to
        ``screenshots/<stem>.jpg`` with its same-EUID filesystem access.
        ``delta_ms`` is signed (``frame_ms - timestamp_ms``). Both are null on a
        legitimate miss (no ALLOW frame within cap, no frames, or indeterminate
        blocked geometry → fail-closed).
        """

        stem: str | None
        delta_ms: int | None

    class AppsListResponse(EnvelopeResponse):
        """SCR-179 vocabulary source for the in-app query parser.

        Distinct ``app_name`` / ``app_bundle_id`` values plus **bare hostnames**
        derived from ``browser_url`` (never a full URL — the path/query, where
        OAuth codes / session tokens live, never leaves the daemon). The
        hostname list is a same-EUID-only *browsing-profile* artifact derived
        from the local-only ``recording.db``: it must never cross the EUID
        boundary or any sync / export / telemetry path (R6). ``truncated`` flags
        that a distinct-value cap was hit so the client knows the vocabulary is
        partial rather than complete.
        """

        app_names: list[str]
        app_bundles: list[str]
        hostnames: list[str]
        truncated: bool

    class WhoAmIResponse(EnvelopeResponse):
        """SCR-148: the currently signed-in cloud account on this daemon.

        Mirrors the ``auth.whoami`` shape (and the CLI ``whoami --json``
        envelope). An agent compares ``uid`` against a recording's
        ``owner_uid`` to tell a permanent account-mismatch block (re-login as
        the right account) apart from a transient upload failure. ``stale`` is
        True when a refresh-token exists but could not be refreshed (offline),
        so ``uid``/``email`` are unknown though the user is nominally signed in.
        """

        signed_in: bool
        uid: str | None = None
        email: str | None = None
        stale: bool = False
        # Cloud-paywall entitlement (display/UX only; the signer's hard gate is
        # the real enforcement). Additive, defaulted, backward-compatible.
        subscribed: bool = False

    class BackfillStartRequest(_DaemonModel):
        """SCR-178 ``backfill.start`` input.

        The request carries no required fields — the backfill enumerates the
        user's existing recordings server-side. Optional knobs are accepted but
        deliberately NOT exposed on the public verb (production callers send an
        empty body); they exist so a future plan-tier / operator override can
        bound the run without an API bump.
        """

        # Optional, but must be strictly positive when supplied — a non-positive
        # budget/cap is a client error, not a "run forever / index nothing" knob.
        budget_s: float | None = Field(default=None, gt=0)
        max_frames_per_recording: int | None = Field(default=None, gt=0)

    class BackfillCancelRequest(_DaemonModel):
        """SCR-178 ``backfill.cancel`` input (no parameters)."""

    class BackfillStatusResponse(EnvelopeResponse):
        """The privacy-safe backfill status snapshot (R9).

        Carries ONLY the run state + frozen-denominator counts + an OPAQUE
        ordinal (``current_unit_index``). The recording directory name is
        structurally absent — the EventBus is readable by any same-EUID
        subscriber (incl. the MCP ``/v0/events`` stream), so a dir name (which
        encodes timing/context) must never cross this boundary. ``state`` is
        typed ``str`` (not ``Literal``) so a future run-state value decodes
        tolerantly: one of ``idle`` / ``running`` / ``paused`` / ``cancelled`` /
        ``completed`` / ``failed``.
        """

        state: str
        done: int
        skipped: int
        failed: int
        total: int
        current_unit_index: int

    class BackfillProgressEvent(_DaemonModel):
        """The ``backfill.progress`` / ``backfill.<terminal>`` EventBus payload.

        Same privacy-safe shape as :class:`BackfillStatusResponse` minus the
        envelope fields — published on ``/v0/events`` for live progress. NO
        recording directory name (R9). ``type`` is the event name
        (``backfill.progress`` / ``backfill.completed`` / ``backfill.paused`` /
        ``backfill.cancelled`` / ``backfill.failed``).
        """

        type: str
        state: str
        done: int
        skipped: int
        failed: int
        total: int
        current_unit_index: int

    class TasksListRequest(_DaemonModel):
        """U10 ``tasks.list`` input: the recording whose named tasks to read.

        ``recording`` is validated by the canonical name validator in the handler
        (traversal-safe), not via a ``Literal`` — same posture as
        ``frame.nearest`` / ``content.search``.
        """

        recording: str

    class TaskSegment(_DaemonModel):
        """One named task segment from a LOCAL recording's tasks store (U4).

        Read from the ``pipeline_task_segments`` ledger table inside the
        local-only ``recording.db`` (never uploaded — R4/R8), so these named
        tasks stay on the Mac. ``start_ts`` / ``end_ts`` are Unix seconds (the
        ledger's native units). ``category`` / ``confidence`` are optional
        provider metadata; the idle-gap heuristic fallback (U7) emits neither.
        """

        task_index: int
        start_ts: float
        end_ts: float
        name: str
        category: str | None = None
        confidence: str | None = None

    class TasksListResponse(EnvelopeResponse):
        """A recording's named-task segments, ordered by ``task_index``.

        ``recording`` echoes the requested name. ``tasks`` is empty (never
        absent) for a recording with no tasks store — a provider miss, the
        heuristic producing none, a legacy recording, or a cloud/both recording
        (whose tasks store is local-only and thus never present remotely). The
        app renders the empty case gracefully rather than treating it as an
        error.
        """

        recording: str
        tasks: list[TaskSegment]

    _MODELS = {
        "EnvelopeResponse": EnvelopeResponse,
        "DaemonInfoResponse": DaemonInfoResponse,
        "PermissionGrants": PermissionGrants,
        "RecordingSummary": RecordingSummary,
        "ListResponse": ListResponse,
        "SessionSnapshotResponse": SessionSnapshotResponse,
        "RecordingStartRequest": RecordingStartRequest,
        "RecordingStartResponse": RecordingStartResponse,
        "RecordingStopRequest": RecordingStopRequest,
        "RecordingStopResponse": RecordingStopResponse,
        "PermissionRequestRequest": PermissionRequestRequest,
        "PermissionRequestResponse": PermissionRequestResponse,
        "ContentSearchRequest": ContentSearchRequest,
        "ContentHit": ContentHit,
        "ContentSearchResponse": ContentSearchResponse,
        "TranscriptSearchRequest": TranscriptSearchRequest,
        "TranscriptHit": TranscriptHit,
        "TranscriptSearchResponse": TranscriptSearchResponse,
        "TimelineQueryRequest": TimelineQueryRequest,
        "TimelineRow": TimelineRow,
        "TimelineQueryResponse": TimelineQueryResponse,
        "TimelineDayRequest": TimelineDayRequest,
        "DayBlockedInterval": DayBlockedInterval,
        "DaySegmentRecording": DaySegmentRecording,
        "TimelineDayResponse": TimelineDayResponse,
        "FrameNearestRequest": FrameNearestRequest,
        "FrameNearestResponse": FrameNearestResponse,
        "AppsListResponse": AppsListResponse,
        "WhoAmIResponse": WhoAmIResponse,
        "BackfillStartRequest": BackfillStartRequest,
        "BackfillCancelRequest": BackfillCancelRequest,
        "BackfillStatusResponse": BackfillStatusResponse,
        "BackfillProgressEvent": BackfillProgressEvent,
        "TasksListRequest": TasksListRequest,
        "TaskSegment": TaskSegment,
        "TasksListResponse": TasksListResponse,
    }
    # `__getattr__` below dispatches every documented model name through
    # `_MODELS`, so injecting them into `globals()` would just shadow that
    # path with a stale reference on reload.
    return _MODELS


def __getattr__(name: str) -> Any:
    if name in _MODEL_NAMES:
        return _load_models()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_MODEL_NAMES])


__all__ = [
    "API_SCHEMA_VERSION",
    "RECORDING_START_API_VERSION",
    "_DAEMON_INFO_API_VERSION",
    "_LIST_API_VERSION",
    "_SNAPSHOT_API_VERSION",
    "_EVENTS_API_VERSION",
    "_RECORDING_START_API_VERSION",
    "_RECORDING_STOP_API_VERSION",
    "_PERMISSION_REQUEST_API_VERSION",
    "_PERMISSION_CLEANUP_API_VERSION",
    "_CONTENT_SEARCH_API_VERSION",
    "_TRANSCRIPT_SEARCH_API_VERSION",
    "_TIMELINE_QUERY_API_VERSION",
    "_FRAME_NEAREST_API_VERSION",
    "_APPS_LIST_API_VERSION",
    "_AUTH_WHOAMI_API_VERSION",
    "_BACKFILL_API_VERSION",
    "_TASKS_LIST_API_VERSION",
    "daemon_version",
    "envelope",
] + sorted(_MODEL_NAMES)
