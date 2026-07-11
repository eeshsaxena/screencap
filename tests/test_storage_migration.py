"""Tests for screencap.storage_migration (SCR-228 U2).

Data-integrity-critical: the crash/reconciliation and validation-rejection
paths are exercised first, then the happy move and permissions.
"""

import inspect
import json
import os
from pathlib import Path

import pytest

from screencap import storage_migration as sm
from screencap.storage_migration import (
    Reason,
    migrate,
    reconcile_pending,
    validate_target,
)


def _seed_library(root: Path) -> Path:
    """Create a recordings dir with one recording (db + chunk + screenshots)."""
    root.mkdir(parents=True, exist_ok=True)
    rec = root / "rec-1"
    (rec / "screenshots").mkdir(parents=True)
    (rec / "recording.db").write_text("SQLITE-LOCAL-ONLY")
    (rec / "chunk_0000.mp4").write_text("video-bytes")
    (rec / "screenshots" / "0001.jpg").write_text("jpg")
    return root


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)


# --- validation ---


class TestValidateTarget:
    def test_valid_same_volume_empty_target(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"  # non-existent, same volume
        assert validate_target(source, target).ok

    def test_env_override_rejected_first(self, tmp_path, monkeypatch):
        source = _seed_library(tmp_path / "recordings")
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "x"))
        res = validate_target(source, tmp_path / "new")
        assert not res.ok and res.code == Reason.ENV_OVERRIDE

    def test_source_missing(self, tmp_path):
        res = validate_target(tmp_path / "gone", tmp_path / "new")
        assert not res.ok and res.code == Reason.SOURCE_MISSING

    def test_same_as_source(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        res = validate_target(source, source)
        assert not res.ok and res.code == Reason.SAME_AS_SOURCE

    def test_target_nested_in_source(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        res = validate_target(source, source / "sub")
        assert not res.ok and res.code == Reason.NESTED

    def test_source_nested_in_target(self, tmp_path):
        source = _seed_library(tmp_path / "outer" / "recordings")
        res = validate_target(source, tmp_path / "outer")
        assert not res.ok and res.code == Reason.NESTED

    def test_target_not_empty(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"
        target.mkdir()
        (target / "stray.txt").write_text("x")
        res = validate_target(source, target)
        assert not res.ok and res.code == Reason.TARGET_NOT_EMPTY

    def test_empty_existing_target_ok(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"
        target.mkdir()  # empty, as a stray get_recordings_dir() read would
        assert validate_target(source, target).ok

    def test_cross_volume_rejected(self, tmp_path, monkeypatch):
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "external" / "recs"

        # Simulate a different filesystem for the target anchor.
        def fake_st_dev(path):
            return 1 if Path(path) == source.resolve() else 2

        monkeypatch.setattr(sm, "_st_dev", fake_st_dev)
        res = validate_target(source, target)
        assert not res.ok and res.code == Reason.CROSS_VOLUME

    def test_cloud_synced_rejected(self, tmp_path, monkeypatch):
        # Point HOME at a fake home with an active CloudStorage root.
        fake_home = tmp_path / "home"
        (fake_home / "Library" / "CloudStorage" / "Dropbox-Acme").mkdir(
            parents=True
        )
        monkeypatch.setenv("HOME", str(fake_home))
        source = _seed_library(fake_home / ".screencap" / "recordings")
        target = fake_home / "Library" / "CloudStorage" / "Dropbox-Acme" / "recs"
        res = validate_target(source, target)
        assert not res.ok and res.code == Reason.CLOUD_SYNCED


# --- migrate (happy + permissions) ---


class TestMigrate:
    def test_same_volume_move(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"
        committed = []

        outcome = migrate(
            source, target, committed.append, run_dir=tmp_path / "run"
        )

        assert outcome.ok
        assert not source.exists()
        assert target.exists()
        # Whole tree relocated, contents intact.
        assert (target / "rec-1" / "recording.db").read_text() == "SQLITE-LOCAL-ONLY"
        assert (target / "rec-1" / "chunk_0000.mp4").read_text() == "video-bytes"
        assert (target / "rec-1" / "screenshots" / "0001.jpg").exists()
        # Config flip is the commit point; called exactly once with the target.
        assert [Path(p).resolve() for p in committed] == [target.resolve()]
        # Breadcrumb cleared on success.
        assert not (tmp_path / "run" / sm.BREADCRUMB_NAME).exists()

    def test_new_root_is_0700(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"
        migrate(source, target, lambda p: None, run_dir=tmp_path / "run")
        assert oct(target.stat().st_mode)[-3:] == "700"

    def test_move_into_empty_existing_target(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"
        target.mkdir()  # empty existing dir (KTD-4 rmdir-then-rename path)
        outcome = migrate(source, target, lambda p: None, run_dir=tmp_path / "run")
        assert outcome.ok
        assert (target / "rec-1" / "recording.db").exists()

    def test_creates_missing_target_parent(self, tmp_path):
        # A headless CLI may pass a nested path whose parents don't exist yet.
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "a" / "b" / "new"
        outcome = migrate(source, target, lambda p: None, run_dir=tmp_path / "run")
        assert outcome.ok
        assert (target / "rec-1" / "recording.db").exists()

    def test_nonempty_target_returns_typed_failure_source_intact(self, tmp_path):
        # Target filled between validation and the move → clean typed outcome,
        # not a raw error, and the source is left untouched.
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"
        target.mkdir()
        (target / "stray").write_text("x")
        outcome = migrate(source, target, lambda p: None, run_dir=tmp_path / "run")
        assert not outcome.ok and outcome.code == Reason.TARGET_NOT_EMPTY
        assert (source / "rec-1" / "recording.db").exists()
        assert not (tmp_path / "run" / sm.BREADCRUMB_NAME).exists()

    def test_commit_config_failure_rolls_back_rename(self, tmp_path):
        # The config flip is the single commit point; if it raises, the atomic
        # move must roll back so config never points at a vanished source.
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"

        def failing_commit(_p):
            raise RuntimeError("config lock timeout")

        with pytest.raises(RuntimeError):
            migrate(source, target, failing_commit, run_dir=tmp_path / "run")

        assert (source / "rec-1" / "recording.db").read_text() == "SQLITE-LOCAL-ONLY"
        assert not target.exists()
        assert not (tmp_path / "run" / sm.BREADCRUMB_NAME).exists()

    def test_failed_root_hardening_rolls_back(self, tmp_path, monkeypatch):
        # A chmod that doesn't land 0o700 (silent failure on a world-traversable
        # parent) must fail the migration, not report success — verified by
        # re-stat, then rolled back.
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)  # chmod no-ops
        committed = []
        with pytest.raises(OSError):
            migrate(source, target, committed.append, run_dir=tmp_path / "run")
        assert (source / "rec-1").exists()  # rolled back
        assert not target.exists()
        assert committed == []  # config never flipped


# --- reconciliation (crash recovery) ---


class TestReconcile:
    def _write_breadcrumb(self, run_dir: Path, from_p: Path, to_p: Path):
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / sm.BREADCRUMB_NAME).write_text(
            json.dumps({"from": str(from_p), "to": str(to_p)})
        )

    def test_none_when_no_breadcrumb(self, tmp_path):
        assert reconcile_pending(lambda p: None, run_dir=tmp_path / "run") is None

    def test_rename_completed_converges_to_target(self, tmp_path):
        run_dir = tmp_path / "run"
        from_p = tmp_path / "recordings"  # renamed away (does not exist)
        to_p = _seed_library(tmp_path / "new")  # exists (rename done)
        self._write_breadcrumb(run_dir, from_p, to_p)

        committed = []
        outcome = reconcile_pending(committed.append, run_dir=run_dir)

        assert outcome.ok and outcome.moved_to == str(to_p)
        assert committed == [to_p]  # config forced to the new location
        assert not (run_dir / sm.BREADCRUMB_NAME).exists()

    def test_rename_never_happened_keeps_source(self, tmp_path):
        run_dir = tmp_path / "run"
        from_p = _seed_library(tmp_path / "recordings")  # still exists
        to_p = tmp_path / "new"  # never created
        self._write_breadcrumb(run_dir, from_p, to_p)

        committed = []
        outcome = reconcile_pending(committed.append, run_dir=run_dir)

        assert outcome.ok and outcome.moved_to == str(from_p)
        assert committed == []  # config already at source; not touched
        assert not (run_dir / sm.BREADCRUMB_NAME).exists()

    def test_both_exist_prefers_nonempty_target(self, tmp_path):
        # Crash between rename and config-flip left the real library at `to`;
        # a later get_recordings_dir() mkdir recreated an empty `from`. Reconcile
        # must converge on the non-empty `to`, not strand it by keeping `from`.
        run_dir = tmp_path / "run"
        from_p = tmp_path / "recordings"
        from_p.mkdir()  # empty, freshly recreated
        to_p = _seed_library(tmp_path / "new")  # the real moved library
        self._write_breadcrumb(run_dir, from_p, to_p)

        committed = []
        outcome = reconcile_pending(committed.append, run_dir=run_dir)

        assert outcome.moved_to == str(to_p)
        assert committed == [to_p]
        assert not (run_dir / sm.BREADCRUMB_NAME).exists()

    def test_idempotent(self, tmp_path):
        run_dir = tmp_path / "run"
        from_p = tmp_path / "recordings"
        to_p = _seed_library(tmp_path / "new")
        self._write_breadcrumb(run_dir, from_p, to_p)

        reconcile_pending(lambda p: None, run_dir=run_dir)
        # Second pass: breadcrumb already cleared → nothing to do.
        assert reconcile_pending(lambda p: None, run_dir=run_dir) is None


# --- R7: recording.db stays local-only across a move ---


class TestLocalOnlyInvariant:
    @pytest.mark.privacy
    def test_recording_db_moves_locally_and_no_cloud_seam(self, tmp_path):
        source = _seed_library(tmp_path / "recordings")
        target = tmp_path / "new"
        migrate(source, target, lambda p: None, run_dir=tmp_path / "run")

        # recording.db is present at the new LOCAL path, byte-identical, and
        # gone from the old location — a pure local relocation.
        moved_db = target / "rec-1" / "recording.db"
        assert moved_db.read_text() == "SQLITE-LOCAL-ONLY"
        assert not (source / "rec-1" / "recording.db").exists()

        # The migration engine must never IMPORT a cloud/upload seam (R8).
        # (Check import statements, not copy — user-facing messages legitimately
        # mention "upload" when warning about synced folders.)
        import_lines = "\n".join(
            line
            for line in inspect.getsource(sm).splitlines()
            if line.strip().startswith(("import ", "from "))
        ).lower()
        assert "upload" not in import_lines
        assert "gcs" not in import_lines
        assert "google" not in import_lines
