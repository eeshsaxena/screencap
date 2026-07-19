"""U10 — the in-vault clips store + catalog (``screencap.clips``).

Clips are DURABLE, retention-exempt artifacts governed by the vault privacy
rules, so these pin the safety contract of the store itself (the daemon verbs
that wrap it live in ``tests/daemon/test_clip_verbs.py``):

* create → a hardened ``.clips/`` mp4 + a catalog entry carrying
  ``source_recording`` + ``creator`` + the clip honesty flag;
* ``create_clip`` over a POLICY-purged range FAILS CLOSED (``policy_purged``)
  and writes nothing — never re-cuts purged pixels into a durable artifact;
* a USER-purged (``origin='user'``) range does NOT block the cut (only policy
  does — the origin discrimination);
* source eviction leaves the clip playable (AE8 second half);
* a POLICY purge over the clip's span deletes it (full overlap) or flags it
  (partial overlap) (AE8 first half);
* a USER range-delete over the span leaves the clip (R11);
* the ``.clips/`` dir is skipped by catalog listing, backfill enumeration, and
  the retention sweep, and never enters an upload list.

Privacy-marked + Vision-free (raw sqlite + injected exporter, no OCR / PyAV) — CI
runs only ``pytest -m privacy``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Reuse the realistic per-chunk recording fixture from the range-delete tests.
from tests.test_range_delete import (
    _chunk_bounds,
    _make_recording,
    _ms,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _fake_export(rec_dir, rel_start_ms, rel_end_ms, out_path):
    """Stand-in for ``engine.video.export_clip`` — writes a small mp4, no PyAV."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\x00" * 2048)
    return out


def _seed_purge_span(db_path: Path, start_s: float, end_s: float, *, origin: str) -> None:
    """Write one ``purged_interval`` row with an explicit ``origin``."""
    from screencap.enforcement.scrub_worker import ensure_purged_interval_schema

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        ensure_purged_interval_schema(cur)
        cur.execute(
            "INSERT INTO purged_interval (start_ts, end_ts, disabled_at, origin) "
            "VALUES (?, ?, ?, ?)",
            (float(start_s), float(end_s), None, origin),
        )
        conn.commit()
    finally:
        conn.close()


def _create(tmp_path, recording="rec", *, start_s, end_s, creator="ui"):
    from screencap import clips

    return clips.create_clip(
        recording,
        _ms(start_s),
        _ms(end_s),
        tz_offset_seconds=0,
        creator=creator,
        recordings_dir=tmp_path,
        export_fn=_fake_export,
    )


# ---------------------------------------------------------------------------
# create → catalog entry + playable file.
# ---------------------------------------------------------------------------


def test_create_writes_catalog_entry_and_file(tmp_path):
    from screencap import clips

    _make_recording(tmp_path, n_chunks=2)
    cs, ce = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)

    assert res.ok, res.reason
    entry = res.clip
    assert entry["source_recording"] == "rec"
    assert entry["creator"] == "ui"
    assert entry["start_ms"] == _ms(cs + 5)
    assert entry["end_ms"] == _ms(cs + 50)
    assert entry["source_day"]  # KTD-11 local calendar day, non-empty
    assert entry["honesty_flags"]["clip_video_capture_blocked_only"] is True
    assert "id" in entry and "created_at" in entry

    # Playable file present + non-empty in the hardened dot-dir.
    mp4 = tmp_path / ".clips" / f"{entry['id']}.mp4"
    assert mp4.is_file() and mp4.stat().st_size > 0

    # Catalog round-trips the entry.
    listed = clips.list_clips(recordings_dir=tmp_path)
    assert [c["id"] for c in listed] == [entry["id"]]


