"""Tests for the Unit 3 PID-file flock mutex (claim_lock / release_lock /
read_lock_metadata / lock_is_active)."""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from screencap import pidfile


@pytest.fixture(autouse=True)
def _isolate_lock(tmp_path, monkeypatch):
    """Redirect LOCK_DIR / LOCK_FILE / PID_FILE to tmp_path and reset module state."""
    lock_dir = tmp_path / "run"
    monkeypatch.setattr(pidfile, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(pidfile, "LOCK_FILE", lock_dir / "recording.lock")
    monkeypatch.setattr(pidfile, "PID_FILE", tmp_path / "recording.pid")
    # Drop any leaked module-global lock state from a prior test
    if pidfile._LOCKED_FD is not None:
        try:
            pidfile.release_lock()
        except Exception:
            pass
        pidfile._LOCKED_FD = None
    yield
    if pidfile._LOCKED_FD is not None:
        try:
            pidfile.release_lock()
        except Exception:
            pass
        pidfile._LOCKED_FD = None


class TestClaimLock:
    def test_first_claim_succeeds(self, tmp_path):
        fd = pidfile.claim_lock(tmp_path / "cap", claimant="cli")
        assert isinstance(fd, int)
        assert pidfile.LOCK_FILE.exists()

    def test_second_claim_in_same_process_is_noop(self, tmp_path):
        fd1 = pidfile.claim_lock(tmp_path / "cap", claimant="cli")
        fd2 = pidfile.claim_lock(tmp_path / "cap", claimant="cli")
        assert fd1 == fd2

    def test_metadata_written_on_claim(self, tmp_path):
        capture = tmp_path / "cap"
        pidfile.claim_lock(capture, claimant="swiftui")
        meta = pidfile.read_lock_metadata()
        assert meta is not None
        assert meta["pid"] == os.getpid()
        assert meta["capture_dir"] == str(capture)
        assert meta["claimant"] == "swiftui"
        assert "started_at" in meta

    def test_release_clears_module_state(self, tmp_path):
        pidfile.claim_lock(tmp_path / "cap")
        assert pidfile._LOCKED_FD is not None
        pidfile.release_lock()
        assert pidfile._LOCKED_FD is None

    def test_release_when_not_held_is_noop(self):
        pidfile.release_lock()  # must not raise

    def test_claim_lock_accepts_none_capture_dir(self, tmp_path):
        """Post-todo-014: SessionController claims with capture_dir=None at
        controller-init time (the per-recording dir isn't allocated yet);
        update_lock_metadata() plumbs the real dir in later. Lock metadata
        must persist `capture_dir: null`, not the cwd or the literal string
        \"None\"."""
        pidfile.claim_lock(None, claimant="swiftui")
        meta = pidfile.read_lock_metadata()
        assert meta is not None
        assert meta["capture_dir"] is None
        assert meta["claimant"] == "swiftui"

    def test_update_lock_metadata_writes_per_recording_dir(self, tmp_path):
        """update_lock_metadata replaces the placeholder capture_dir with the
        per-recording directory once allocated, without disturbing the other
        metadata fields (pid / started_at / claimant)."""
        pidfile.claim_lock(None, claimant="swiftui")
        meta_before = pidfile.read_lock_metadata()
        assert meta_before["capture_dir"] is None

        rec_dir = tmp_path / "rec-1"
        ok = pidfile.update_lock_metadata(rec_dir)
        assert ok is True

        meta_after = pidfile.read_lock_metadata()
        assert meta_after["capture_dir"] == str(rec_dir)
        # Other metadata preserved.
        assert meta_after["pid"] == meta_before["pid"]
        assert meta_after["started_at"] == meta_before["started_at"]
        assert meta_after["claimant"] == "swiftui"

    def test_update_lock_metadata_returns_false_when_not_held(self):
        """No lock held → update is a safe no-op returning False (callers
        don't need to track held-state)."""
        assert pidfile.update_lock_metadata("/tmp/whatever") is False

    def test_claim_lock_initializes_recording_fields_to_none(self, tmp_path):
        """Per-recording state starts null. SessionController claims the lock
        at __init__; the per-recording fields stay null until _on_start_click
        plumbs them in via update_lock_metadata. status --json must read
        is_recording from these fields, not from lock_is_active."""
        pidfile.claim_lock(None, claimant="cli")
        meta = pidfile.read_lock_metadata()
        assert meta["recording_started_at"] is None
        assert meta["recording_name"] is None
        # Controller-init started_at is still set (distinct field).
        assert isinstance(meta["started_at"], float)

    def test_update_lock_metadata_sets_per_recording_fields(self, tmp_path):
        """Passing recording_started_at + recording_name plumbs them in
        without touching pid / claimant / controller-init started_at."""
        pidfile.claim_lock(None, claimant="swiftui")
        controller_started_at = pidfile.read_lock_metadata()["started_at"]

        rec_dir = tmp_path / "rec-A"
        ok = pidfile.update_lock_metadata(
            rec_dir,
            recording_started_at=controller_started_at + 5.0,
            recording_name="rec-A",
        )
        assert ok is True

        meta = pidfile.read_lock_metadata()
        assert meta["capture_dir"] == str(rec_dir)
        assert meta["recording_started_at"] == controller_started_at + 5.0
        assert meta["recording_name"] == "rec-A"
        # Controller-init fields preserved.
        assert meta["started_at"] == controller_started_at
        assert meta["claimant"] == "swiftui"

    def test_update_lock_metadata_accepts_optional_started_by(self, tmp_path):
        """started_by is additive provenance for daemon-routed sessions."""
        pidfile.claim_lock(None, claimant="daemon")

        ok = pidfile.update_lock_metadata(tmp_path / "rec-A", started_by="swiftui-via-daemon")

        assert ok is True
        meta = pidfile.read_lock_metadata()
        assert meta["started_by"] == "swiftui-via-daemon"
        assert meta["claimant"] == "daemon"

    def test_update_lock_metadata_back_to_back_recordings_refresh_started_at(self, tmp_path):
        """The bug this fixes: previously update_lock_metadata only mutated
        capture_dir, so back-to-back recordings reported the same elapsed
        time (the controller-init timestamp) instead of refreshing on each
        new capture."""
        pidfile.claim_lock(None, claimant="cli")

        # First recording.
        pidfile.update_lock_metadata(
            tmp_path / "rec-1",
            recording_started_at=1000.0,
            recording_name="rec-1",
        )
        assert pidfile.read_lock_metadata()["recording_started_at"] == 1000.0

        # Second recording in the same controller — must refresh.
        pidfile.update_lock_metadata(
            tmp_path / "rec-2",
            recording_started_at=2000.0,
            recording_name="rec-2",
        )
        meta = pidfile.read_lock_metadata()
        assert meta["recording_started_at"] == 2000.0
        assert meta["recording_name"] == "rec-2"
        assert meta["capture_dir"] == str(tmp_path / "rec-2")

    def test_clear_lock_recording_nulls_per_recording_fields(self, tmp_path):
        """_on_stop_click calls clear_lock_recording so status --json reports
        is_recording=false immediately, even though the controller still
        holds the flock."""
        pidfile.claim_lock(None, claimant="cli")
        pidfile.update_lock_metadata(
            tmp_path / "rec-1",
            recording_started_at=1000.0,
            recording_name="rec-1",
        )

        ok = pidfile.clear_lock_recording()
        assert ok is True

        meta = pidfile.read_lock_metadata()
        assert meta["capture_dir"] is None
        assert meta["recording_started_at"] is None
        assert meta["recording_name"] is None
        # Controller fields preserved — the flock is still held.
        assert isinstance(meta["pid"], int)
        assert meta["claimant"] == "cli"
        assert isinstance(meta["started_at"], float)

    def test_clear_lock_recording_returns_false_when_not_held(self):
        assert pidfile.clear_lock_recording() is False


class TestStopForceTargetsLockOwner:
    """`screencap stop --force` must SIGKILL the lock owner directly.
    Previously it skipped the lock-owner path (gated by `if not force:`)
    and only scanned orphans — but find_orphaned_processes() returns []
    while the SessionController parent is alive (pidfile.py:107). Result:
    `stop --force` against a hung-but-alive controller did nothing.
    """

    def test_stop_force_kills_lock_owner_via_metadata(self, tmp_path, monkeypatch):
        """Hold the lock from a child process; verify `screencap stop --force`
        sends SIGKILL to that child's PID. Uses CliRunner + lock-metadata
        path; doesn't actually spawn a recorder."""
        import multiprocessing
        import time as _time
        from click.testing import CliRunner

        from screencap.cli import cli

        # Pre-create the lock dir so the child can open the file.
        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)

        ctx = multiprocessing.get_context("spawn")
        started_q = ctx.Queue()
        # Hold the lock with our test claimant for a long time so the
        # parent stop call can target the child.
        proc = ctx.Process(
            target=_hold_lock_with_claimant,
            args=(pidfile.LOCK_FILE, pidfile.LOCK_DIR, started_q, "cli", 30.0),
        )
        proc.start()
        try:
            assert started_q.get(timeout=5) == "ready"
            child_pid = proc.pid

            # _is_screencap_process inspects cmdline for "screencap" — our
            # child's cmdline is the python interpreter running the test
            # subprocess target, which won't match. Stub it to True for
            # this child PID so the stop command's safety check passes.
            from screencap import pidfile as _pidfile_module
            original_is_screencap = _pidfile_module._is_screencap_process

            def _is_screencap_stub(pid):
                if pid == child_pid:
                    return True
                return original_is_screencap(pid)

            monkeypatch.setattr(_pidfile_module, "_is_screencap_process", _is_screencap_stub)
            # Force prose output so the existing assertion on PID text
            # holds — CliRunner makes stdout non-TTY which would otherwise
            # auto-flip ``stop --json`` (todo 009). We're testing the
            # process side here, not the JSON envelope.
            from screencap import cli as _cli_module
            monkeypatch.setattr(
                _cli_module, "_should_default_to_json", lambda: False,
            )

            runner = CliRunner()
            result = runner.invoke(cli, ["stop", "--force"], catch_exceptions=False)
            assert result.exit_code == 0
            # Force-kill output should reference the lock-owner PID.
            assert f"PID {child_pid}" in result.output or "Force-killing" in result.output

            # Wait for the kernel to reap.
            for _ in range(20):
                if not proc.is_alive():
                    break
                _time.sleep(0.1)
            assert not proc.is_alive(), "child should have been SIGKILLed"
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)

    def test_pid_recycle_guard_skips_when_create_time_too_late(self, tmp_path, monkeypatch):
        """Todo 017: when ``psutil.Process.create_time()`` is more than the
        tolerance window after the lock metadata's ``started_at``, the guard
        nulls ``parent_pid`` and falls through to the orphan scan instead of
        SIGTERM/SIGKILL-ing what could be a recycled, unrelated PID.

        Without this branch under test, a regression that inverts the
        comparison (`<` instead of `>`) would not fail any test today.
        """
        import multiprocessing
        import time as _time
        from click.testing import CliRunner

        from screencap.cli import cli

        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        ctx = multiprocessing.get_context("spawn")
        started_q = ctx.Queue()
        proc = ctx.Process(
            target=_hold_lock_with_claimant,
            args=(pidfile.LOCK_FILE, pidfile.LOCK_DIR, started_q, "cli", 30.0),
        )
        proc.start()
        try:
            assert started_q.get(timeout=5) == "ready"
            child_pid = proc.pid

            from screencap import pidfile as _pidfile_module
            monkeypatch.setattr(
                _pidfile_module, "_is_screencap_process",
                lambda pid: pid == child_pid,
            )
            from screencap import cli as _cli_module
            monkeypatch.setattr(
                _cli_module, "_should_default_to_json", lambda: False,
            )

            # Force the recycle-detected branch by reporting a fabricated
            # create_time well past the 60s tolerance (todo 014 widened
            # this window — we still need to exceed it).
            real_meta = _pidfile_module.read_lock_metadata()
            assert real_meta is not None
            recycled_create_time = float(real_meta["started_at"]) + 1000.0

            import psutil
            real_process = psutil.Process

            def _wrapped_process(pid, *a, **kw):
                # Selective override — only intercept the targeted PID so
                # ``process_iter`` (used by find_orphaned_processes) keeps
                # working for every other process on the system.
                proc = real_process(pid, *a, **kw)
                if pid == child_pid:
                    proc.create_time = lambda: recycled_create_time
                return proc

            monkeypatch.setattr(psutil, "Process", _wrapped_process)

            runner = CliRunner()
            result = runner.invoke(cli, ["stop", "--force"], catch_exceptions=False)
            assert result.exit_code == 0
            # The recycle-detected branch should have nulled parent_pid;
            # the child must STILL be alive (we never sent SIGKILL).
            assert "appears recycled" in result.output
            assert proc.is_alive(), "recycle guard fired but process was killed anyway"
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)

    def test_pid_recycle_guard_skips_on_psutil_no_such_process(self, tmp_path, monkeypatch):
        """Todo 017: if psutil cannot find the recorded PID, the guard
        falls through to the orphan scan with a nulled parent_pid rather
        than risking a kill against an unrelated process."""
        from click.testing import CliRunner

        from screencap.cli import cli

        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        # Manually plant a lock metadata entry that points at a PID that
        # almost certainly doesn't exist.
        if pidfile._LOCKED_FD is not None:
            pidfile.release_lock()
        pidfile.claim_lock(tmp_path / "cap")
        try:
            import psutil
            real_process = psutil.Process
            recorded_pid = pidfile.read_lock_metadata()["pid"]

            def _wrapped_process(pid, *a, **kw):
                if pid == recorded_pid:
                    raise psutil.NoSuchProcess(pid)
                return real_process(pid, *a, **kw)

            monkeypatch.setattr(psutil, "Process", _wrapped_process)

            runner = CliRunner()
            result = runner.invoke(cli, ["stop"], catch_exceptions=False)
            # Should exit cleanly — no parent to stop, no orphans either.
            assert result.exit_code == 0
        finally:
            if pidfile._LOCKED_FD is not None:
                pidfile.release_lock()

    def test_flock_oserror_does_not_leak_fd(self, monkeypatch, tmp_path):
        """Todo 008: any flock failure that isn't BlockingIOError must close
        the fd opened on the previous line. Without this, NFS EOPNOTSUPP /
        virtual-FS EINVAL leak the fd for the lifetime of the process."""
        import fcntl as _fcntl

        # Reset to clean state
        if pidfile._LOCKED_FD is not None:
            pidfile.release_lock()

        opened_fds = []
        original_open = os.open

        def _tracking_open(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened_fds.append(fd)
            return fd

        monkeypatch.setattr(os, "open", _tracking_open)

        def _flock_eio(*args, **kwargs):
            raise OSError(5, "simulated EIO from flock")

        monkeypatch.setattr(_fcntl, "flock", _flock_eio)

        with pytest.raises(OSError, match="simulated"):
            pidfile.claim_lock(tmp_path / "cap")

        # No leaked fd: each opened fd was closed by the OSError-handling path.
        for fd in opened_fds:
            with pytest.raises(OSError):
                os.fstat(fd)


class TestLockIsActiveReadOnly:
    def test_lock_is_active_works_with_readonly_open(self, tmp_path, monkeypatch):
        """Todo 028: lock_is_active probe opens RDONLY so a future
        permission-hardening pass (root-owned + group-readable lockfile)
        doesn't silently break detection. Verify the probe still works
        when O_RDWR would have failed."""
        import errno

        if pidfile._LOCKED_FD is not None:
            pidfile.release_lock()

        # Hold the lock from this process so the probe should report active.
        pidfile.claim_lock(tmp_path / "cap")
        try:
            # Sanity: probe reports active under normal conditions.
            assert pidfile.lock_is_active() is True

            # Now monkey-patch os.open to fail on O_RDWR but succeed on O_RDONLY,
            # simulating a permission-restricted lockfile.
            original_open = os.open

            def _restrict_rdwr(path, flags, *args, **kwargs):
                if flags & os.O_RDWR:
                    raise PermissionError(errno.EACCES, "simulated RDWR denial")
                return original_open(path, flags, *args, **kwargs)

            monkeypatch.setattr(os, "open", _restrict_rdwr)

            # Probe must still succeed via O_RDONLY — would return False if
            # we had stayed on O_RDWR.
            assert pidfile.lock_is_active() is True
        finally:
            pidfile.release_lock()


class TestResetForTests:
    def test_reset_clears_state(self, tmp_path):
        """Todo 029: _reset_for_tests is the documented contract for tests
        that need to claim_lock without inheriting prior state."""
        pidfile.claim_lock(tmp_path / "cap")
        assert pidfile._LOCKED_FD is not None

        pidfile._reset_for_tests()
        assert pidfile._LOCKED_FD is None

    def test_reset_is_idempotent(self):
        """No held lock → reset is a safe no-op."""
        if pidfile._LOCKED_FD is not None:
            pidfile.release_lock()
        pidfile._reset_for_tests()  # must not raise
        pidfile._reset_for_tests()  # call twice — still safe


class TestReadLockMetadata:
    def test_missing_file_returns_none(self):
        assert pidfile.read_lock_metadata() is None

    def test_corrupt_content_returns_none(self):
        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        pidfile.LOCK_FILE.write_text("not json{{{")
        assert pidfile.read_lock_metadata() is None

    def test_valid_content_returns_dict(self, tmp_path):
        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"pid": 99, "claimant": "cli", "capture_dir": str(tmp_path), "started_at": 1.0}
        pidfile.LOCK_FILE.write_text(json.dumps(payload))
        assert pidfile.read_lock_metadata() == payload


