"""Characterization-first tests for U9 migration & reconciliation.

These tests are written BEFORE the seeding logic (characterization-first per
the U9 plan). They seed fixtures with OLD-PATH artifacts — legacy
``.chunk_*_status.json`` markers, a stale ``recording_complete.json`` sentinel,
a partially-uploaded chunk set — and assert CONSERVATIVE reconciliation:

  * a legacy status file is a HINT, never proof: a chunk is only ``UPLOADED``
    after a FRESH GCS re-confirm (the Bug-2 survivorship boundary);
  * GCS unreachable at migration time -> every unverified chunk is
    ``PENDING``/needs-verification, NEVER ``UPLOADED`` (mirrors
    ``reconcile_against_gcs``'s fail-conservative posture);
  * the stale ``recording_complete.json`` sentinel is IGNORED — ``chunks_expected``
    is regenerated from the reconciled ledger (Bug 3);
  * local->cloud promotion refuses with a clear error if a required chunk's
    local media was evicted and it is not confirmed in GCS (AE8 — never a
    partial cloud copy with silent holes).

Run with::

    PYTHONPATH=src python -m pytest tests/test_migration_reconcile.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from screencap import pipeline_state as ps


# ---------------------------------------------------------------------------
# Fixtures — an OLD-PATH chunked recording dir as it exists before the new
# code first runs over it: chunk media on disk, legacy per-chunk status files,
# maybe a stale recording_complete.json, and (for in-flight) no ledger yet.
# ---------------------------------------------------------------------------


def _make_old_path_recording(
    base: Path,
    name: str,
    *,
    n_chunks: int,
    legacy_uploaded: set[int] | None = None,
    stale_sentinel_count: int | None = None,
    destination: str | None = "cloud",
    seed_ledger: bool = False,
) -> Path:
    """Create an old-path chunked recording dir with no U1 ledger seeded.

    ``legacy_uploaded`` — chunk indices that have a legacy
    ``.chunk_chunk_NNNN_status.json`` marker (the old per-chunk "core files
    uploaded" proof). ``stale_sentinel_count`` — if set, write a
    ``recording_complete.json`` claiming this ``chunks_expected`` (the Bug-3
    stale sentinel). ``seed_ledger`` — if True, also seed the U1 ledger PENDING
    (the mid-old-path-upgrade case where the new code seeded rows but no GCS
    confirm happened yet).
    """
    from screencap.engine.db import create_db, crud

    legacy_uploaded = legacy_uploaded or set()
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    db_path = d / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    rec = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    for i in range(n_chunks):
        crud.insert_action_event(session, rec, 1000.0 + i * 5 + 1, {
            "name": "click", "mouse_x": 10.0, "mouse_y": 20.0,
            "mouse_button_name": "left", "mouse_pressed": True,
        })
    session.close()
    engine.dispose()

    # Chunk media + per-chunk artifacts (as the old path left them on disk).
    for i in range(n_chunks):
        (d / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 1024)
        (d / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 256)
        (d / f"events_{i:04d}.jsonl").write_text('{"_meta": 1}\n')
        (d / f"chunk_{i:04d}_manifest.json").write_text("{}")

    # Legacy per-chunk status markers (old chunk_processor.upload_chunk_files
    # naming: .chunk_<basename-without-ext>_status.json, i.e.
    # .chunk_chunk_0000_status.json).
    for i in legacy_uploaded:
        (d / f".chunk_chunk_{i:04d}_status.json").write_text(json.dumps({
            "uploaded_at": "2026-01-01T00:00:00",
            "files": [
                f"chunk_{i:04d}.mp4", f"audio_{i:04d}.flac",
                f"events_{i:04d}.jsonl", f"chunk_{i:04d}_manifest.json",
            ],
        }))

    if stale_sentinel_count is not None:
        (d / "recording_complete.json").write_text(json.dumps({
            "recording": name,
            "chunks_expected": stale_sentinel_count,
            "stop_reason": "stale",
        }))

    if destination is not None:
        (d / ".recording_intent").write_text(json.dumps({
            "version": 1,  # legacy intent — no retention policy fields
            "destination": destination,
            "privacy_mode": "public",
            "show_on_website": True,
        }))
    (d / ".recording_id").write_text(name)

    if seed_ledger:
        ps.ensure_pipeline_state_schema(db_path)
        ledger = ps.PipelineLedger(db_path)
        for i in range(n_chunks):
            ledger.seed_chunk(i)

    return d


def _remote_set(present: set[int]):
    """Build a ``remote_exists(idx) -> bool`` over a known-present index set."""
    def _f(idx: int) -> bool:
        return idx in present
    return _f


def _gcs_unreachable(idx: int) -> bool:
    """A ``remote_exists`` that raises — GCS unreachable / signing down."""
    raise RuntimeError("GCS unreachable (offline / expired token)")


# ---------------------------------------------------------------------------
# Happy path (migration): partially-uploaded old-path recording reconciles.
# ---------------------------------------------------------------------------


def test_partial_upload_reconciles_confirmed_chunks_uploaded(tmp_path):
    """Already-uploaded (legacy-status + GCS-confirmed) chunks become UPLOADED;
    the rest stay PENDING for re-run. No re-upload of confirmed chunks."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=4, legacy_uploaded={0, 1},
    )
    # GCS confirms chunks 0,1 present; 2,3 are not.
    ledger = ps.reconcile_ledger_from_disk(
        d, remote_exists=_remote_set({0, 1}),
    )
    rows = {r.chunk_index: r for r in ledger.all_chunks()}
    assert len(rows) == 4  # closed set: one row per chunk on disk
    assert rows[0].upload_state == ps.UploadState.UPLOADED
    assert rows[1].upload_state == ps.UploadState.UPLOADED
    assert rows[2].upload_state == ps.UploadState.PENDING
    assert rows[3].upload_state == ps.UploadState.PENDING


