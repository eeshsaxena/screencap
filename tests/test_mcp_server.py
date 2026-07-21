"""Tests for the SCR-118 U5 ``screencap mcp`` thin stdio server.

The tool functions are exercised directly (they are module-level), against both
a stub client (mapping / clamp / error / no-match) and a real in-process daemon
over ASGITransport (round-trip + held liveness subscription). The stdout
discipline test runs the REAL ``mcp`` import so dependency-side stdout noise is
caught.
"""

from __future__ import annotations

import asyncio
import contextlib
import io

import httpx
import pytest

from screencap.mcp import server
from screencap.mcp._client import AsyncDaemonClient, DaemonError, LivenessSubscription

# The MCP surface is the agent-facing recall/search contract (pointer-only hits, the
# frame.nearest `encrypted` steer, the frame.read decrypt seam). Mark privacy so these
# guards actually RUN on CI's privacy lanes — otherwise a response-shape/registration
# drift (as happened for `encrypted` + `read_frame`) ships green because unrun.
pytestmark = pytest.mark.privacy

# --------------------------------------------------------------------------
# Stub client (mapping / clamp / error / no-match)
# --------------------------------------------------------------------------


class _StubClient:
    def __init__(self, responses: dict | None = None, *, raises: Exception | None = None):
        self.responses = responses or {}
        self.raises = raises
        self.calls: list[tuple] = []

    async def _reply(self, kind, *args):
        self.calls.append((kind, *args))
        if self.raises is not None:
            raise self.raises
        return self.responses[kind]

    async def content_search(self, query, *, recording=None, limit=None):
        return await self._reply("content", query, recording, limit)

    async def diary_search(self, query, *, recording=None, limit=None):
        return await self._reply("diary", query, recording, limit)

    async def transcript_search(self, query, *, recording=None, limit=None):
        return await self._reply("transcript", query, recording, limit)

    async def timeline_query(self, *, start_ms=None, end_ms=None, app=None, recording=None, limit=None):
        return await self._reply("timeline", start_ms, end_ms, app, recording, limit)

    async def frame_nearest(self, recording, timestamp_ms, *, staleness_cap_ms=None):
        return await self._reply("frame", recording, timestamp_ms, staleness_cap_ms)

    async def frame_read(self, recording, stem):
        return await self._reply("frame_bytes", recording, stem)

    async def list_recordings(self):
        return await self._reply("recordings")

    async def whoami(self):
        return await self._reply("whoami")

    async def timeline_day(self, *, date=None, tz_offset_seconds=None):
        return await self._reply("timeline_day", date, tz_offset_seconds)

    async def tasks_query(self, *, start_date=None, end_date=None, tz_offset_seconds=None):
        return await self._reply("tasks_query", start_date, end_date, tz_offset_seconds)


def _use_client(monkeypatch, client) -> None:
    async def _fake_client():
        return client

    monkeypatch.setattr(server, "_client", _fake_client)


@pytest.mark.asyncio
async def test_content_tool_maps_pointer_only_hits(monkeypatch):
    stub = _StubClient({
        "content": {
            "ok": True,
            "hits": [{"recording": "demo", "timestamp_ms": 125000, "snippet": "invoice", "score": -1.2}],
            "index_state": "ok",
        }
    })
    _use_client(monkeypatch, stub)

    result = await server.search_screen_content("invoice")

    assert isinstance(result, server.ContentSearchResult)
    assert result.index_state == "ok"
    assert result.hits[0].recording == "demo"
    assert result.hits[0].timestamp_ms == 125000
    # Pointer-only: the model has no path/bytes field by construction.
    assert set(server.ContentHit.model_fields) == {"recording", "timestamp_ms", "snippet", "score"}


@pytest.mark.asyncio
async def test_diary_tool_maps_pointer_only_block_hits(monkeypatch):
    """The read-only diary mirror maps ``(recording, block_id, span, snippet)``
    pointer hits — no path/bytes/full-bullet field on the model (day-diary U6)."""
    stub = _StubClient({
        "diary": {
            "ok": True,
            "hits": [{
                "recording": "2026-06-01_0900",
                "block_id": "blk-morning",
                "start_ms": 10_000,
                "end_ms": 120_000,
                "snippet": "spinning back kick",
                "score": -1.4,
            }],
            "index_state": "ok",
        }
    })
    _use_client(monkeypatch, stub)

    result = await server.search_diary("spinning")

    assert isinstance(result, server.DiarySearchResult)
    assert result.index_state == "ok"
    hit = result.hits[0]
    assert hit.recording == "2026-06-01_0900" and hit.block_id == "blk-morning"
    assert hit.start_ms == 10_000 and hit.end_ms == 120_000
    # Pointer-only by construction: no path / bytes / full-bullet field.
    assert set(server.DiaryBlockHit.model_fields) == {
        "recording", "block_id", "start_ms", "end_ms", "snippet", "score",
    }
    # The tool forwards to the diary verb (not content).
    assert stub.calls[0][0] == "diary"