def test_clips_dir_is_hardened(tmp_path):

    _make_recording(tmp_path, n_chunks=1)
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 1, end_s=cs + 20)
    assert res.ok
    clips_dir = tmp_path / ".clips"
    assert (clips_dir.stat().st_mode & 0o777) == 0o700
    catalog = clips_dir / "catalog.json"
    assert (catalog.stat().st_mode & 0o777) == 0o600
    mp4 = clips_dir / f"{res.clip['id']}.mp4"
    assert (mp4.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Fail-closed over a POLICY-purged range (purge-then-clip ordering).
# ---------------------------------------------------------------------------


def test_create_over_policy_purged_range_fails_closed(tmp_path):
    from screencap import clips

    rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
    cs, ce = _chunk_bounds(0)
    # Purge THEN clip: a policy span covering the requested window.
    _seed_purge_span(rec_dir / "recording.db", cs, ce, origin="policy")

    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)
    assert not res.ok
    assert res.reason == "policy_purged"
    # Nothing written: no mp4, no catalog entry (never even a catalog file).
    assert not (tmp_path / ".clips" / "catalog.json").exists() or clips.list_clips(
        recordings_dir=tmp_path
    ) == []
    assert not any((tmp_path / ".clips").glob("*.mp4")) if (tmp_path / ".clips").exists() else True


def test_create_over_null_origin_purge_fails_closed(tmp_path):
    """A legacy NULL-origin purge classifies as POLICY (fail-closed)."""
    rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
    cs, ce = _chunk_bounds(0)
    _seed_purge_span(rec_dir / "recording.db", cs, ce, origin=None)  # legacy NULL
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)
    assert not res.ok
    assert res.reason == "policy_purged"


def test_create_over_user_purged_range_is_allowed(tmp_path):
    """A ``origin='user'`` purge does NOT block clip.create — only policy does."""
    rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
    cs, ce = _chunk_bounds(0)
    _seed_purge_span(rec_dir / "recording.db", cs, ce, origin="user")
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)
    assert res.ok, res.reason


# ---------------------------------------------------------------------------
# Failure taxonomy reuse.
# ---------------------------------------------------------------------------


def test_create_not_eligible_missing_recording(tmp_path):
    from screencap import clips

    (tmp_path / ".clips").mkdir()  # store may exist; recording does not
    res = clips.create_clip(
        "nope", 1000, 2000, recordings_dir=tmp_path, export_fn=_fake_export
    )
    assert not res.ok and res.reason == "not_eligible"


def test_create_maps_engine_reason(tmp_path):
    from screencap import clips
    from screencap.engine.video import NoFramesInRangeError

    _make_recording(tmp_path, n_chunks=1)
    cs, _ = _chunk_bounds(0)

    def _raise(*_a, **_k):
        raise NoFramesInRangeError("empty range")

    res = clips.create_clip(
        "rec", _ms(cs + 1), _ms(cs + 20),
        recordings_dir=tmp_path, export_fn=_raise,
    )
    assert not res.ok and res.reason == "no_frames_in_range"


# ---------------------------------------------------------------------------
# AE8 second half — source eviction leaves the clip playable.
# ---------------------------------------------------------------------------


def test_source_eviction_leaves_clip_playable(tmp_path):
    from screencap import clips

    rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)
    assert res.ok
    mp4 = tmp_path / ".clips" / f"{res.clip['id']}.mp4"

    # Simulate retention eviction of the SOURCE footage: unlink its chunks
    # (and the whole recording dir) — the clip lives outside it and survives.
    for chunk in rec_dir.glob("chunk_*.mp4"):
        chunk.unlink()
    assert mp4.is_file()  # clip untouched
    assert [c["id"] for c in clips.list_clips(recordings_dir=tmp_path)] == [res.clip["id"]]


# ---------------------------------------------------------------------------
# AE8 first half — POLICY purge over the clip's span deletes / flags it.
# ---------------------------------------------------------------------------


def test_policy_purge_full_overlap_deletes_clip(tmp_path):
    from screencap import clips

    _make_recording(tmp_path, n_chunks=2)
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)
    assert res.ok
    mp4 = tmp_path / ".clips" / f"{res.clip['id']}.mp4"

    # A policy purge span that fully covers the clip → delete (mp4 + entry gone).
    counts = clips.purge_clips_for_intervals(
        "rec", [(cs, cs + 100)], recordings_dir=tmp_path
    )
    assert counts.get("deleted") == 1
    assert not mp4.exists()
    assert clips.list_clips(recordings_dir=tmp_path) == []