def test_legacy_status_without_gcs_confirm_is_not_uploaded(tmp_path):
    """A legacy status file is a HINT, not proof: with NO fresh GCS confirm the
    chunk stays PENDING — the Bug-2 survivorship boundary."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=2, legacy_uploaded={0, 1},
    )
    # GCS confirms NOTHING despite both having legacy markers.
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set(set()))
    rows = {r.chunk_index: r for r in ledger.all_chunks()}
    assert rows[0].upload_state == ps.UploadState.PENDING
    assert rows[1].upload_state == ps.UploadState.PENDING


def test_unknown_chunks_seeded_pending(tmp_path):
    """A chunk on disk with NO legacy status marker is seeded PENDING (closed
    set), never assumed done."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=3, legacy_uploaded=set(),
    )
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set(set()))
    rows = ledger.all_chunks()
    assert len(rows) == 3
    assert all(r.upload_state == ps.UploadState.PENDING for r in rows)


# ---------------------------------------------------------------------------
# Stale sentinel (Bug 3): recording_complete.json ignored; chunks_expected
# regenerated from the reconciled ledger.
# ---------------------------------------------------------------------------


def test_stale_sentinel_ignored_chunks_expected_from_ledger(tmp_path):
    """A legacy recording_complete.json claiming chunks_expected=0 (Bug 3) is
    ignored — chunks_expected is regenerated from the chunks actually on disk."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=5, legacy_uploaded={0, 1, 2, 3, 4},
        stale_sentinel_count=0,  # the Bug-3 "manifests not generated yet" case
    )
    ledger = ps.reconcile_ledger_from_disk(
        d, remote_exists=_remote_set({0, 1, 2, 3, 4}),
    )
    # chunks_expected comes from the reconciled ledger (5 on disk), NOT the
    # stale sentinel's 0.
    assert ledger.chunks_expected() == 5


def test_stale_sentinel_overcount_ignored(tmp_path):
    """A stale sentinel claiming MORE chunks than exist on disk is also ignored
    — the frozen count tracks the reconciled evidence, not the sentinel."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=2, legacy_uploaded=set(),
        stale_sentinel_count=99,
    )
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set(set()))
    assert ledger.chunks_expected() == 2


