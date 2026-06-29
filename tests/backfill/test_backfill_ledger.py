"""Tests for the SCR-178 U3 backfill ledger (closed-set, resumable, cross-process).

The ledger is the on-disk source of truth for backfill progress: a run must be
genuinely resumable across a daemon restart / cancel / budget-pause, and "% done"
must be computed against a FROZEN denominator (no survivorship bias — the lesson
from ``chunk-upload-sentinel-gating-and-data-loss.md``). Every test injects a
``tmp_path`` DB so nothing touches the real ``~/.screencap/backfill_state.db``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from screencap.backfill.ledger import BackfillLedger, RunState, UnitStatus


def _ledger(tmp_path: Path) -> BackfillLedger:
    return BackfillLedger(tmp_path / "backfill_state.db")


def test_seed_marks_all_pending_with_frozen_denominator(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    units = [("rec-a", 0), ("rec-a", 1), ("rec-b", 0)]
    led.seed(units)

    assert led.progress() == (0, 0, 0, 3)  # done, skipped, failed, total

    led.mark("rec-a", 0, UnitStatus.DONE, rows_written=12)
    led.mark("rec-b", 0, UnitStatus.SKIPPED)

    done, skipped, failed, total = led.progress()
    assert (done, skipped, failed, total) == (1, 1, 0, 3)
    # Denominator stays N regardless of how many units are terminal.
    assert total == len(units)


def test_unit_status_reflects_marks_and_unseeded(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.seed([("rec", 0), ("rec", 1)])

    assert led.unit_status("rec", 0) == UnitStatus.PENDING
    led.mark("rec", 0, UnitStatus.DONE, rows_written=4)
    assert led.unit_status("rec", 0) == UnitStatus.DONE
    assert led.unit_status("rec", 1) == UnitStatus.PENDING
    # Not part of the seeded closed set → None (never widens the denominator).
    assert led.unit_status("rec", 99) is None
    assert led.unit_status("other", 0) is None


def test_resume_next_pending_and_is_complete(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.seed([("rec", 0), ("rec", 1), ("rec", 2)])

    # next_pending walks the un-terminal units in stable order.
    assert led.next_pending() == ("rec", 0)
    led.mark("rec", 0, UnitStatus.DONE, rows_written=3)
    assert led.next_pending() == ("rec", 1)
    led.mark("rec", 1, UnitStatus.SKIPPED)
    assert led.next_pending() == ("rec", 2)

    # is_complete flips only when EVERY unit is terminal.
    assert led.is_complete() is False
    led.mark("rec", 2, UnitStatus.FAILED, rows_written=0)
    assert led.next_pending() is None
    assert led.is_complete() is True
    # SKIPPED/FAILED are terminal but distinct from DONE.
    assert led.progress() == (1, 1, 1, 3)


def test_is_complete_false_on_empty_ledger(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    assert led.is_complete() is False


def test_seed_idempotent_no_denominator_drift_or_duplicates(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    units = [("rec-a", 0), ("rec-a", 1)]
    led.seed(units)
    led.mark("rec-a", 0, UnitStatus.DONE, rows_written=5)

    # Re-seeding the SAME closed set must not change the denominator, must not
    # duplicate rows, and must NOT reset the already-advanced unit to PENDING.
    led.seed(units)
    assert led.progress() == (1, 0, 0, 2)
    assert led.next_pending() == ("rec-a", 1)


def test_reset_then_seed_is_clean_replacement(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.seed([("old", 0), ("old", 1)])
    led.mark("old", 0, UnitStatus.DONE)
    led.set_run_state(RunState.CANCELLED)

    led.reset()
    assert led.progress() == (0, 0, 0, 0)
    assert led.run_state() is None

    led.seed([("new", 0)])
    assert led.progress() == (0, 0, 0, 1)
    assert led.next_pending() == ("new", 0)


def test_concurrent_instances_do_not_corrupt_state(tmp_path: Path) -> None:
    db = tmp_path / "backfill_state.db"
    a = BackfillLedger(db)
    b = BackfillLedger(db)  # separate connection, same file
    a.seed([("rec", 0), ("rec", 1)])

    # Two instances write transitions to the same DB; BEGIN IMMEDIATE +
    # busy_timeout keep them from corrupting each other.
    a.mark("rec", 0, UnitStatus.SKIPPED)
    b.mark("rec", 1, UnitStatus.DONE, rows_written=7)

    # SKIPPED never collapses into DONE.
    assert a.progress() == (1, 1, 0, 2)
    assert b.progress() == (1, 1, 0, 2)
    assert b.next_pending() is None


def test_crash_safety_pending_unit_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "backfill_state.db"
    led = BackfillLedger(db)
    led.seed([("rec", 0), ("rec", 1)])
    led.mark("rec", 0, UnitStatus.DONE)
    # Simulate an abort: drop the instance, reopen against the same file.
    del led

    reopened = BackfillLedger(db)
    assert reopened.next_pending() == ("rec", 1)
    assert reopened.is_complete() is False
    assert reopened.progress() == (1, 0, 0, 2)


def test_run_state_cancelled_vs_paused(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.seed([("rec", 0)])

    assert led.run_state() is None
    assert led.is_cancelled() is False

    led.set_run_state(RunState.CANCELLED)
    assert led.run_state() == RunState.CANCELLED
    assert led.is_cancelled() is True

    led.set_run_state(RunState.PAUSED)
    assert led.run_state() == RunState.PAUSED
    assert led.is_cancelled() is False
    # Run state is orthogonal to per-unit terminality.
    assert led.is_complete() is False
    led.mark("rec", 0, UnitStatus.DONE)
    assert led.is_complete() is True
    assert led.run_state() == RunState.PAUSED


def test_run_state_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "backfill_state.db"
    BackfillLedger(db).set_run_state(RunState.PAUSED)
    assert BackfillLedger(db).run_state() == RunState.PAUSED


def test_mark_on_unseeded_unit_is_noop(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.seed([("rec", 0)])
    # mark must never widen the closed set.
    led.mark("ghost", 9, UnitStatus.DONE)
    assert led.progress() == (0, 0, 0, 1)


def test_context_manager(tmp_path: Path) -> None:
    db = tmp_path / "backfill_state.db"
    with BackfillLedger(db) as led:
        led.seed([("rec", 0)])
        assert led.progress() == (0, 0, 0, 1)


def test_db_and_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "sub"
    db = parent / "backfill_state.db"
    led = BackfillLedger(db)
    led.seed([("rec", 0)])  # force a real write so the file exists

    assert (stat.S_IMODE(os.stat(db).st_mode)) == 0o600
    assert (stat.S_IMODE(os.stat(parent).st_mode)) == 0o700


def test_default_path_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from screencap import config

    monkeypatch.setattr(config, "get_base_dir", lambda: tmp_path)
    from screencap.backfill.ledger import default_ledger_path

    assert default_ledger_path() == tmp_path / "backfill_state.db"
