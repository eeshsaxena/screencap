"""Deferred-delete upgrade migration engine + job + verbs (SCR-258 U6, KTD-18).

Vision-free, ``@pytest.mark.privacy``. The container is MOCKED: a symlink-backed
:class:`FakeMounter` models the "encrypted volume" (writes persist in a backing
dir; attach/detach flip a symlink at the mountpoint) so copy/verify/cutover/sweep
run against REAL files copied between temp dirs, exercising the ledger + the
deferred-delete + sidecar-cutover-window invariants without any ``hdiutil``.

Coverage (plan U6 scenarios):
* AE6: decline (never started) changes nothing; kill between COPIED and VERIFIED
  resumes + re-verifies, no loss / no double-delete, plaintext authoritative;
  accept-while-recording skips the active dir this round, migrates it next round,
  cutover waits.
* DEFERRED-DELETE (crux): a VERIFIED recording is still listed + readable from the
  plaintext root mid-migration; plaintext removed only by the post-cutover sweep;
  a kill between cutover and sweep resumes the sweep (no orphaned plaintext).
* End-to-end multi-recording: byte-identical inside the container, no plaintext
  after the sweep.
* Sidecars copied only in the cutover window: a row written during record-through
  before cutover is present after migration (NOT dropped by a per-round copy).
* Lock during migration pauses the job at a recording boundary; unlock resumes.
* Disk preflight failure + mid-copy ENOSPC → distinct PAUSED state + typed reason,
  plaintext intact.
* Corrupt/missing ledger → not-done; re-run converges.
* Custom-recordings install → encrypt.start refuses with the documented reason.
* Status snapshots are recording-name-free (R9); encrypt.start audit record is
  written with peer provenance + outcome.
"""