# ---------------------------------------------------------------------------
# Error path (remote missing): legacy status present but remote object gone.
# ---------------------------------------------------------------------------


def test_legacy_status_but_remote_missing_is_needs_work(tmp_path):
    """A legacy status file says uploaded, but the remote object is missing ->
    the chunk is re-stated as needs-work (PENDING), NOT assumed uploaded."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=3, legacy_uploaded={0, 1, 2},
    )
    # Remote has 0 only; 1 and 2 were uploaded long ago but the objects are gone.
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set({0}))
    rows = {r.chunk_index: r for r in ledger.all_chunks()}
    assert rows[0].upload_state == ps.UploadState.UPLOADED
    assert rows[1].upload_state == ps.UploadState.PENDING
    assert rows[2].upload_state == ps.UploadState.PENDING


# ---------------------------------------------------------------------------
# Error path (GCS unreachable): never flip to UPLOADED; stays needs-verification.
# ---------------------------------------------------------------------------


def test_gcs_unreachable_seeds_pending_never_uploaded(tmp_path):
    """GCS unreachable (offline / expired token / signing down) at migration
    time -> every unverified chunk is PENDING, NEVER UPLOADED, even when legacy
    status files claim upload. Mirrors reconcile_against_gcs fail-conservative."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=3, legacy_uploaded={0, 1, 2},
    )
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_gcs_unreachable)
    rows = ledger.all_chunks()
    assert len(rows) == 3
    assert all(r.upload_state == ps.UploadState.PENDING for r in rows)


def test_gcs_unreachable_leaves_recording_non_evictable(tmp_path):
    """With GCS unreachable, no chunk is UPLOADED, so eviction is refused for
    every chunk (the safe non-evictable state — a migration seed can never
    authorize a later eviction without a fresh confirm)."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=2, legacy_uploaded={0, 1},
    )
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_gcs_unreachable)
    for idx in (0, 1):
        with pytest.raises(ps.EvictionRefused):
            ledger.begin_eviction(idx, remote_exists=lambda: True)


# ---------------------------------------------------------------------------
# Mid-old-path upgrade: ledger rows already seeded PENDING (new code ran but no
# GCS confirm yet) — reconcile conservatively, no eviction before re-stat.
# ---------------------------------------------------------------------------


def test_mid_old_path_upgrade_reconciles_conservatively(tmp_path):
    """A recording mid-old-path-upload when the new code first runs: the ledger
    has PENDING rows already; reconcile only flips GCS-confirmed chunks, the
    rest stay PENDING — no eviction before a fresh re-stat."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=4, legacy_uploaded={0}, seed_ledger=True,
    )
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set({0, 1}))
    rows = {r.chunk_index: r for r in ledger.all_chunks()}
    # 0 had a legacy marker AND is confirmed; 1 is confirmed despite no marker;
    # 2,3 stay PENDING.
    assert rows[0].upload_state == ps.UploadState.UPLOADED
    assert rows[1].upload_state == ps.UploadState.UPLOADED
    assert rows[2].upload_state == ps.UploadState.PENDING
    assert rows[3].upload_state == ps.UploadState.PENDING