@pytest.mark.asyncio
async def test_transcript_and_timeline_tools_map(monkeypatch):
    stub = _StubClient({
        "transcript": {"ok": True, "hits": [{"recording": "d", "chunk_index": 3, "snippet": "budget"}], "coverage": "best_effort"},
        "timeline": {"ok": True, "rows": [{"recording": "d", "timestamp_ms": 1, "app": "Safari", "title": "T"}], "coverage": "authoritative"},
    })
    _use_client(monkeypatch, stub)

    tr = await server.search_transcript("budget")
    assert tr.coverage == "best_effort"
    assert tr.hits[0].chunk_index == 3

    tl = await server.query_timeline(app="safari")
    assert tl.coverage == "authoritative"
    assert tl.rows[0].app == "Safari"


@pytest.mark.asyncio
async def test_transcript_tool_surfaces_chunk_timing(monkeypatch):
    # SCR-186: the new nullable chunk-timing fields reach the agent-facing model.
    stub = _StubClient({
        "transcript": {
            "ok": True,
            "hits": [{
                "recording": "d", "chunk_index": 0, "snippet": "budget",
                "timestamp_ms": 1_719_400_000_000,
                "timestamp_granularity": "chunk",
                "chunk_duration_ms": 900_000,
            }],
            "coverage": "best_effort",
        },
    })
    _use_client(monkeypatch, stub)

    tr = await server.search_transcript("budget")
    hit = tr.hits[0]
    assert hit.timestamp_ms == 1_719_400_000_000
    assert hit.timestamp_granularity == "chunk"
    assert hit.chunk_duration_ms == 900_000


@pytest.mark.asyncio
async def test_resolve_frame_maps_pointer(monkeypatch):
    stub = _StubClient({
        "frame": {"ok": True, "stem": "1719400010.000000", "delta_ms": -1000},
    })
    _use_client(monkeypatch, stub)

    result = await server.resolve_frame("demo", 1_719_400_011_000)
    assert isinstance(result, server.FrameNearest)
    assert result.stem == "1719400010.000000"
    assert result.delta_ms == -1000
    assert result.encrypted is False  # unencrypted corpus -> read the .jpg directly
    assert result.store_state == "mounted"  # SCR-258: carries store_state like its siblings
    # Pointer-only by construction: a stem + delta + the encrypted steer + the
    # store_state metadata, no path/bytes field.
    assert set(server.FrameNearest.model_fields) == {
        "stem",
        "delta_ms",
        "encrypted",
        "store_state",
    }


@pytest.mark.asyncio
async def test_resolve_frame_miss_is_null(monkeypatch):
    stub = _StubClient({"frame": {"ok": True, "stem": None, "delta_ms": None}})
    _use_client(monkeypatch, stub)

    result = await server.resolve_frame("demo", 0, staleness_cap_ms=900_000)
    assert result.stem is None and result.delta_ms is None
    # The chunk-scaled cap is forwarded to the daemon verb.
    _, _rec, _ts, forwarded_cap = stub.calls[0]
    assert forwarded_cap == 900_000


@pytest.mark.asyncio
async def test_read_frame_maps_bytes(monkeypatch):
    stub = _StubClient({
        "frame_bytes": {
            "ok": True, "image_base64": "ZmFrZS1qcGVn", "content_type": "image/jpeg",
        },
    })
    _use_client(monkeypatch, stub)

    result = await server.read_frame("demo", "1719400010.000000")
    assert isinstance(result, server.FrameBytes)
    assert result.image_base64 == "ZmFrZS1qcGVn"
    assert result.content_type == "image/jpeg"
    # The stem is forwarded verbatim to the daemon verb.
    assert stub.calls[0] == ("frame_bytes", "demo", "1719400010.000000")


@pytest.mark.asyncio
async def test_read_frame_refusal_is_null(monkeypatch):
    # Fail-closed: missing / blocked / unscrubbed-chunk collapses to a null image.
    stub = _StubClient({"frame_bytes": {"ok": True, "image_base64": None, "content_type": None}})
    _use_client(monkeypatch, stub)

    result = await server.read_frame("demo", "1719400010.000000")
    assert result.image_base64 is None
    assert result.content_type is None