from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from screencap import migration
from screencap.migration import (
    MigrationLedger,
    MigrationPaths,
    MigrationPhase,
    MigrationState,
    UnitState,
    run_migration,
    should_auto_resume,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class FakeMounter:
    """Symlink-backed stand-in for the container: attach = symlink → backing.

    The backing dir IS the persistent "encrypted volume": writing through an
    attached mountpoint (a symlink) lands in the backing dir, so a detach + a
    re-attach at a different mountpoint faithfully shows the same data — exactly
    the "same bundle, new mountpoint" swap the real cutover performs.
    """

    def __init__(self, backing: Path) -> None:
        self.backing = backing
        backing.mkdir(parents=True, exist_ok=True)
        self.attach_calls: list[str] = []
        self.detach_calls: list[str] = []

    def attach(self, mountpoint: Path) -> None:
        mountpoint = Path(mountpoint)
        self.attach_calls.append(str(mountpoint))
        mountpoint.parent.mkdir(parents=True, exist_ok=True)
        if mountpoint.is_symlink():
            return
        if mountpoint.exists():
            if mountpoint.is_dir() and not any(mountpoint.iterdir()):
                mountpoint.rmdir()
            else:
                raise RuntimeError(f"mountpoint not empty: {mountpoint}")
        os.symlink(self.backing, mountpoint)

    def detach(self, mountpoint: Path) -> None:
        mountpoint = Path(mountpoint)
        self.detach_calls.append(str(mountpoint))
        if mountpoint.is_symlink():
            mountpoint.unlink()

    def is_attached(self, mountpoint: Path) -> bool:
        return Path(mountpoint).is_symlink()


def _write_recording(root: Path, name: str, files: dict[str, bytes]) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


def _tree_bytes(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, _dirs, fnames in os.walk(root):
        for f in fnames:
            p = Path(dirpath) / f
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


class _Env:
    """A fully-wired engine environment over temp dirs with the container faked."""

    def __init__(self, tmp_path: Path) -> None:
        self.plaintext_root = tmp_path / "recordings"
        self.plaintext_root.mkdir()
        self.backing = tmp_path / "backing"
        self.interim = tmp_path / "run" / "migrate-mnt"
        self.aside = tmp_path / "recordings-migrated-plaintext"
        self.sidecar_src = tmp_path / "base"
        self.sidecar_src.mkdir()
        self.mounter = FakeMounter(self.backing)
        self.ledger = MigrationLedger(tmp_path / "migrate_state.db")
        self.flag_writes: list[bool] = []
        self.paths = MigrationPaths(
            plaintext_root=self.plaintext_root,
            interim_mountpoint=self.interim,
            aside_root=self.aside,
            sidecar_source_dir=self.sidecar_src,
        )

    def run(self, **overrides):
        kwargs = dict(
            ledger=self.ledger,
            paths=self.paths,
            mounter=self.mounter,
            set_container_enabled=self.flag_writes.append,
        )
        kwargs.update(overrides)
        return run_migration(**kwargs)

    def container_view(self) -> Path:
        """The authoritative mountpoint post-cutover (== plaintext_root path)."""
        return self.plaintext_root


# ---------------------------------------------------------------------------
# End-to-end + deferred-delete (crux, proof-first)
# ---------------------------------------------------------------------------


def test_end_to_end_multi_recording_byte_identical_no_plaintext(tmp_path):
    env = _Env(tmp_path)
    originals = {
        "rec-a": {"recording.db": b"AAA", "screenshots/0.jpg": b"img0"},
        "rec-b": {"recording.db": b"BBB", "video.mp4": b"\x00\x01\x02"},
        "rec-c": {"recording.db": b"CCC"},
    }
    saved = {}
    for name, files in originals.items():
        _write_recording(env.plaintext_root, name, files)
        saved[name] = dict(files)

    summary = env.run()

    assert summary.state is MigrationState.COMPLETED
    assert summary.phase is MigrationPhase.DONE
    assert summary.deleted == 3
    # Container holds every recording byte-identically (read through the mount).
    view = env.container_view()
    for name, files in saved.items():
        for rel, data in files.items():
            assert (view / name / rel).read_bytes() == data
    # No plaintext originals remain (aside swept).
    assert not env.aside.exists()
    # The flag was flipped to make the container authoritative.
    assert env.flag_writes and env.flag_writes[-1] is True


def test_deferred_delete_verified_recording_readable_until_sweep(tmp_path):
    """CRUX invariant: a VERIFIED recording is STILL readable from the plaintext
    root mid-migration and is NOT deleted before the post-cutover sweep."""
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    _write_recording(env.plaintext_root, "rec-b", {"recording.db": b"BBB"})

    # An active recording ('rec-b') blocks cutover, so the pass copies+verifies
    # rec-a and returns RUNNING before any cutover/delete.
    summary = env.run(active_recording_name=lambda: "rec-b")

    assert summary.state is MigrationState.RUNNING
    assert env.ledger.unit_status("rec-a") is UnitState.VERIFIED
    # DEFERRED-DELETE PROOF: rec-a's plaintext is still present + readable, NOT
    # deleted, and no aside dir was created (nothing moved off the read-root).
    assert (env.plaintext_root / "rec-a" / "recording.db").read_bytes() == b"AAA"
    assert env.ledger.unit_status("rec-a") is not UnitState.PLAINTEXT_DELETED
    assert not env.aside.exists()
    # rec-b (active) is skipped this round — still PENDING, plaintext intact.
    assert env.ledger.unit_status("rec-b") is UnitState.PENDING
    assert (env.plaintext_root / "rec-b" / "recording.db").read_bytes() == b"BBB"

    # Next round: the recording has finished → migrate rec-b, cutover, sweep.
    summary2 = env.run(active_recording_name=lambda: None)
    assert summary2.state is MigrationState.COMPLETED
    assert not env.aside.exists()
    view = env.container_view()
    assert (view / "rec-a" / "recording.db").read_bytes() == b"AAA"
    assert (view / "rec-b" / "recording.db").read_bytes() == b"BBB"


def test_no_plaintext_delete_call_before_cutover(tmp_path, monkeypatch):
    """Proof-first: NOTHING under the plaintext root is unlinked before cutover.

    A spy on ``shutil.rmtree`` (the only deletion path) records every target; with
    cutover blocked by an active recording, it must never touch the plaintext
    root — the deferred-delete guarantee, asserted at the syscall boundary.
    """
    import shutil

    targets: list[str] = []
    real_rmtree = shutil.rmtree

    def _spy(path, *a, **k):
        targets.append(str(path))
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(migration.shutil, "rmtree", _spy)

    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    _write_recording(env.plaintext_root, "rec-b", {"recording.db": b"BBB"})

    env.run(active_recording_name=lambda: "rec-b")

    plaintext = str(env.plaintext_root)
    assert not any(t.startswith(plaintext) and "migrated-plaintext" not in t
                   for t in targets), targets


def test_kill_between_cutover_and_sweep_resumes(tmp_path, monkeypatch):
    """A kill right after cutover (before the sweep completes) resumes the sweep;
    the container is authoritative and no orphaned plaintext is left."""
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    _write_recording(env.plaintext_root, "rec-b", {"recording.db": b"BBB"})

    # Simulate a crash the instant cutover finishes but before the sweep runs.
    boom = RuntimeError("killed after cutover")

    def _explode(*a, **k):
        raise boom

    monkeypatch.setattr(migration, "_run_sweep", _explode)
    with pytest.raises(RuntimeError):
        env.run()

    # Durable state: cutover is done (container authoritative), plaintext moved
    # aside but NOT yet deleted.
    assert env.ledger.phase() is MigrationPhase.CUTOVER_DONE
    assert env.aside.exists()
    view = env.container_view()
    assert (view / "rec-a" / "recording.db").read_bytes() == b"AAA"

    # Resume: the real sweep runs, deletes the aside plaintext, completes.
    monkeypatch.undo()
    summary = env.run()
    assert summary.state is MigrationState.COMPLETED
    assert not env.aside.exists()
    assert env.ledger.unit_status("rec-a") is UnitState.PLAINTEXT_DELETED


def test_resume_between_copied_and_verified_reverifies(tmp_path, monkeypatch):
    """AE6: a kill between COPIED and VERIFIED resumes by RE-VERIFYING (not
    re-copying) an already-copied recording, then converges with no loss."""
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})

    # Pre-stage: the recording was already copied into the container and the
    # ledger left it COPIED (the crash window). Emulate by attaching + copying.
    env.mounter.attach(env.interim)
    (env.interim / "rec-a").mkdir()
    (env.interim / "rec-a" / "recording.db").write_bytes(b"AAA")
    env.ledger.seed(["rec-a"])
    env.ledger.mark("rec-a", UnitState.COPIED)

    copies: list[str] = []
    real_copy = migration._copy_recording

    def _spy_copy(src, dst):
        copies.append(str(src))
        return real_copy(src, dst)

    monkeypatch.setattr(migration, "_copy_recording", _spy_copy)

    summary = env.run()

    assert summary.state is MigrationState.COMPLETED
    # RE-VERIFY not re-copy: the already-COPIED, byte-matching unit was NOT
    # re-copied on resume.
    assert copies == []
    assert not env.aside.exists()
    assert (env.container_view() / "rec-a" / "recording.db").read_bytes() == b"AAA"