def test_reconcile_does_not_reseed_advanced_rows(tmp_path):
    """Reconcile is idempotent: a row already UPLOADED (e.g. a prior reconcile)
    is left UPLOADED and never reset to PENDING by a second pass."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=2, legacy_uploaded={0, 1}, seed_ledger=True,
    )
    ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set({0, 1}))
    # Second pass with GCS now unreachable must NOT downgrade the UPLOADED rows.
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_gcs_unreachable)
    rows = {r.chunk_index: r for r in ledger.all_chunks()}
    assert rows[0].upload_state == ps.UploadState.UPLOADED
    assert rows[1].upload_state == ps.UploadState.UPLOADED


# ---------------------------------------------------------------------------
# chunks_expected is frozen and not re-frozen to a conflicting value.
# ---------------------------------------------------------------------------


def test_reconcile_freezes_chunks_expected_once(tmp_path):
    """Reconcile freezes chunks_expected from the on-disk count; a re-run with
    the same count is a no-op (does not raise)."""
    d = _make_old_path_recording(tmp_path, "rec", n_chunks=3, legacy_uploaded=set())
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set(set()))
    assert ledger.chunks_expected() == 3
    # Idempotent re-run — must not raise on re-freezing the same count.
    ledger2 = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set(set()))
    assert ledger2.chunks_expected() == 3


def test_reconcile_respects_existing_frozen_count(tmp_path):
    """If chunks_expected is already frozen (engine writer froze it at the final
    rotation), reconcile does NOT refreeze a conflicting value — it leaves the
    engine's frozen count intact even if more chunk files appear later."""
    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=3, legacy_uploaded=set(), seed_ledger=True,
    )
    # Engine writer already froze chunks_expected at 3.
    ps.PipelineLedger(d / "recording.db").freeze_chunks_expected(3)
    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set(set()))
    assert ledger.chunks_expected() == 3


# ---------------------------------------------------------------------------
# Legacy single-file (R14): no chunks -> no closed chunk set seeded; the dir is
# still readable. The reconciler returns a ledger with zero chunk rows.
# ---------------------------------------------------------------------------


def test_legacy_single_file_seeds_no_chunks(tmp_path):
    """A non-chunked (single-file) recording has no chunk_*.mp4; reconcile seeds
    NO chunk rows (it is not forced into the per-chunk ledger model — R14)."""
    from screencap.engine.db import create_db, crud

    d = tmp_path / "legacy"
    d.mkdir(parents=True)
    db_path = d / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080,
        "pixel_ratio": 2.0, "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    session.close()
    engine.dispose()
    (d / "video.mp4").write_bytes(b"single-file video")
    (d / ".recording_id").write_text("legacy")

    ledger = ps.reconcile_ledger_from_disk(d, remote_exists=_remote_set(set()))
    assert ledger.all_chunks() == []
    # No chunks on disk -> chunks_expected frozen at 0 (nothing to gate on).
    assert ledger.chunks_expected() in (0, None)


# ---------------------------------------------------------------------------
# AE8 — promotion hole detection. A local recording with some chunks already
# evicted, promoted to cloud, must REFUSE with a clear error and upload nothing
# partial. The hole detector reads the ledger: an expected chunk whose local
# media is gone AND that is not confirmed in GCS is a HOLE.
# ---------------------------------------------------------------------------


def _evict_chunk_files(d: Path, idx: int) -> None:
    """Simulate retention having evicted chunk ``idx``'s local media files."""
    for nm in (
        f"chunk_{idx:04d}.mp4", f"audio_{idx:04d}.flac",
        f"events_{idx:04d}.jsonl", f"chunk_{idx:04d}_manifest.json",
    ):
        (d / nm).unlink(missing_ok=True)


def test_promotion_refuses_when_required_chunk_evicted(tmp_path):
    """AE8: a local recording whose retention evicted chunk 1's media, promoted
    to cloud, has a HOLE — detect_promotion_holes flags chunk 1, and the
    promotion must refuse (never a partial cloud copy)."""
    from screencap.terminal_stage import detect_promotion_holes

    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=3, legacy_uploaded=set(),
        destination="local", seed_ledger=True,
    )
    # Mark all LOCAL_DONE (a finished local recording), then evict chunk 1 and
    # record EVICTED in the ledger as retention would.
    ledger = ps.PipelineLedger(d / "recording.db")
    ledger.freeze_chunks_expected(3)
    for i in range(3):
        ledger.mark_local_done(i)
    _evict_chunk_files(d, 1)
    ledger.begin_local_eviction(1)
    ledger.commit_eviction(1, unlink=lambda: None)

    # Chunk 1's media is gone and it is NOT in GCS (local recording was never
    # uploaded). That is a hole.
    holes = detect_promotion_holes(d, remote_exists=_remote_set(set()))
    assert 1 in holes
    assert 0 not in holes and 2 not in holes