class TestLockIsActive:
    def test_no_file_inactive(self):
        assert pidfile.lock_is_active() is False

    def test_held_lock_is_active(self, tmp_path):
        pidfile.claim_lock(tmp_path / "cap")
        assert pidfile.lock_is_active() is True

    def test_released_lock_is_inactive(self, tmp_path):
        pidfile.claim_lock(tmp_path / "cap")
        pidfile.release_lock()
        # File still exists with stale content but flock is free
        assert pidfile.LOCK_FILE.exists()
        assert pidfile.lock_is_active() is False

    def test_stale_content_with_no_holder_inactive(self):
        # Simulate a file left over from a process that died abruptly (kernel
        # released the flock; file content remains).
        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        pidfile.LOCK_FILE.write_text(json.dumps({"pid": 99999, "claimant": "cli"}))
        assert pidfile.lock_is_active() is False


# --- Cross-process contention --------------------------------------------- #


def _claim_in_child(lock_path, started_q, hold_seconds):
    """Subprocess target: claim the lock at ``lock_path``, signal, hold, exit.

    Uses raw ``fcntl.flock`` — NOT ``pidfile.claim_lock`` — because the
    parent test fixture monkey-patches ``pidfile.LOCK_FILE`` to a tmp_path
    location, but the patch doesn't survive ``multiprocessing.spawn``
    re-import. The child would otherwise lock a different path
    (``~/.screencap/run/recording.lock``) and the parent's contention check
    would never fire (false negative). Production ``claim_lock`` metadata-
    write is covered by ``test_metadata_written_on_claim`` intra-process.
    """
    import fcntl as _fcntl
    import os as _os

    fd = _os.open(str(lock_path), _os.O_RDWR | _os.O_CREAT, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        started_q.put("contended")
        return
    started_q.put("acquired")
    time.sleep(hold_seconds)
    # Process exit auto-releases the flock.


class TestCrossProcessMutex:
    def test_second_process_sees_contention(self, tmp_path):
        """When a child holds the flock, a parent claim_lock raises LockContended."""
        ctx = multiprocessing.get_context("spawn")
        started_q: multiprocessing.Queue = ctx.Queue()
        proc = ctx.Process(
            target=_claim_in_child,
            args=(pidfile.LOCK_FILE, started_q, 2.0),
        )
        # Pre-create dir so the child can open the file
        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        proc.start()
        try:
            assert started_q.get(timeout=5) == "acquired"
            with pytest.raises(pidfile.LockContended):
                pidfile.claim_lock(tmp_path / "cap")
        finally:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)

    def test_lock_released_after_holder_dies(self, tmp_path):
        """Kernel releases the flock when the holder process exits."""
        ctx = multiprocessing.get_context("spawn")
        started_q: multiprocessing.Queue = ctx.Queue()
        proc = ctx.Process(
            target=_claim_in_child,
            args=(pidfile.LOCK_FILE, started_q, 0.1),
        )
        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        proc.start()
        try:
            assert started_q.get(timeout=5) == "acquired"
            proc.join(timeout=5)
            # Now the child has exited; we should be able to claim cleanly.
            fd = pidfile.claim_lock(tmp_path / "cap")
            assert isinstance(fd, int)
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)


