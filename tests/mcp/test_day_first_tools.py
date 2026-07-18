"""Tests for the U13 day-first MCP tools (R18) — ``browse_day`` / ``query_tasks``
/ ``create_clip``.

Each is a THIN forward to a daemon verb the UI already consumes
(``/v0/timeline.day`` / ``/v0/tasks.query`` / ``/v0/clip.create``, KTD-2), re-wrapped
as a typed, POINTER-ONLY result. Recording identifiers are opaque plumbing; day +
time is the user-facing vocabulary (KTD-1). R18: there is NO range-delete and NO
clip-delete tool — the human-only guarantee is a tool-surface control.

Vision-free + privacy-marked so these guards actually run on the CI privacy lane.
Two seams are exercised:
  * the stub-client seam (mapping / provenance passthrough / creator + honesty
    flag / KTD-11 day grouping), mirroring ``tests/test_mcp_server.py``; and
  * a real in-process daemon (ASGITransport) for ``create_clip`` writing through the
    same catalog the U10 ``clip.list`` reads.
"""

from __future__ import annotations

import contextlib
import inspect

import httpx
import pytest

from screencap.mcp import server
from screencap.mcp._client import AsyncDaemonClient, DaemonError

pytestmark = pytest.mark.privacy


# --------------------------------------------------------------------------
# Stub client (mirrors tests/test_mcp_server.py::_StubClient shape)
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

    async def timeline_day(self, *, date, tz_offset_seconds=0):
        return await self._reply("day", date, tz_offset_seconds)

    async def tasks_query(self, *, start_date, end_date, tz_offset_seconds=0):
        return await self._reply("tasks", start_date, end_date, tz_offset_seconds)

    async def clip_create(self, *, recording, start_ms, end_ms, tz_offset_seconds=0, creator="mcp"):
        return await self._reply("clip", recording, start_ms, end_ms, tz_offset_seconds, creator)


def _use_client(monkeypatch, client) -> None:
    async def _fake_client():
        return client

    monkeypatch.setattr(server, "_client", _fake_client)


# ==========================================================================
# browse_day -> /v0/timeline.day
# ==========================================================================


def _day_envelope() -> dict:
    return {
        "ok": True,
        "date": "2026-07-18",
        "store_mounted": True,
        "coverage_complete": True,
        "recordings": [
            {
                "name": "rec-a",
                "recording_id": "rid-a",
                "state": "done",
                "start_ms": 1_000_000,
                "end_ms": 2_000_000,
                # gap provenance, split honestly (R7):
                "blocked_proven": [{"start_ms": 1_100_000, "end_ms": 1_200_000}],
                "unverifiable": [{"start_ms": 1_300_000, "end_ms": 1_350_000}],
                "deleted": [{"start_ms": 1_500_000, "end_ms": 1_600_000}],
                "purged": [
                    {
                        "start_ms": 1_700_000,
                        "end_ms": 1_750_000,
                        "bundle_id": "com.example.app",
                        "app_name": "Example",
                        "root_domain": None,
                    }
                ],
                "tasks": [
                    {"task_index": 0, "start_ts": 1000.0, "end_ts": 1400.0, "name": "wrote spec"},
                ],
                "end_status": "clean",
            }
        ],
    }


@pytest.mark.asyncio
async def test_browse_day_forwards_and_maps(monkeypatch):
    stub = _StubClient({"day": _day_envelope()})
    _use_client(monkeypatch, stub)

    result = await server.browse_day("2026-07-18", tz_offset_seconds=-25200)

    assert isinstance(result, server.DayResult)
    # Forwarded to the timeline.day verb with the day + tz.
    assert stub.calls[0] == ("day", "2026-07-18", -25200)
    assert result.date == "2026-07-18"
    assert result.store_mounted is True
    assert result.coverage_complete is True
    rec = result.recordings[0]
    # Pointer vocabulary: the recording key is exposed as ``recording`` (opaque
    # plumbing) — day + time is the user-facing surface (KTD-1).
    assert rec.recording == "rec-a"
    assert rec.start_ms == 1_000_000 and rec.end_ms == 2_000_000
    assert rec.tasks[0].name == "wrote spec"


@pytest.mark.asyncio
async def test_browse_day_carries_gap_provenance(monkeypatch):
    """The honest provenance split (R7) survives the forward: proven masking,
    unverifiable gaps, removed-by-you, and removed-by-your-rules stay distinct."""
    stub = _StubClient({"day": _day_envelope()})
    _use_client(monkeypatch, stub)

    rec = (await server.browse_day("2026-07-18")).recordings[0]

    assert [(i.start_ms, i.end_ms) for i in rec.blocked_proven] == [(1_100_000, 1_200_000)]
    assert [(i.start_ms, i.end_ms) for i in rec.unverifiable] == [(1_300_000, 1_350_000)]
    assert [(i.start_ms, i.end_ms) for i in rec.deleted] == [(1_500_000, 1_600_000)]
    assert [(p.start_ms, p.end_ms) for p in rec.purged] == [(1_700_000, 1_750_000)]
    # The purge carries the disable-target identity, distinct from a proven block.
    assert rec.purged[0].bundle_id == "com.example.app"
    assert rec.purged[0].app_name == "Example"