def test_policy_purge_partial_overlap_flags_clip(tmp_path):
    from screencap import clips

    _make_recording(tmp_path, n_chunks=2)
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 100)
    assert res.ok
    mp4 = tmp_path / ".clips" / f"{res.clip['id']}.mp4"

    # A purge covering only the FIRST half of the clip → flag, keep the file.
    counts = clips.purge_clips_for_intervals(
        "rec", [(cs, cs + 40)], recordings_dir=tmp_path
    )
    assert counts.get("flagged") == 1
    assert mp4.is_file()  # kept, but disclosed
    listed = clips.list_clips(recordings_dir=tmp_path)
    assert len(listed) == 1
    assert listed[0]["honesty_flags"].get("policy_purged_partial") is True


def test_policy_purge_no_overlap_leaves_clip(tmp_path):
    from screencap import clips

    _make_recording(tmp_path, n_chunks=2)
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)
    assert res.ok
    counts = clips.purge_clips_for_intervals(
        "rec", [(cs + 500, cs + 600)], recordings_dir=tmp_path
    )
    assert counts.get("deleted", 0) == 0 and counts.get("flagged", 0) == 0
    assert len(clips.list_clips(recordings_dir=tmp_path)) == 1


def test_policy_purge_only_targets_matching_recording(tmp_path):
    """A purge for a DIFFERENT recording never touches this clip."""
    from screencap import clips

    _make_recording(tmp_path, n_chunks=2, name="rec")
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)
    assert res.ok
    clips.purge_clips_for_intervals("other", [(cs, cs + 100)], recordings_dir=tmp_path)
    assert len(clips.list_clips(recordings_dir=tmp_path)) == 1


# ---------------------------------------------------------------------------
# R11 — a USER range-delete over the clip span leaves the clip.
# ---------------------------------------------------------------------------


def test_user_range_delete_leaves_clip(tmp_path):
    from screencap import clips
    from screencap.range_delete import execute_delete, resolve_range

    _make_recording(tmp_path, n_chunks=2)
    cs, ce = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 5, end_s=cs + 50)
    assert res.ok
    mp4 = tmp_path / ".clips" / f"{res.clip['id']}.mp4"

    # A user range-delete over the SAME span — must not cascade to clips (R11).
    preview = resolve_range(_ms(cs + 5), _ms(cs + 50), recordings_dir=tmp_path)
    execute_delete(
        _ms(cs + 5), _ms(cs + 50), preview.resolved_map(), recordings_dir=tmp_path
    )

    assert mp4.is_file()
    listed = clips.list_clips(recordings_dir=tmp_path)
    assert len(listed) == 1
    # The kept clip is disclosed by the delete preview (R20).
    kept = [c for r in preview.recordings for c in r.kept_clips]
    assert [c["id"] for c in kept] == [res.clip["id"]]


# ---------------------------------------------------------------------------
# delete_clip.
# ---------------------------------------------------------------------------


def test_delete_clip_removes_file_and_entry(tmp_path):
    from screencap import clips

    _make_recording(tmp_path, n_chunks=1)
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 1, end_s=cs + 20)
    assert res.ok
    mp4 = tmp_path / ".clips" / f"{res.clip['id']}.mp4"

    removed = clips.delete_clip(res.clip["id"], recordings_dir=tmp_path)
    assert removed
    assert not mp4.exists()
    assert clips.list_clips(recordings_dir=tmp_path) == []
    # Idempotent — deleting an unknown id is a benign False.
    assert clips.delete_clip(res.clip["id"], recordings_dir=tmp_path) is False


# ---------------------------------------------------------------------------
# Catalog survives daemon restart (fresh reads see the persisted store).
# ---------------------------------------------------------------------------


def test_catalog_persists_across_reads(tmp_path):
    from screencap import clips

    _make_recording(tmp_path, n_chunks=1)
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 1, end_s=cs + 20)
    assert res.ok
    # A brand-new read (no shared in-memory state) still finds the clip.
    again = clips.list_clips(recordings_dir=tmp_path)
    assert [c["id"] for c in again] == [res.clip["id"]]