@pytest.mark.asyncio
async def test_resolve_frame_is_registered():
    mcp = server.build_server()
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "search_screen_content", "search_diary", "search_transcript",
        "query_timeline", "resolve_frame", "read_frame", "list_recordings",
        "whoami", "chat_answer",
        # U13 (R18) day-first browse tools — NO delete-shaped tool (human-only).
        "browse_day", "query_tasks", "create_clip",
    }


@pytest.mark.asyncio
async def test_no_thread_or_block_mutation_tool_registered():
    """U7 Scope Boundary / SECURITY.md R18: thread & block curation is human-only —
    NO create/rename/merge/split/delete-shaped thread/block tool is ever forwarded
    over MCP. The read/browse surface is the whole tool set (pinned above)."""
    mcp = server.build_server()
    names = {t.name for t in await mcp.list_tools()}
    forbidden = {
        "merge_tasks", "split_task", "rename_task", "update_task", "delete_task",
        "create_task", "curate_task", "edit_task",
        "merge_thread", "split_thread", "link_thread", "curate_thread",
        "merge_block", "split_block", "rename_block", "edit_block", "delete_block",
    }
    assert names.isdisjoint(forbidden), names
    # The only mutating tool is clip creation (R18-attributed, no delete counterpart).
    assert {n for n in names if "clip" in n} == {"create_clip"}


@pytest.mark.asyncio
async def test_query_tasks_mirrors_diary_and_rollup_fields(monkeypatch):
    """The cross-day ``query_tasks`` tool mirrors the day-diary fields + thread
    rollup READ-ONLY so an agent sees exactly what the app sees (U7/KTD-9)."""
    stub = _StubClient({
        "tasks_query": {
            "ok": True, "start_date": "2026-06-01", "end_date": "2026-06-01",
            "days": [{"date": "2026-06-01", "tasks": [{
                "recording": "2026-06-01_0900", "recording_id": "id0", "task_index": 0,
                "start_ts": 1.0, "end_ts": 61.0, "name": "Kata drills",
                "bullets": ["spinning back kick"], "block_id": "blk-morning",
                "thread_id": "thr-kata", "is_open": False,
                "thread_total_minutes": 60.0, "thread_sitting_count": 2,
                "thread_sitting_index": 1,
            }]}],
            "recordings": [], "store_state": "mounted",
        }
    })
    _use_client(monkeypatch, stub)

    result = await server.query_tasks("2026-06-01", "2026-06-01")

    hit = result.days[0].tasks[0]
    assert hit.block_id == "blk-morning" and hit.thread_id == "thr-kata"
    assert hit.bullets == ["spinning back kick"] and hit.is_open is False
    assert hit.thread_total_minutes == 60.0 and hit.thread_sitting_count == 2
    assert hit.thread_sitting_index == 1
    # It forwarded to the tasks.query verb (not a mutation).
    assert stub.calls[0][0] == "tasks_query"


@pytest.mark.asyncio
async def test_browse_day_mirrors_diary_block_fields(monkeypatch):
    """The ``browse_day`` tool mirrors a block's diary fields READ-ONLY. Rollups are
    null here (the day band does not compute them — use query_tasks)."""
    stub = _StubClient({
        "timeline_day": {
            "ok": True, "date": "2026-06-01",
            "recordings": [{
                "name": "2026-06-01_0900", "recording_id": "id0", "state": "done",
                "start_ms": 0, "end_ms": 1000,
                "tasks": [{
                    "task_index": 0, "start_ts": 1.0, "end_ts": 61.0, "name": "Kata",
                    "bullets": ["kick"], "block_id": "blk-a", "thread_id": "thr-k",
                    "is_open": True,
                }],
            }],
        }
    })
    _use_client(monkeypatch, stub)

    result = await server.browse_day("2026-06-01")

    task = result.recordings[0].tasks[0]
    assert task.block_id == "blk-a" and task.thread_id == "thr-k"
    assert task.bullets == ["kick"] and task.is_open is True
    # Rollup fields present on the model but null (not computed by the day band).
    assert task.thread_total_minutes is None and task.thread_sitting_count is None


@pytest.mark.asyncio
async def test_tool_limit_is_clamped(monkeypatch):
    stub = _StubClient({"content": {"ok": True, "hits": [], "index_state": "no_match"}})
    _use_client(monkeypatch, stub)

    await server.search_screen_content("x", limit=10_000)

    # The forwarded limit was clamped to the tool-layer max.
    _, _query, _rec, forwarded_limit = stub.calls[0]
    assert forwarded_limit == server._MAX_TOOL_LIMIT