def test_promotion_no_holes_when_evicted_chunk_is_in_gcs(tmp_path):
    """An evicted chunk that IS confirmed in GCS is NOT a hole — the cloud copy
    survives even though the local media was reclaimed (the normal
    delete-after-upload cloud case)."""
    from screencap.terminal_stage import detect_promotion_holes

    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=3, legacy_uploaded={1}, seed_ledger=True,
    )
    ledger = ps.PipelineLedger(d / "recording.db")
    ledger.freeze_chunks_expected(3)
    ledger.mark_staged(1)
    ledger.mark_uploaded(1)
    _evict_chunk_files(d, 1)
    ledger.begin_eviction(1, remote_exists=lambda: True)
    ledger.commit_eviction(1, unlink=lambda: None)

    # GCS confirms chunk 1 present -> no hole even though local media is gone.
    holes = detect_promotion_holes(d, remote_exists=_remote_set({1}))
    assert holes == []


def test_promotion_no_holes_when_all_media_present(tmp_path):
    """All chunk media on disk and nothing evicted -> no holes (the happy
    promotion case: re-run the terminal stage on the surviving chunks)."""
    from screencap.terminal_stage import detect_promotion_holes

    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=3, legacy_uploaded=set(),
        destination="local", seed_ledger=True,
    )
    holes = detect_promotion_holes(d, remote_exists=_remote_set(set()))
    assert holes == []


def test_promotion_hole_when_media_gone_without_ledger_eviction(tmp_path):
    """Defense-in-depth: even if a chunk's media vanished WITHOUT a ledger
    EVICTED record (manual deletion, partial copy), a missing required chunk not
    confirmed in GCS is still a hole — the detector keys off on-disk presence +
    GCS, not only the ledger's EVICTED state."""
    from screencap.terminal_stage import detect_promotion_holes

    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=2, legacy_uploaded=set(),
        destination="local", seed_ledger=True,
    )
    ledger = ps.PipelineLedger(d / "recording.db")
    ledger.freeze_chunks_expected(2)
    for i in range(2):
        ledger.mark_local_done(i)
    _evict_chunk_files(d, 0)  # media gone but ledger still LOCAL_DONE

    holes = detect_promotion_holes(d, remote_exists=_remote_set(set()))
    assert 0 in holes


def test_promote_recording_to_cloud_raises_on_hole(tmp_path):
    """The promotion guard raises PromotionRefused with an actionable message
    listing the missing chunk(s) — never producing a partial cloud copy."""
    from screencap.terminal_stage import (
        PromotionRefused,
        assert_promotable_to_cloud,
    )

    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=3, legacy_uploaded=set(),
        destination="local", seed_ledger=True,
    )
    ledger = ps.PipelineLedger(d / "recording.db")
    ledger.freeze_chunks_expected(3)
    for i in range(3):
        ledger.mark_local_done(i)
    _evict_chunk_files(d, 2)
    ledger.begin_local_eviction(2)
    ledger.commit_eviction(2, unlink=lambda: None)

    with pytest.raises(PromotionRefused) as exc:
        assert_promotable_to_cloud(d, remote_exists=_remote_set(set()))
    msg = str(exc.value)
    assert "2" in msg  # names the missing chunk
    assert d.name in msg


def test_promote_recording_to_cloud_ok_when_no_holes(tmp_path):
    """assert_promotable_to_cloud returns cleanly when every required chunk is
    present locally or confirmed in GCS."""
    from screencap.terminal_stage import assert_promotable_to_cloud

    d = _make_old_path_recording(
        tmp_path, "rec", n_chunks=2, legacy_uploaded=set(),
        destination="local", seed_ledger=True,
    )
    ledger = ps.PipelineLedger(d / "recording.db")
    ledger.freeze_chunks_expected(2)
    for i in range(2):
        ledger.mark_local_done(i)
    # No eviction — all media present.
    assert_promotable_to_cloud(d, remote_exists=_remote_set(set()))  # no raise