def test_decline_changes_nothing(tmp_path):
    """AE6: declining (never starting) leaves the plaintext fully intact."""
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    before = _tree_bytes(env.plaintext_root)
    # No run invoked. Ledger is empty / not-done; plaintext untouched.
    assert env.ledger.run_state() is None
    assert not env.ledger.is_done()
    assert _tree_bytes(env.plaintext_root) == before


# ---------------------------------------------------------------------------
# Sidecar copied only in the cutover window (proof-first)
# ---------------------------------------------------------------------------


def _make_index_db(path: Path, rows: list[str]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS frames (txt TEXT)")
        conn.executemany("INSERT INTO frames (txt) VALUES (?)", [(r,) for r in rows])
        conn.commit()
    finally:
        conn.close()


def _index_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT txt FROM frames ORDER BY txt")]
    finally:
        conn.close()


def test_sidecar_row_written_during_record_through_survives(tmp_path):
    """A row written to the plaintext content index AFTER the copy round but
    BEFORE cutover is present after migration — proving sidecars are copied in
    the cutover window, never dropped by a stale per-round copy."""
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    idx = env.sidecar_src / "content_index.db"
    _make_index_db(idx, ["row-A"])

    # Round 1: cutover blocked by an active recording. Recordings copy; sidecars
    # are NOT touched (they only copy in the cutover window).
    env.run(active_recording_name=lambda: "rec-b")
    # ... a record-through write lands in the plaintext index between rounds.
    _make_index_db(idx, ["row-B"])
    assert not (env.backing / ".store").exists()  # sidecar not copied per-round

    # Round 2: cutover runs, copying the sidecar (now A + B) into the container.
    summary = env.run(active_recording_name=lambda: None)
    assert summary.state is MigrationState.COMPLETED
    migrated_idx = env.container_view() / ".store" / "content_index.db"
    assert migrated_idx.exists()
    assert set(_index_rows(migrated_idx)) == {"row-A", "row-B"}


def test_sidecars_copied_under_cutover_reservation_and_writer_pause(tmp_path):
    """The sidecar copy happens INSIDE the acquire_migration reservation and
    AFTER the writers were paused (KTD-18 ordering), asserted via spies."""
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    _make_index_db(env.sidecar_src / "content_index.db", ["row-A"])

    order: list[str] = []
    import contextlib

    @contextlib.contextmanager
    def _reservation():
        order.append("reserve-enter")
        try:
            yield
        finally:
            order.append("reserve-exit")

    @contextlib.contextmanager
    def _pause():
        order.append("pause-writers")
        yield

    real_copy = migration._copy_sidecars_into_container

    def _copy_spy(paths, names):
        order.append("copy-sidecars")
        return real_copy(paths, names)

    import screencap.migration as m
    m._copy_sidecars_into_container = _copy_spy
    try:
        env.run(cutover_reservation=_reservation, pause_sidecar_writers=_pause)
    finally:
        m._copy_sidecars_into_container = real_copy

    # Reservation opened, writers paused, THEN sidecars copied, all before the
    # reservation closed.
    assert order.index("reserve-enter") < order.index("pause-writers")
    assert order.index("pause-writers") < order.index("copy-sidecars")
    assert order.index("copy-sidecars") < order.index("reserve-exit")


# ---------------------------------------------------------------------------
# Disk preflight + ENOSPC → distinct PAUSED, plaintext intact
# ---------------------------------------------------------------------------


def test_disk_preflight_failure_pauses_plaintext_intact(tmp_path):
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    before = _tree_bytes(env.plaintext_root)

    summary = env.run(disk_preflight=lambda needed: migration.PAUSE_REASON_DISK)

    assert summary.state is MigrationState.PAUSED
    assert summary.paused_reason == migration.PAUSE_REASON_DISK
    # Plaintext intact; nothing copied, no cutover.
    assert _tree_bytes(env.plaintext_root) == before
    assert not env.aside.exists()
    assert env.ledger.unit_status("rec-a") is UnitState.PENDING


def test_midcopy_enospc_pauses_with_reason_plaintext_intact(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    before = _tree_bytes(env.plaintext_root)

    def _enospc(src, dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(migration, "_copy_recording", _enospc)

    summary = env.run()

    assert summary.state is MigrationState.PAUSED
    assert summary.paused_reason == migration.PAUSE_REASON_DISK
    assert env.ledger.unit_status("rec-a") is UnitState.PENDING  # left PENDING
    assert _tree_bytes(env.plaintext_root) == before
    assert not env.aside.exists()
    # A paused disk run is auto-resume eligible (unlike a cancel).
    assert should_auto_resume(env.ledger)


# ---------------------------------------------------------------------------
# Cancel + corrupt/missing ledger + auto-resume policy
# ---------------------------------------------------------------------------


def test_bare_stop_event_halt_is_resumable(tmp_path):
    """A bare ``stop_event`` (a lock pause / teardown) halts at a boundary and
    leaves the run RESUMABLE (RUNNING), with the plaintext untouched."""
    import threading

    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    stop = threading.Event()
    stop.set()

    summary = env.run(stop_event=stop)
    assert summary.state is MigrationState.RUNNING
    assert should_auto_resume(env.ledger) is True  # interrupted → resumes
    assert (env.plaintext_root / "rec-a" / "recording.db").exists()
    # A fresh (unset) run converges.
    assert env.run().state is MigrationState.COMPLETED


def test_explicit_cancel_is_not_auto_resumed(tmp_path):
    """A run the JOB marked CANCELLED (before setting the flag) stays CANCELLED
    at the boundary and is NOT auto-resumed (honors the user's last intent)."""
    import threading

    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    env.ledger.seed(["rec-a"])
    env.ledger.set_run(state=MigrationState.CANCELLED)  # job.cancel() shape
    stop = threading.Event()
    stop.set()

    summary = env.run(stop_event=stop)
    assert summary.state is MigrationState.CANCELLED
    assert should_auto_resume(env.ledger) is False
    assert (env.plaintext_root / "rec-a" / "recording.db").exists()


def test_corrupt_or_missing_ledger_converges_on_rerun(tmp_path):
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    # A fresh ledger is "not done"; a re-run converges to COMPLETED.
    assert not env.ledger.is_done()
    env.run()
    assert env.ledger.is_done()
    # Idempotent: a second run is a no-op that stays COMPLETED (no double-delete).
    summary = env.run()
    assert summary.state is MigrationState.COMPLETED
    assert env.ledger.progress()[3] == 1  # deleted count stable


def test_ledger_seed_is_closed_set_idempotent(tmp_path):
    ledger = MigrationLedger(tmp_path / "m.db")
    ledger.seed(["a", "b"])
    ledger.mark("a", UnitState.VERIFIED)
    ledger.seed(["a", "b"])  # re-seed same set: no denominator change, no reset
    assert ledger.progress()[-1] == 2
    assert ledger.unit_status("a") is UnitState.VERIFIED  # not reset to PENDING


# ---------------------------------------------------------------------------
# FIX 1 — record-through straggler is never lost; sweep never blanket-deletes
# ---------------------------------------------------------------------------


def test_record_through_straggler_is_not_lost_at_cutover(tmp_path):
    """FIX 1 (data-loss): a recording created in the plaintext root AFTER the
    closed set was seeded (a record-through straggler started+stopped during the
    long copy window) is detected before cutover, seeded, and migrated on a later
    pass — never moved aside by the wholesale swap and destroyed by the sweep."""
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})

    made = {"done": False}

    def _straggler_preflight(_needed):
        # Fires once, AFTER the closed set is seeded but BEFORE the copy loop —
        # exactly the window where a record-through recording escapes the seed.
        if not made["done"]:
            _write_recording(
                env.plaintext_root, "rec-straggler", {"recording.db": b"SSS"}
            )
            made["done"] = True
        return None

    summary = env.run(disk_preflight=_straggler_preflight)

    # The pass must NOT cutover: it detects the untracked straggler and re-drives.
    assert summary.state is MigrationState.RUNNING
    assert not env.aside.exists()
    # The straggler is intact in the plaintext root, seeded PENDING, NOT deleted.
    assert (
        env.plaintext_root / "rec-straggler" / "recording.db"
    ).read_bytes() == b"SSS"
    assert env.ledger.unit_status("rec-straggler") is UnitState.PENDING

    # Next pass: the straggler is copied+verified, then cutover + sweep — no loss.
    summary2 = env.run()
    assert summary2.state is MigrationState.COMPLETED
    view = env.container_view()
    assert (view / "rec-a" / "recording.db").read_bytes() == b"AAA"
    assert (view / "rec-straggler" / "recording.db").read_bytes() == b"SSS"
    assert not env.aside.exists()


def test_sweep_preserves_untracked_aside_entry(tmp_path):
    """FIX 1: the post-cutover sweep deletes ONLY ledger-tracked recordings and
    never blanket-rmtrees the aside root — an untracked directory under aside (a
    straggler a wholesale swap moved aside) is preserved, never destroyed."""
    env = _Env(tmp_path)
    env.aside.mkdir(parents=True)
    _write_recording(env.aside, "rec-a", {"recording.db": b"AAA"})  # tracked
    _write_recording(env.aside, "stray-rec", {"recording.db": b"ZZZ"})  # untracked
    env.ledger.seed(["rec-a"])
    env.ledger.mark("rec-a", UnitState.VERIFIED)
    env.ledger.set_run(
        phase=MigrationPhase.CUTOVER_DONE, aside_path=str(env.aside)
    )

    summary = env.run()  # resume fast-path → post-cutover sweep

    assert summary.state is MigrationState.COMPLETED
    # Tracked recording swept; untracked stray PRESERVED; aside root kept intact.
    assert not (env.aside / "rec-a").exists()
    assert (env.aside / "stray-rec" / "recording.db").read_bytes() == b"ZZZ"
    assert env.aside.exists()
    assert env.ledger.unit_status("rec-a") is UnitState.PLAINTEXT_DELETED


# ---------------------------------------------------------------------------
# FIX 4 — interrupted-cutover recovery must not write plaintext to a detached
# host path
# ---------------------------------------------------------------------------


def test_interrupted_cutover_after_detach_writes_no_host_plaintext(
    tmp_path, monkeypatch
):
    """FIX 4 (privacy): recovering a cutover that crashed AFTER the interim was
    detached must NOT re-copy the plaintext sidecars onto the bare (detached)
    interim host path. The sidecars are already inside the container; recovery
    skips the re-copy and leaves no plaintext residue on the host."""
    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    _make_index_db(env.sidecar_src / "content_index.db", ["row-A"])
    # The sidecars were already copied INTO the container before the crash+detach.
    (env.backing / ".store").mkdir(parents=True)
    _make_index_db(env.backing / ".store" / "content_index.db", ["row-A"])
    # Ledger: mid-CUTTING_OVER, rec-a verified, interim NOT attached (post-detach).
    env.ledger.seed(["rec-a"])
    env.ledger.mark("rec-a", UnitState.VERIFIED)
    env.ledger.set_run(
        phase=MigrationPhase.CUTTING_OVER, aside_path=str(env.aside)
    )
    assert not env.mounter.is_attached(env.interim)  # detached before the crash

    copied = {"n": 0}
    real = migration._copy_sidecars_into_container

    def _spy(paths, names):
        copied["n"] += 1
        return real(paths, names)

    monkeypatch.setattr(migration, "_copy_sidecars_into_container", _spy)

    summary = env.run()

    assert summary.state is MigrationState.COMPLETED
    # No sidecar re-copy attempted (it would leak plaintext to the detached host).
    assert copied["n"] == 0
    # No plaintext residue at the detached interim host path.
    assert not (env.interim / ".store").exists()
    # The container still carries the sidecar (copied before the crash).
    assert (env.container_view() / ".store" / "content_index.db").exists()


# ---------------------------------------------------------------------------
# FIX 5 (engine arm) — cutover halts at a ledger-safe boundary on a stop signal
# ---------------------------------------------------------------------------


def test_cutover_halts_at_safe_boundary_when_stopped(tmp_path):
    """FIX 5: a stop signalled as the cutover reservation opens halts the cutover
    at its ledger-safe boundary — no swap, no ``hdiutil`` on the mountpoint —
    leaving the run RUNNING (resumable). Proves a lock cannot race the swap."""
    import contextlib
    import threading

    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    stop = threading.Event()

    @contextlib.contextmanager
    def _reservation_that_locks():
        # Model a storage.lock arriving exactly as the cutover reservation opens.
        stop.set()
        yield

    summary = env.run(
        stop_event=stop, cutover_reservation=_reservation_that_locks
    )

    # Halted BEFORE the swap: phase stayed COPYING, run is RUNNING (resumable).
    assert summary.state is MigrationState.RUNNING
    assert env.ledger.phase() is MigrationPhase.COPYING
    # No swap hdiutil ran on the mountpoint: the interim was never detached, and
    # the container was never attached at the final mountpoint for the swap.
    assert env.mounter.detach_calls == []
    assert not env.mounter.is_attached(env.plaintext_root)
    assert (env.plaintext_root / "rec-a" / "recording.db").exists()
    assert should_auto_resume(env.ledger)
    # A fresh (unlocked) pass converges to COMPLETED.
    assert env.run().state is MigrationState.COMPLETED


# ---------------------------------------------------------------------------
# The job (mirrors BackfillJob) — idempotent start, cancel, status, pause/resume
# ---------------------------------------------------------------------------


def _bus():
    from screencap.daemon.event_bus import EventBus

    return EventBus()


def _job(tmp_path):
    from screencap.daemon.encrypt_job import EncryptJob

    return EncryptJob(_bus(), ledger=MigrationLedger(tmp_path / "job.db"))


async def _drain(bus, *, want: set[str], timeout: float = 2.0) -> list[str]:
    import asyncio

    sub = await bus.subscribe()
    seen: list[str] = []
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while not want.issubset(set(seen)):
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                ev = await asyncio.wait_for(sub.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            seen.append(ev.get("type"))
    finally:
        await bus.remove(sub)
    return seen


async def test_job_status_snapshot_is_name_free(tmp_path):
    from screencap.daemon.encrypt_job import EncryptStatusSnapshot

    job = _job(tmp_path)
    snap = job.status()
    assert isinstance(snap, EncryptStatusSnapshot)
    payload = snap.as_payload()
    allowed = {"state", "phase", "pending", "copied", "verified", "deleted",
               "total", "paused_reason"}
    assert set(payload) == allowed  # no recording name / path fields (R9)
    assert snap.state == "idle"


async def test_job_idempotent_start_and_completion_event(tmp_path):
    from screencap.daemon.encrypt_job import (
        EVENT_ENCRYPT_COMPACT_SUGGESTED,
        EVENT_ENCRYPT_COMPLETED,
    )

    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    from screencap.daemon.encrypt_job import EncryptJob

    bus = _bus()
    job = EncryptJob(bus, ledger=env.ledger)

    def _factory():
        def _run(**kwargs):
            return env.run(**kwargs)
        return _run

    # Idempotent: a second start while running returns the in-flight snapshot and
    # does not spawn a second task.
    job.start(_factory)
    task1 = job._task
    job.start(_factory)
    assert job._task is task1

    seen = await _drain(
        bus, want={EVENT_ENCRYPT_COMPLETED, EVENT_ENCRYPT_COMPACT_SUGGESTED}
    )
    assert EVENT_ENCRYPT_COMPLETED in seen
    # Completion emits the one-time storage-compact suggestion.
    assert EVENT_ENCRYPT_COMPACT_SUGGESTED in seen
    assert env.ledger.is_done()


async def test_job_lock_pause_at_boundary_then_unlock_resumes(tmp_path):
    """Lock during migration pauses the job at a recording boundary (shutdown
    sets the stop flag); unlock auto-resumes it to completion."""
    import asyncio
    import threading

    env = _Env(tmp_path)
    for i in range(3):
        _write_recording(env.plaintext_root, f"rec-{i}", {"recording.db": bytes([i])})

    from screencap.daemon.encrypt_job import EncryptJob

    bus = _bus()
    job = EncryptJob(bus, ledger=env.ledger)

    gate = threading.Event()

    def _factory():
        def _run(*, stop_event, progress_cb, ledger):
            # Halt at the first recording boundary once the test releases the gate,
            # so shutdown()'s stop flag is observed like the real engine's.
            gate.wait(timeout=2.0)
            return env.run(
                stop_event=stop_event, progress_cb=progress_cb, ledger=ledger
            )
        return _run

    job.start(_factory)
    # U9 lock pause hook: shutdown() sets the stop flag + awaits the worker.
    gate.set()
    await job.shutdown(timeout=3.0)
    assert not job.is_running()

    # Unlock auto-resume: resume_if_pending re-runs iff work remains.
    snap = job.resume_if_pending()
    if snap is not None:
        # Wait for the resumed run to finish.
        for _ in range(50):
            if not job.is_running():
                break
            await asyncio.sleep(0.05)
    assert env.ledger.is_done()
    assert not env.aside.exists()


async def test_job_cancel_marks_cancelled(tmp_path):
    import asyncio
    import threading

    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    from screencap.daemon.encrypt_job import EncryptJob

    bus = _bus()
    job = EncryptJob(bus, ledger=env.ledger)
    release = threading.Event()

    def _factory():
        def _run(*, stop_event, progress_cb, ledger):
            release.wait(timeout=2.0)
            return env.run(
                stop_event=stop_event, progress_cb=progress_cb, ledger=ledger
            )
        return _run

    job.start(_factory)
    job.cancel()  # sets the stop flag
    release.set()
    for _ in range(50):
        if not job.is_running():
            break
        await asyncio.sleep(0.05)
    assert env.ledger.run_state() is MigrationState.CANCELLED
    assert (env.plaintext_root / "rec-a").exists()  # plaintext intact on cancel


async def test_running_result_redrives_to_completed_and_emits_completed_once(
    tmp_path,
):
    """FIX 2: a RUNNING result (an active recording blocking cutover) is re-driven
    to COMPLETED on its OWN — no external daemon restart — and encrypt.completed is
    emitted EXACTLY ONCE, only at real completion (never for the RUNNING pass)."""
    import asyncio

    from screencap.daemon.encrypt_job import EVENT_ENCRYPT_COMPLETED, EncryptJob

    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    bus = _bus()
    job = EncryptJob(bus, ledger=env.ledger)
    job._redrive_delay = 0.01  # fast re-drive for the test

    calls = {"n": 0}

    def _factory():
        def _run(*, stop_event, progress_cb, ledger):
            calls["n"] += 1
            # First pass: an active recording blocks cutover → RUNNING. Later
            # passes: the recording finished → migrate + cutover + sweep.
            active = "rec-a" if calls["n"] == 1 else None
            return env.run(
                stop_event=stop_event,
                progress_cb=progress_cb,
                ledger=ledger,
                active_recording_name=lambda: active,
            )

        return _run

    sub = await bus.subscribe()
    job.start(_factory)
    for _ in range(300):
        if not job.is_running():
            break
        await asyncio.sleep(0.01)

    assert not job.is_running()
    assert env.ledger.is_done()
    assert calls["n"] >= 2  # it re-drove itself, with no external restart

    completed = 0
    while True:
        try:
            ev = sub.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if ev.get("type") == EVENT_ENCRYPT_COMPLETED:
            completed += 1
    await bus.remove(sub)
    assert completed == 1  # exactly once, only at real completion


async def test_running_then_lock_pause_never_emits_completed(tmp_path):
    """FIX 2: a run that returns RUNNING and is then paused (a lock ``shutdown``)
    must NEVER emit encrypt.completed; the ledger stays RUNNING (auto-resumable)."""
    import asyncio

    from screencap.daemon.encrypt_job import EVENT_ENCRYPT_COMPLETED, EncryptJob

    env = _Env(tmp_path)
    _write_recording(env.plaintext_root, "rec-a", {"recording.db": b"AAA"})
    bus = _bus()
    job = EncryptJob(bus, ledger=env.ledger)
    job._redrive_delay = 5.0  # long, so the loop parks between re-drives

    def _factory():
        def _run(*, stop_event, progress_cb, ledger):
            # Always RUNNING (a perpetual active recording), honoring stop.
            return env.run(
                stop_event=stop_event,
                progress_cb=progress_cb,
                ledger=ledger,
                active_recording_name=lambda: "rec-a",
            )

        return _run

    sub = await bus.subscribe()
    job.start(_factory)
    await asyncio.sleep(0.1)  # let the first RUNNING pass complete + park
    await job.shutdown(timeout=3.0)  # lock pause: set stop flag + await worker

    assert not job.is_running()
    # Interrupted → RUNNING (auto-resumes), NEVER flipped to COMPLETED.
    assert env.ledger.run_state() is MigrationState.RUNNING
    assert should_auto_resume(env.ledger)

    saw_completed = False
    while True:
        try:
            ev = sub.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if ev.get("type") == EVENT_ENCRYPT_COMPLETED:
            saw_completed = True
    await bus.remove(sub)
    assert not saw_completed


# ---------------------------------------------------------------------------
# The verbs: custom-recordings refusal (R16), audit record, name-free status.
# ---------------------------------------------------------------------------


def _build_app(monkeypatch, tmp_path):
    from screencap.daemon.app import build_app
    from screencap.daemon.supervisor import Supervisor

    app = build_app()
    app.state.store_state = "mounted"
    app.state.store_reason = None
    sup = Supervisor(app.state.event_bus, reconcile_on_init=False)
    app.state.supervisor = sup
    return app


def _asgi(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def audit_at(tmp_path, monkeypatch):
    from screencap.daemon import audit_log

    target = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    return target


async def test_encrypt_start_refuses_custom_recordings_install(
    tmp_path, monkeypatch, audit_at
):
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "custom-recs"))
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    import screencap.config as cfg
    cfg.invalidate_config_cache()

    app = _build_app(monkeypatch, tmp_path)
    async with _asgi(app) as client:
        resp = await client.post("/v0/storage.encrypt.start")

    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "storage_migration_failed"
    assert body["reason"] == "custom_recordings_dir"
    assert "custom recordings" in body["message"].lower()
    # Audit record written with peer provenance + the refusal outcome (KTD-23).
    lines = audit_at.read_text().splitlines()
    assert len(lines) == 1
    import json
    rec = json.loads(lines[0])
    assert rec["verb"] == "storage.encrypt.start"
    assert rec["outcome"] == "custom_recordings_dir"
    assert "classification" in rec and "peer_pid" in rec


async def test_encrypt_start_refuses_immutable_env_disabled_install(
    tmp_path, monkeypatch, audit_at
):
    """An env-forced plaintext process cannot be enabled by migration cutover."""
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "0")
    import screencap.config as cfg

    cfg.invalidate_config_cache()
    app = _build_app(monkeypatch, tmp_path)
    async with _asgi(app) as client:
        resp = await client.post("/v0/storage.encrypt.start")

    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "container_disabled"
    rec = __import__("json").loads(audit_at.read_text().splitlines()[-1])
    assert rec["outcome"] == "container_disabled"