@pytest.mark.asyncio
async def test_no_matches_returns_empty_not_error(monkeypatch):
    stub = _StubClient({"content": {"ok": True, "hits": [], "index_state": "no_match"}})
    _use_client(monkeypatch, stub)

    result = await server.search_screen_content("nothing")
    assert result.hits == []
    assert result.index_state == "no_match"


@pytest.mark.asyncio
async def test_daemon_unreachable_raises_clean_error(monkeypatch):
    stub = _StubClient(raises=DaemonError("daemon unreachable"))
    _use_client(monkeypatch, stub)

    with pytest.raises(DaemonError):
        await server.search_screen_content("x")


@pytest.mark.asyncio
async def test_list_recordings_projects_known_fields_only(monkeypatch):
    """The tool projects each recording dict onto RecordingSummary's known
    fields — extra daemon fields are stripped, and the known ones map through."""
    stub = _StubClient({
        "recordings": {
            "ok": True,
            "recordings": [
                {
                    "name": "demo",
                    "date": "2026-06-11",
                    "duration": "00:05",
                    "has_audio": True,
                    "transcribed": False,
                    "uploaded": True,
                    # Extra fields the daemon may add — must be dropped, not raise.
                    "size_bytes": 12345,
                    "path": "/Users/x/.screencap/recordings/demo",
                    "internal_flag": True,
                },
            ],
        }
    })
    _use_client(monkeypatch, stub)

    result = await server.list_recordings()

    assert isinstance(result, server.RecordingsResult)
    assert len(result.recordings) == 1
    rec = result.recordings[0]
    # Known fields mapped correctly.
    assert rec.name == "demo"
    assert rec.date == "2026-06-11"
    assert rec.duration == "00:05"
    assert rec.has_audio is True
    assert rec.transcribed is False
    assert rec.uploaded is True
    # Extra fields stripped — the model carries only its declared fields, so a
    # leaked path can't ride through.
    dumped = rec.model_dump()
    assert set(dumped) == set(server.RecordingSummary.model_fields)
    assert "path" not in dumped
    assert "size_bytes" not in dumped


@pytest.mark.asyncio
async def test_list_recordings_surfaces_account_mismatch_fields(monkeypatch):
    """SCR-148: owner_uid + upload_warning ride through to RecordingSummary."""
    stub = _StubClient({
        "recordings": {
            "ok": True,
            "recordings": [
                {
                    "name": "demo", "date": "2026-06-11", "duration": "00:05",
                    "has_audio": False, "transcribed": False, "uploaded": False,
                    "owner_uid": "uid-abc",
                    "upload_warning": "uploads disabled: offline",
                },
            ],
        }
    })
    _use_client(monkeypatch, stub)

    result = await server.list_recordings()
    rec = result.recordings[0]
    assert rec.owner_uid == "uid-abc"
    assert rec.upload_warning == "uploads disabled: offline"


@pytest.mark.asyncio
async def test_list_recordings_account_fields_default_none(monkeypatch):
    """A daemon that omits the SCR-148 fields decodes them as None, not an error."""
    stub = _StubClient({
        "recordings": {
            "ok": True,
            "recordings": [
                {
                    "name": "legacy", "date": "2026-06-11", "duration": "00:05",
                    "has_audio": False, "transcribed": False, "uploaded": True,
                },
            ],
        }
    })
    _use_client(monkeypatch, stub)

    rec = (await server.list_recordings()).recordings[0]
    assert rec.owner_uid is None
    assert rec.upload_warning is None


@pytest.mark.asyncio
async def test_whoami_tool_reports_signed_in_identity(monkeypatch):
    """SCR-148: the whoami tool forwards the daemon's signed-in uid/email."""
    stub = _StubClient({
        "whoami": {
            "ok": True, "signed_in": True, "uid": "uid-xyz",
            "email": "a@b.com", "stale": False,
        }
    })
    _use_client(monkeypatch, stub)

    result = await server.whoami()
    assert isinstance(result, server.WhoAmIResult)
    assert result.signed_in is True
    assert result.uid == "uid-xyz"
    assert result.email == "a@b.com"
    assert result.stale is False


@pytest.mark.asyncio
async def test_whoami_tool_reports_signed_out(monkeypatch):
    """Signed-out forwards signed_in=false with null identity."""
    stub = _StubClient({"whoami": {"ok": True, "signed_in": False}})
    _use_client(monkeypatch, stub)

    result = await server.whoami()
    assert result.signed_in is False
    assert result.uid is None
    assert result.email is None


