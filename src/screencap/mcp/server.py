"""FastMCP stdio server exposing ScreenCap's retrieval surface to an agent.

Five tools — ``search_screen_content``, ``search_transcript``,
``query_timeline``, ``list_recordings``, ``whoami`` — each forward to a daemon
``/v0/*`` read verb and re-wrap the response as a typed, POINTER-ONLY result
(text snippets + ``(recording, timestamp)`` pointers; never frame pixels). The
server holds no query logic of its own (R1).

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


class ContentSearchResult(BaseModel):
    hits: list[ContentHit]
    # ok / no_match / not_indexed / index_degraded / store_unavailable — so the
    # agent never mistakes an empty/degraded index for ground truth.
    index_state: str


class TranscriptHit(BaseModel):
    recording: str
    chunk_index: int
    snippet: str


class TranscriptSearchResult(BaseModel):
    hits: list[TranscriptHit]
    coverage: str


class TimelineRow(BaseModel):
    recording: str
    timestamp_ms: int
    app: str | None
    title: str | None


class TimelineResult(BaseModel):
    rows: list[TimelineRow]
    coverage: str


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


class WhoAmIResult(BaseModel):
    """The cloud account currently signed in on the daemon (SCR-148)."""

    signed_in: bool
    uid: str | None = None
    email: str | None = None
    # True when signed in but the token could not be refreshed (offline), so
    # uid/email are unknown.
    stale: bool = False


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
                    raise DaemonError(f"could not reach the ScreenCap daemon: {exc}") from exc
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
    )


async def query_timeline(
    start_ms: int | None = None,
    end_ms: int | None = None,
    app: str | None = None,
    recording: str | None = None,
    limit: int | None = None,
) -> TimelineResult:
    """Query the authoritative app/window/time timeline from event tables."""
    env = await (await _client()).timeline_query(
        start_ms=start_ms, end_ms=end_ms, app=app,
        recording=recording, limit=_clamp_or_none(limit),
    )
    return TimelineResult(
        rows=[TimelineRow(**r) for r in env.get("rows", [])],
        coverage=env.get("coverage", "authoritative"),
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
            "Search a local ScreenCap screen-recording memory. Returns text "
            "snippets + (recording, timestamp) pointers, never frame images. "
            "On-screen content recall is best-effort (action-gated frames, OCR "
            "and redaction limits); the timeline is authoritative."
        ),
        lifespan=_lifespan,
    )
    for fn in (
        search_screen_content, search_transcript, query_timeline,
        list_recordings, whoami,
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