def _race_claim_in_child(lock_path, lock_dir, result_q):
    """Subprocess target for a 2-process race: try to claim, report outcome."""
    # Re-import in the child (spawn mode = fresh interpreter)
    from pathlib import Path as _Path

    from screencap import pidfile as _pf

    _pf.LOCK_DIR = _Path(str(lock_dir))
    _pf.LOCK_FILE = _Path(str(lock_path))
    _pf._LOCKED_FD = None
    try:
        _pf.claim_lock(_Path("/tmp/cap"))
        # Hold briefly so the racer gets a stable contended view
        time.sleep(0.3)
        result_q.put("won")
    except _pf.LockContended:
        result_q.put("lost")


def _hold_lock_with_claimant(lock_path, lock_dir, started_q, claimant, hold_seconds):
    """Subprocess target: claim with a specific claimant, signal ready, hold."""
    from pathlib import Path as _Path

    from screencap import pidfile as _pf

    _pf.LOCK_DIR = _Path(str(lock_dir))
    _pf.LOCK_FILE = _Path(str(lock_path))
    _pf._LOCKED_FD = None
    _pf.claim_lock(_Path("/tmp/cap"), claimant=claimant)
    started_q.put("ready")
    time.sleep(hold_seconds)


class TestRaceCondition:
    def test_n_concurrent_starts_exactly_one_wins(self, tmp_path):
        """Race verification: 10 concurrent claim_lock attempts → exactly 1 winner."""
        ctx = multiprocessing.get_context("spawn")
        result_q: multiprocessing.Queue = ctx.Queue()
        n = 10
        procs = []
        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        for _ in range(n):
            p = ctx.Process(
                target=_race_claim_in_child,
                args=(pidfile.LOCK_FILE, pidfile.LOCK_DIR, result_q),
            )
            procs.append(p)

        # Start them all together
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=10)

        outcomes = []
        while not result_q.empty():
            outcomes.append(result_q.get_nowait())

        won_count = outcomes.count("won")
        lost_count = outcomes.count("lost")
        assert won_count == 1, f"expected 1 winner, got {won_count} (outcomes: {outcomes})"
        assert won_count + lost_count == n, f"some processes neither won nor lost: {outcomes}"


class TestContendedExceptionPayload:
    def test_owner_carries_existing_metadata(self, tmp_path):
        """LockContended.owner reflects the active holder's metadata."""
        ctx = multiprocessing.get_context("spawn")
        started_q: multiprocessing.Queue = ctx.Queue()

        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        proc = ctx.Process(
            target=_hold_lock_with_claimant,
            args=(pidfile.LOCK_FILE, pidfile.LOCK_DIR, started_q, "swiftui", 2.0),
        )
        proc.start()
        try:
            assert started_q.get(timeout=5) == "ready"
            with pytest.raises(pidfile.LockContended) as exc_info:
                pidfile.claim_lock(tmp_path / "cap")
            owner = exc_info.value.owner
            assert owner.get("claimant") == "swiftui"
            assert owner.get("pid") == proc.pid
        finally:
            proc.terminate()
            proc.join(timeout=5)