@pytest.mark.asyncio
async def test_whoami_tool_reports_stale_offline(monkeypatch):
    """Stale/offline shape: signed_in=True, uid=None, email=None, stale=True.

    This shape is produced when auth is cached but cannot be verified (offline).
    The MCP tool must surface it intact so the agent knows the account is
    temporarily UNVERIFIABLE, not signed-out.
    """
    stub = _StubClient({
        "whoami": {
            "ok": True, "signed_in": True, "uid": None, "email": None, "stale": True,
        }
    })
    _use_client(monkeypatch, stub)

    result = await server.whoami()
    assert isinstance(result, server.WhoAmIResult)
    assert result.signed_in is True
    assert result.uid is None
    assert result.email is None
    assert result.stale is True


# --------------------------------------------------------------------------
# Real in-process daemon round-trip (ASGITransport)
# --------------------------------------------------------------------------


def _test_daemon_client() -> AsyncDaemonClient:
    from screencap.daemon.app import build_app

    return AsyncDaemonClient(transport=httpx.ASGITransport(app=build_app()))


@pytest.mark.asyncio
async def test_content_tool_round_trips_through_daemon(monkeypatch, tmp_path):
    import screencap.content_index as content_index

    store_path = tmp_path / "content_index.db"
    monkeypatch.setattr(content_index, "default_index_path", lambda: store_path)
    with content_index.ContentIndex(store_path) as store:
        store.write_frames(
            "demo", [content_index.IndexFrame(timestamp_ms=1000, text="the invoice total")]
        )

    client = _test_daemon_client()
    _use_client(monkeypatch, client)
    try:
        result = await server.search_screen_content("invoice")
    finally:
        await client.aclose()

    assert result.index_state == "ok"
    assert result.hits[0].recording == "demo"
    assert "invoice" in result.hits[0].snippet.lower()


@pytest.mark.asyncio
async def test_resolve_frame_round_trips_through_daemon(monkeypatch, tmp_path):
    # Seed a recording with two screenshots; the nearer one is blocked, so the
    # real verb (ALLOW filter included) must resolve to the farther ALLOW frame.
    import screencap.frame_blocked as fb

    recordings_dir = tmp_path / "recordings"
    screenshots = recordings_dir / "demo" / "screenshots"
    screenshots.mkdir(parents=True)
    t0 = 1_719_400_000.0
    (screenshots / f"{t0:.6f}.jpg").write_bytes(b"")
    (screenshots / f"{t0 + 10.0:.6f}.jpg").write_bytes(b"")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    monkeypatch.setattr(
        fb, "build_is_blocked",
        lambda rec_dir, tss: (lambda ts: ts == t0 + 10.0),
    )

    client = _test_daemon_client()
    _use_client(monkeypatch, client)
    try:
        result = await server.resolve_frame("demo", int((t0 + 11.0) * 1000))
    finally:
        await client.aclose()

    assert result.stem == "1719400000.000000"  # the ALLOW frame, not the blocked nearer one


async def _events_app(scope, receive, send):
    """Minimal ASGI app that emits one `subscribed` NDJSON frame then ends.

    A finite stream avoids the in-process ASGITransport deadlock that closing an
    *infinite* stream causes (production uses a real UDS transport where aclose
    closes the socket promptly). This exercises that open() consumes the
    subscribed frame and aclose() cleans up without hanging.
    """
    assert scope["type"] == "http"
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/x-ndjson")],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"type": "subscribed", "cursor": 0}\n',
        "more_body": True,
    })
    await send({"type": "http.response.body", "body": b"", "more_body": False})


@pytest.mark.asyncio
async def test_liveness_subscription_opens_and_closes():
    client = AsyncDaemonClient(transport=httpx.ASGITransport(app=_events_app))
    sub = LivenessSubscription(client.raw)
    try:
        # open() must consume the `subscribed` frame without raising; the whole
        # cycle is timeout-guarded so a regression hangs the test loudly.
        await asyncio.wait_for(sub.open(), timeout=5.0)
    finally:
        await asyncio.wait_for(sub.aclose(), timeout=5.0)
        await client.aclose()


# --------------------------------------------------------------------------
# stdout discipline (real mcp import)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_writes_nothing_to_stdout(monkeypatch):
    stub = _StubClient({"content": {"ok": True, "hits": [], "index_state": "no_match"}})
    _use_client(monkeypatch, stub)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        # Building the FastMCP app (real SDK) + a tool call must not touch stdout
        # — the stdio transport owns it for the JSON-RPC stream.
        server.build_server()
        await server.search_screen_content("x")

    assert buf.getvalue() == ""


def test_mcp_command_registered():
    from screencap.cli import cli

    assert "mcp" in cli.commands
