"""AE5 — deletion parity across the MCP tool surface (R18, R10).

After an irreversible range delete, EVERY agent-facing MCP tool must return
nothing in the deleted window: content + transcript hits absent, timeline rows
absent, frame resolution fails closed, the day surface marks the window removed
(gap provenance, never live footage), the cross-day task list drops the purged
task, a clip cannot be cut over deleted footage, and the Chat evidence bundle
carries no moment from it. This is the tool-surface half of the human-only
deletion guarantee: the agent can never read back what a human deleted.

The delete is driven by the ``range_delete`` core (the same machinery the
``/v0/delete.*`` job wraps); every assertion then runs THROUGH the MCP tool
functions against a real in-process daemon (ASGITransport), so this exercises the
MCP -> daemon -> data path end to end. Each stream is seeded IN-range first and a
baseline is asserted, so no "absent after delete" assertion can pass vacuously; a
sibling out-of-range chunk survives every tool, proving the delete was surgical,
not total.

Vision-free (raw sqlite + seeded content index, no OCR/PyAV) + privacy-marked so
it runs on the CI privacy lane.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest

from screencap.mcp import server
from screencap.mcp._client import AsyncDaemonClient

# The realistic per-chunk recording fixture (ledger + chunk media/manifests +
# window/action/screenshot rows + per-chunk & bare transcripts).
from tests.test_range_delete import _chunk_bounds, _make_recording, _ms

pytestmark = pytest.mark.privacy


# The MIDDLE chunk (1 of 3) is the DELETE target; chunk 0 is the surviving
# out-of-range sibling. A middle chunk keeps the recording's day-clamped span
# straddling the deleted extent, so ``timeline.day`` renders the deleted stretch
# as an in-span "removed by you" gap (deleting the LAST chunk would just shrink the
# span, dropping the tail off the timeline instead of marking it removed).
_N_CHUNKS = 3
_DELETED_IDX = 1
_KEPT_IDX = 0

_DEL_TOKEN = "chunkspeech0001xyzzy"   # transcript token seeded in chunk 1 (deleted)
_KEPT_TOKEN = "chunkspeech0000xyzzy"  # transcript token seeded in chunk 0 (kept)
_DEL_OCR = "alphauniquetokenone"      # content-index OCR token in chunk 1 (deleted)
_KEPT_OCR = "betauniquetokentwo"      # content-index OCR token in chunk 0 (kept)


def _mid_ms(idx: int) -> int:
    cs, _ = _chunk_bounds(idx)
    return _ms(cs + 10.0)  # the fixture places the frame/action at chunk_start + 10s


@contextlib.asynccontextmanager
async def _daemon_at(monkeypatch, recordings, index_path):
    """Wire the MCP server's ``_client`` at a real in-process daemon over the
    isolated recordings dir + content index."""
    import screencap.content_index as content_index
    from screencap.daemon.app import build_app

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings))
    monkeypatch.setattr(content_index, "default_index_path", lambda: index_path)

    client = AsyncDaemonClient(transport=httpx.ASGITransport(app=build_app()))

    async def _fake():
        return client

    monkeypatch.setattr(server, "_client", _fake)
    try:
        yield client
    finally:
        await client.aclose()


def _seed_content_index(index_path, recording="rec") -> None:
    import screencap.content_index as content_index

    with content_index.ContentIndex(index_path) as store:
        store.write_frames(
            recording,
            [
                content_index.IndexFrame(timestamp_ms=_mid_ms(_KEPT_IDX), text=_KEPT_OCR),
                content_index.IndexFrame(timestamp_ms=_mid_ms(_DELETED_IDX), text=_DEL_OCR),
            ],
        )


def _seed_tasks(ledger) -> None:
    # AGENT-owned tasks (source='agent', edited=0): a task NAME derives from the
    # window titles the delete removes, so an agent task over the deleted range is a
    # derived artifact purged under the R7 lifecycle rule. (A source='user' task is
    # explicit intent and PRESERVED by design — the wrong shape for an AE5 "gone
    # after delete" assertion; ``insert_task_segment`` forces 'user', so seed the
    # agent set directly.)
    from screencap.pipeline_state import TaskSegmentRow

    ledger.replace_task_segments([
        TaskSegmentRow(task_index=_KEPT_IDX, start_ts=_chunk_bounds(_KEPT_IDX)[0] + 1,
                       end_ts=_chunk_bounds(_KEPT_IDX)[1] - 1, name="kept task"),
        TaskSegmentRow(task_index=_DELETED_IDX, start_ts=_chunk_bounds(_DELETED_IDX)[0] + 1,
                       end_ts=_chunk_bounds(_DELETED_IDX)[1] - 1, name="deleted task"),
    ])


def _do_range_delete(recordings):
    """Delete chunk 1 via the range_delete core; return its rounded ms extent."""
    from screencap.range_delete import execute_delete, resolve_range

    cs, _ = _chunk_bounds(_DELETED_IDX)
    preview = resolve_range(_ms(cs + 1), _ms(cs + 2), recordings_dir=recordings)
    plan = preview.recordings[0]
    assert plan.chunk_indices == [_DELETED_IDX]  # only chunk 1 is in range
    report = execute_delete(
        preview.start_ms, preview.end_ms, preview.resolved_map(), recordings_dir=recordings
    )
    assert not report.reconfirm_required
    return plan.rounded_start_ms, plan.rounded_end_ms


@pytest.mark.asyncio
async def test_range_delete_leaves_every_mcp_tool_empty_in_range(monkeypatch, tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    index_path = tmp_path / "content_index.db"

    rec_dir, ledger = _make_recording(recordings, n_chunks=_N_CHUNKS)
    _seed_content_index(index_path)
    _seed_tasks(ledger)

    # Stub the ALLOW filter to allow-all (mirrors the frame.nearest round-trip test
    # in test_mcp_server): the fixture's recording.db isn't built for canonical
    # scrub-context derivation, so the real predicate fails CLOSED for every frame
    # — which would make the BEFORE-delete baseline vacuous. Allow-all isolates the
    # deletion effect: frame.nearest resolves while the screenshot is on disk and
    # misses once the delete unlinks it. (The real predicate fails closed AFTER the
    # delete too, so the AE5 "fails closed" claim holds either way.)
    import screencap.frame_blocked as fb
    monkeypatch.setattr(fb, "build_is_blocked", lambda rec_dir, tss, **kw: (lambda ts: False))

    del_ts = _mid_ms(_DELETED_IDX)
    kept_ts = _mid_ms(_KEPT_IDX)
    day = "1970-01-01"  # the fixture starts at unix 1000s
    # The fixture seeds each chunk's window_event at chunk_start, so a timeline
    # query must span the chunk bounds (not just the mid frame) to see the row.
    dcs, dce = _chunk_bounds(_DELETED_IDX)
    kcs, kce = _chunk_bounds(_KEPT_IDX)

    async with _daemon_at(monkeypatch, recordings, index_path):
        # --- BASELINE: every stream sees the chunk-1 (soon-deleted) data ---------
        assert any(h.timestamp_ms == del_ts for h in
                   (await server.search_screen_content(_DEL_OCR)).hits)
        assert (await server.search_transcript(_DEL_TOKEN)).hits
        assert (await server.query_timeline(start_ms=_ms(dcs), end_ms=_ms(dce),
                                            recording="rec")).rows
        assert (await server.resolve_frame("rec", del_ts, staleness_cap_ms=1000)).stem is not None
        tasks_before = await server.query_tasks(day, day)
        assert any(t.name == "deleted task" for d in tasks_before.days for t in d.tasks)

        # --- DELETE chunk 1 (irreversible; range_delete core) --------------------
    del_lo, del_hi = _do_range_delete(recordings)

    async with _daemon_at(monkeypatch, recordings, index_path):
        # --- AE5: every tool returns nothing in the deleted window ---------------
        # content: no hit at the deleted frame's timestamp.
        content = await server.search_screen_content(_DEL_OCR)
        assert not any(h.timestamp_ms == del_ts for h in content.hits)

        # transcript: the deleted chunk's speech token is gone from every file.
        assert (await server.search_transcript(_DEL_TOKEN)).hits == []

        # timeline: no rows in the deleted window.
        assert (await server.query_timeline(start_ms=del_lo, end_ms=del_hi,
                                            recording="rec")).rows == []

        # frame: resolution fails closed at the deleted timestamp (no nearby frame).
        assert (await server.resolve_frame("rec", del_ts, staleness_cap_ms=1000)).stem is None

        # browse_day: the deleted window is marked removed-by-you (gap provenance),
        # never surfaced as live footage or a live task.
        result_day = await server.browse_day(day)
        rec = next(r for r in result_day.recordings if r.recording == "rec")
        assert any(iv.start_ms <= del_ts < iv.end_ms for iv in rec.deleted)
        assert not any(t.name == "deleted task" for t in rec.tasks)

        # query_tasks: the purged task is gone from the cross-day grouping.
        tasks_after = await server.query_tasks(day, day)
        assert not any(t.name == "deleted task" for d in tasks_after.days for t in d.tasks)

        # create_clip: a clip cannot be cut over the deleted footage (fails closed).
        from screencap import clips
        monkeypatch.setattr(clips, "_export_clip", _faithful_export)
        clipped = await server.create_clip("rec", del_lo + 1000, del_lo + 3000)
        assert clipped.ok is False
        assert clipped.clip_id is None

        # chat evidence: no moment in the deleted window survives retrieval.
        assert _chat_evidence_in_window(recordings, _DEL_OCR, del_lo, del_hi) == []

        # --- SURGICAL: the out-of-range sibling chunk survives every tool --------
        assert any(h.timestamp_ms == kept_ts for h in
                   (await server.search_screen_content(_KEPT_OCR)).hits)
        assert (await server.search_transcript(_KEPT_TOKEN)).hits
        assert (await server.query_timeline(start_ms=_ms(kcs), end_ms=_ms(kce),
                                            recording="rec")).rows
        assert any(t.name == "kept task" for d in tasks_after.days for t in d.tasks)


def _faithful_export(rec_dir, rel_start_ms, rel_end_ms, out_path):
    """Vision/PyAV-free stand-in mirroring ``engine.video.export_clip``'s
    chunk-coverage gate: succeed only when a SURVIVING source ``chunk_*.mp4`` (with
    its manifest) still covers the requested relative-ms window — else fail closed
    with ``NoFramesInRangeError`` (the daemon maps it to ``no_frames_in_range``)."""
    import json

    from screencap.catalog import read_video_start_anchor
    from screencap.engine.video import NoFramesInRangeError

    anchor_ms = round((read_video_start_anchor(rec_dir) or 0.0) * 1000)
    for mp4 in rec_dir.glob("chunk_*.mp4"):
        idx = int(mp4.stem.split("_")[1])
        manifest = rec_dir / f"chunk_{idx:04d}_manifest.json"
        if not manifest.is_file():
            continue
        data = json.loads(manifest.read_text())
        cs_ms = round(data["chunk_start"] * 1000) - anchor_ms
        ce_ms = round(data["chunk_end"] * 1000) - anchor_ms
        if cs_ms < rel_end_ms and rel_start_ms < ce_ms:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x00" * 2048)
            return out_path
    raise NoFramesInRangeError("no surviving chunk covers the range")


def _chat_evidence_in_window(recordings, question, lo_ms, hi_ms) -> list:
    """The chat evidence-bundle moments (the retrieval seam ``chat.answer`` feeds
    from) whose pointer falls in ``[lo_ms, hi_ms)`` — the honest, non-model-gated
    read of "what evidence Chat could ground on"."""
    from screencap.recall.orchestrator import build_evidence_bundle

    bundle = build_evidence_bundle(question, recordings_dir=recordings)
    return [
        e for e in bundle.evidence
        if lo_ms <= e.pointer.timestamp_ms < hi_ms
    ]