# ---------------------------------------------------------------------------
# The reserved ``.clips/`` dir is invisible to the recordings-tree enumerators.
# ---------------------------------------------------------------------------


def test_clips_dir_ignored_by_enumerators(tmp_path):
    from screencap import catalog
    from screencap.backfill.engine import _enumerate_recordings
    from screencap.daemon.retention_sweep import sweep_once

    _make_recording(tmp_path, n_chunks=1)
    cs, _ = _chunk_bounds(0)
    assert _create(tmp_path, start_s=cs + 1, end_s=cs + 20).ok

    # catalog.list_recordings never surfaces ".clips" as a phantom recording.
    names = {r.name for r in catalog.list_recordings(tmp_path)}
    assert ".clips" not in names and "rec" in names

    # backfill enumeration skips dot-dirs.
    assert all(d.name != ".clips" for d in _enumerate_recordings(tmp_path))

    # retention sweep skips dot-dirs (and never touches the clip mp4s).
    report = sweep_once(tmp_path)
    assert ".clips" not in report.errors
    assert (tmp_path / ".clips").is_dir()  # sweep left the store alone


# ---------------------------------------------------------------------------
# scrub_worker POLICY-purge propagation (R17 wiring) — the seam that cascades a
# retroactive app-disable to overlapping clips.
# ---------------------------------------------------------------------------


def test_scrub_worker_propagates_policy_purge_to_clips(tmp_path, monkeypatch):
    import screencap.clips as clips_mod
    from screencap.enforcement.scrub_worker import ScrubWorker
    from tests.enforcement.test_scrub_worker_v0 import (
        _insert_action_event,
        _insert_screenshot,
        _insert_window_event,
        _make_recording_db,
        _seed_recording,
    )

    # A recording dir named "rec" under the recordings root ``tmp_path`` (so
    # ``.clips`` lives at ``tmp_path/.clips`` and the scrub key is "rec").
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    db_path = _make_recording_db(rec_dir)
    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)
    for ts in (100.0, 101.0, 102.0):
        _insert_window_event(
            conn, recording_id=rec_id, timestamp=ts,
            bundle_id="com.spotify.client", app_name="Spotify",
        )
        _insert_action_event(
            conn, recording_id=rec_id, timestamp=ts + 0.1, window_event_timestamp=ts,
        )
        _insert_screenshot(conn, recording_id=rec_id, timestamp=ts)
    conn.commit()
    conn.close()

    captured = {}

    def _spy(recording, intervals, *, recordings_dir=None):
        captured["recording"] = recording
        captured["intervals"] = list(intervals)
        captured["recordings_dir"] = recordings_dir
        return {"deleted": 0, "flagged": 0}

    monkeypatch.setattr(clips_mod, "purge_clips_for_intervals", _spy)

    worker = ScrubWorker(disable_q=None, recording_db_path=db_path, capture_dir=rec_dir)
    worker._handle({
        "kind": "app",
        "bundle_id": "com.spotify.client",
        "app_name": "Spotify",
        "root_domain": None,
        "ts_unix": 1000.0,
        "source": "menubar",
    })

    # The POLICY purge cascaded to the clips store with this recording's key +
    # the scrubbed intervals rooted at the recordings dir.
    assert captured.get("recording") == "rec"
    assert captured.get("intervals"), "no intervals propagated to clips"
    assert captured.get("recordings_dir") == tmp_path


def test_clip_never_in_upload_list(tmp_path):
    from screencap import upload

    rec_dir, _ = _make_recording(tmp_path, n_chunks=1)
    cs, _ = _chunk_bounds(0)
    res = _create(tmp_path, start_s=cs + 1, end_s=cs + 20)
    assert res.ok
    clip_name = f"{res.clip['id']}.mp4"
    files = upload.list_recording_files(rec_dir)
    assert all(clip_name not in f.name for f in files)
    # And the reserved dir is never itself an uploadable recording dir.
    assert not ((tmp_path / ".clips") / "recording.db").exists()