@pytest.mark.asyncio
async def test_browse_day_older_shape_defaults(monkeypatch):
    """A pre-provenance day shape (no blocked/deleted/purged keys) decodes to empty
    lists, not an error."""
    env = {
        "ok": True,
        "date": "2026-07-18",
        "recordings": [
            {"name": "rec-a", "state": "done", "start_ms": 1, "end_ms": 2,
             "blocked_proven": [], "unverifiable": []},
        ],
    }
    stub = _StubClient({"day": env})
    _use_client(monkeypatch, stub)

    result = await server.browse_day("2026-07-18")
    rec = result.recordings[0]
    assert rec.deleted == [] and rec.purged == [] and rec.tasks == []
    # Absent envelope flags default to the plaintext-install / complete posture.
    assert result.store_mounted is True and result.coverage_complete is True


# ==========================================================================
# query_tasks -> /v0/tasks.query
# ==========================================================================


def _tasks_envelope() -> dict:
    # KTD-11 midnight case: a task starting just after local midnight groups under
    # the NEW day, its predecessor under the prior day — the daemon owns the single
    # day-mapping rule; the tool must preserve that grouping (never re-bucket).
    return {
        "ok": True,
        "start_date": "2026-07-17",
        "end_date": "2026-07-18",
        "days": [
            {
                "date": "2026-07-18",
                "tasks": [
                    {"recording": "rec-b", "recording_id": "rid-b", "task_index": 0,
                     "start_ts": 1_752_800_460.0, "end_ts": 1_752_800_900.0, "name": "after midnight"},
                ],
            },
            {
                "date": "2026-07-17",
                "tasks": [
                    {"recording": "rec-a", "recording_id": "rid-a", "task_index": 3,
                     "start_ts": 1_752_799_900.0, "end_ts": 1_752_800_100.0, "name": "before midnight"},
                ],
            },
        ],
        "recordings": [
            {"name": "rec-a", "recording_id": "rid-a", "state": "produced_tasks"},
            {"name": "rec-c", "recording_id": "rid-c", "state": "nothing_to_name",
             "reason": "idle", "detail": "no work detected"},
        ],
        "store_state": "mounted",
    }


@pytest.mark.asyncio
async def test_query_tasks_forwards_and_maps(monkeypatch):
    stub = _StubClient({"tasks": _tasks_envelope()})
    _use_client(monkeypatch, stub)

    result = await server.query_tasks("2026-07-17", "2026-07-18", tz_offset_seconds=-25200)

    assert isinstance(result, server.TasksResult)
    assert stub.calls[0] == ("tasks", "2026-07-17", "2026-07-18", -25200)
    assert result.start_date == "2026-07-17"
    assert result.end_date == "2026-07-18"


@pytest.mark.asyncio
async def test_query_tasks_preserves_ktd11_day_grouping(monkeypatch):
    """The tool must forward the daemon's day grouping VERBATIM (KTD-11 midnight):
    the after-midnight task stays under 2026-07-18, its predecessor under 2026-07-17
    — the tool never re-buckets by a rule of its own."""
    stub = _StubClient({"tasks": _tasks_envelope()})
    _use_client(monkeypatch, stub)

    result = await server.query_tasks("2026-07-17", "2026-07-18")

    by_day = {d.date: d for d in result.days}
    assert by_day["2026-07-18"].tasks[0].name == "after midnight"
    assert by_day["2026-07-18"].tasks[0].recording == "rec-b"
    assert by_day["2026-07-17"].tasks[0].name == "before midnight"
    # Honest zero-state rollup rides alongside (R21): a recording that named
    # nothing is still listed, so "nothing on file" ≠ "you did nothing".
    statuses = {r.recording: r.state for r in result.recordings}
    assert statuses["rec-a"] == "produced_tasks"
    assert statuses["rec-c"] == "nothing_to_name"


# ==========================================================================
# create_clip -> /v0/clip.create
# ==========================================================================


def _clip_envelope(ok=True, reason=None) -> dict:
    clip = None
    if ok:
        clip = {
            "id": "clip123",
            "source_recording": "rec-a",
            "source_day": "2026-07-18",
            "start_ms": 1_000_000,
            "end_ms": 1_050_000,
            "created_at": 1_752_800_000.0,
            "creator": "mcp",
            "honesty_flags": {"clip_video_capture_blocked_only": True},
            "path": "/Users/x/.screencap/recordings/.clips/clip123.mp4",
        }
    return {"ok": ok, "reason": reason, "clip": clip, "store_state": "mounted"}