async def test_encrypt_status_payload_is_name_free(tmp_path, monkeypatch):
    app = _build_app(monkeypatch, tmp_path)
    async with _asgi(app) as client:
        resp = await client.get("/v0/storage.encrypt.status")
    assert resp.status_code == 200
    body = resp.json()
    for forbidden in ("recording", "recording_name", "name", "path", "dir"):
        assert forbidden not in body
    assert set(body) >= {"state", "phase", "verified", "deleted", "total"}


@pytest.mark.parametrize("container_enabled", [False, True])
async def test_encrypt_start_success_audits_ok_and_starts_job(
    tmp_path, monkeypatch, audit_at, container_enabled
):
    """A non-custom install can opt in from plaintext or resume when enabled.

    ``container_enabled=false`` in mutable config is the upgrade case: clicking
    Encrypt is the explicit opt-in, and migration flips it only at safe cutover.
    """
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.delenv("SCREENCAP_CONTAINER_ENABLED", raising=False)
    import screencap.config as cfg

    # Force a NON-custom recordings dir: default resolution, no config override.
    recs = tmp_path / "recordings"
    monkeypatch.setattr(cfg, "_DEFAULT_RECORDINGS", recs)
    monkeypatch.setattr(
        cfg, "_load_toml", lambda: {"container_enabled": container_enabled}
    )
    cfg.invalidate_config_cache()

    import screencap.daemon.app as app_module

    # Mock the foreground bundle creation + the engine run so no hdiutil runs.
    monkeypatch.setattr(app_module, "_ensure_encrypt_bundle", lambda: None)

    def _fake_factory(app):
        def _factory():
            def _run(*, stop_event, progress_cb, ledger):
                ledger.seed([])
                ledger.set_run(
                    state=MigrationState.COMPLETED, phase=MigrationPhase.DONE
                )
                return ledger.summary()
            return _run
        return _factory

    monkeypatch.setattr(app_module, "_build_encrypt_run_factory", _fake_factory)

    app = _build_app(monkeypatch, tmp_path)
    # Give the job an isolated ledger so it doesn't touch the real base dir.
    from screencap.daemon.encrypt_job import EncryptJob
    app.state.encrypt_job = EncryptJob(
        app.state.event_bus, ledger=MigrationLedger(tmp_path / "verb.db")
    )

    async with _asgi(app) as client:
        resp = await client.post("/v0/storage.encrypt.start")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "state" in body
    lines = audit_at.read_text().splitlines()
    rec = __import__("json").loads(lines[-1])
    assert rec["verb"] == "storage.encrypt.start"
    assert rec["outcome"] == "ok"