@pytest.mark.asyncio
async def test_create_clip_forwards_creator_mcp_and_surfaces_honesty_flag(monkeypatch):
    stub = _StubClient({"clip": _clip_envelope()})
    _use_client(monkeypatch, stub)

    result = await server.create_clip("rec-a", 1_000_000, 1_050_000, tz_offset_seconds=-25200)

    assert isinstance(result, server.ClipResult)
    # R18: the agent path is attributable — creator="mcp" is forwarded, never "ui".
    _, rec, s_ms, e_ms, tz, creator = stub.calls[0]
    assert rec == "rec-a" and s_ms == 1_000_000 and e_ms == 1_050_000
    assert tz == -25200
    assert creator == "mcp"
    # The honesty signal survives the forward (capture-blocked, not post-hoc masked).
    assert result.ok is True
    assert result.clip_id == "clip123"
    assert result.creator == "mcp"
    assert result.clip_video_capture_blocked_only is True
    assert result.honesty_flags["clip_video_capture_blocked_only"] is True


@pytest.mark.asyncio
async def test_create_clip_failure_reason_surfaces(monkeypatch):
    """A clip-domain failure (fail-closed over a policy-purged range, R17) rides the
    result as ok=False + reason, never an exception — mirroring the daemon envelope."""
    stub = _StubClient({"clip": _clip_envelope(ok=False, reason="policy_purged")})
    _use_client(monkeypatch, stub)

    result = await server.create_clip("rec-a", 1_000_000, 1_050_000)
    assert result.ok is False
    assert result.reason == "policy_purged"
    assert result.clip_id is None
    assert result.clip_video_capture_blocked_only is False


@pytest.mark.asyncio
async def test_create_clip_daemon_unreachable_raises_clean_error(monkeypatch):
    stub = _StubClient(raises=DaemonError("daemon unreachable"))
    _use_client(monkeypatch, stub)
    with pytest.raises(DaemonError):
        await server.create_clip("rec-a", 1, 2)


# ==========================================================================
# create_clip writes through the same catalog clip.list reads (real daemon)
# ==========================================================================


@contextlib.asynccontextmanager
async def _daemon_client(monkeypatch):
    from screencap.daemon.app import build_app

    client = AsyncDaemonClient(transport=httpx.ASGITransport(app=build_app()))

    async def _fake():
        return client

    monkeypatch.setattr(server, "_client", _fake)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_create_clip_writes_through_catalog(monkeypatch, tmp_path):
    """The MCP create_clip write lands in the SAME ``.clips`` catalog the U10
    clip.list read discloses — with creator='mcp' persisted (R18 attribution)."""
    from screencap import clips
    from tests.test_clips import _fake_export
    from tests.test_range_delete import _chunk_bounds, _make_recording, _ms

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings))
    monkeypatch.setattr(clips, "_export_clip", _fake_export)  # Vision/PyAV-free
    _make_recording(recordings, n_chunks=2)
    cs, _ = _chunk_bounds(0)

    async with _daemon_client(monkeypatch):
        created = await server.create_clip("rec", _ms(cs + 5), _ms(cs + 50))
    assert created.ok is True
    assert created.creator == "mcp"

    # The write is visible through the U10 catalog reader (same store).
    listed = clips.list_clips(recordings_dir=recordings)
    assert [c["id"] for c in listed] == [created.clip_id]
    assert listed[0]["creator"] == "mcp"


# ==========================================================================
# Registration + human-only tool-surface guard (R18)
# ==========================================================================


@pytest.mark.asyncio
async def test_new_tools_registered():
    mcp = server.build_server()
    names = {t.name for t in await mcp.list_tools()}
    assert {"browse_day", "query_tasks", "create_clip"} <= names


@pytest.mark.asyncio
async def test_no_delete_shaped_tool_is_exposed():
    """R18 — range deletion and clip deletion are HUMAN-ONLY forever: the guarantee
    is enforced at the tool surface. No registered tool is delete-shaped, and none of
    the three new tools accepts a delete/remove/purge-shaped parameter."""
    mcp = server.build_server()
    names = {t.name for t in await mcp.list_tools()}
    for banned in ("delete", "remove", "purge", "destroy", "evict"):
        assert not any(banned in n.lower() for n in names), f"delete-shaped tool {banned!r}"

    for fn in (server.browse_day, server.query_tasks, server.create_clip):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"delete", "remove", "purge", "destroy", "confirm_delete"})
