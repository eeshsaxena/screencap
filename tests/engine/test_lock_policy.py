"""Tests for the ``LockPolicy`` seam (SCR-40, slice 4 of SCR-31).

Promotes the ``_skip_pidfile``-gated lock + identity bundle in
``_run_screen_recorder`` to a pluggable policy. Two implementations:

* ``ClaimLock``    — standalone CLI: orphan preflight, ``claim_lock``,
  identity files, ``write_pidfile`` after children spawn,
  ``delete_pidfile`` on release.
* ``InheritLock``  — session worker: skips claim/preflight/pidfile
  (parent ``SessionController`` owns the lock), but still writes per-
  recording identity files.

These tests pin the seam-level behavioural contract. The wiring into
``_run_screen_recorder`` is exercised by ``test_screen_recorder_parity``.
"""

from __future__ import annotations

import json
from unittest import mock


def test_lock_policy_module_exposes_protocol_and_impls():
    """Smoke test: the module exists with the three documented names.

    Both ``ClaimLock`` and ``InheritLock`` must be constructible with no
    args — ``RecordingPolicies`` is built by the CLI adapter and the
    SessionController worker without any per-recording context at the
    construction site.
    """
    from screencap.engine.lock_policy import ClaimLock, InheritLock, LockPolicy  # noqa: F401

    ClaimLock()
    InheritLock()


def test_inherit_lock_claim_and_release_are_noops(tmp_path):
    """``InheritLock`` must not touch the process-exclusive lock.

    Workers run inside the ``SessionController`` process tree; the parent
    has already claimed at ``__init__``. If a worker re-claimed it would
    self-deadlock; if it released it would unlock the parent's session
    early. Tested via mocks on the pidfile module surface — any non-zero
    interaction is a regression.
    """
    from screencap.engine.lock_policy import InheritLock

    policy = InheritLock()

    with (
        mock.patch("screencap.pidfile.find_orphaned_processes") as orphan_check,
        mock.patch("screencap.pidfile.claim_lock") as claim,
        mock.patch("screencap.pidfile.write_pidfile") as write,
        mock.patch("screencap.pidfile.delete_pidfile") as delete,
    ):
        policy.claim(tmp_path, force_clean=False)
        policy.register_children(tmp_path, [{"pid": 1234, "name": "fake"}])
        policy.release()

    orphan_check.assert_not_called()
    claim.assert_not_called()
    write.assert_not_called()
    delete.assert_not_called()


def test_claim_lock_happy_path_runs_orphan_check_then_claims(tmp_path):
    """``ClaimLock.claim`` (no orphans) calls ``find_orphaned_processes``
    then ``claim_lock(capture_dir, claimant=...)``.

    Pins the order: orphan check first (so we can fail with exit 1 before
    holding any kernel resources), then the flock claim. Standalone CLI
    callers depend on this ordering — flipping it would let a contended
    second invocation hide an orphaned-pid mess from the user.
    """
    from screencap.engine.lock_policy import ClaimLock

    with (
        mock.patch(
            "screencap.pidfile.find_orphaned_processes", return_value=[],
        ) as orphan_check,
        mock.patch("screencap.pidfile.claim_lock") as claim,
        mock.patch("screencap.pidfile.terminate_processes") as terminate,
    ):
        ClaimLock().claim(tmp_path, force_clean=False)

    orphan_check.assert_called_once_with()
    claim.assert_called_once()
    args, kwargs = claim.call_args
    # capture_dir is the positional arg
    assert args[0] == tmp_path
    assert "claimant" in kwargs
    terminate.assert_not_called()


def test_claim_lock_orphans_without_force_clean_raises_systemexit_1(tmp_path):
    """Orphans + ``force_clean=False`` → ``SystemExit(1)`` with no claim attempt.

    Mirrors the wrapper-era behaviour: tell the user to run
    ``screencap stop`` or pass ``--force``, exit code 1, do not silently
    take over a stale pidfile (could be a real recording the user forgot
    about).
    """
    import pytest

    from screencap.engine.lock_policy import ClaimLock

    fake_orphans = [{"pid": 9999, "name": "screencap"}]
    with (
        mock.patch(
            "screencap.pidfile.find_orphaned_processes",
            return_value=fake_orphans,
        ),
        mock.patch("screencap.pidfile.claim_lock") as claim,
        mock.patch("screencap.pidfile.terminate_processes") as terminate,
    ):
        with pytest.raises(SystemExit) as excinfo:
            ClaimLock().claim(tmp_path, force_clean=False)

    assert excinfo.value.code == 1
    claim.assert_not_called()
    terminate.assert_not_called()


def test_claim_lock_orphans_with_force_clean_terminates_and_proceeds(tmp_path):
    """Orphans + ``force_clean=True`` → terminate, delete stale pidfile, claim."""
    from screencap.engine.lock_policy import ClaimLock

    fake_orphans = [{"pid": 9999, "name": "screencap"}]
    with (
        mock.patch(
            "screencap.pidfile.find_orphaned_processes",
            return_value=fake_orphans,
        ),
        mock.patch("screencap.pidfile.claim_lock") as claim,
        mock.patch("screencap.pidfile.terminate_processes") as terminate,
        mock.patch("screencap.pidfile.delete_pidfile") as delete,
    ):
        ClaimLock().claim(tmp_path, force_clean=True)

    terminate.assert_called_once_with(fake_orphans, force=True)
    # The stale pidfile is removed before re-claiming so the new owner
    # gets a clean slate.
    delete.assert_called_once_with()
    claim.assert_called_once()


def test_claim_lock_lock_contended_raises_systemexit_2(tmp_path):
    """``LockContended`` → emit stderr ``lock_contended`` event + ``SystemExit(2)``.

    Exit code 2 is the load-bearing contract for SwiftUI / SessionController
    callers (see ``tests/test_start_lock_contention.py``). The stderr
    event is the structured signal SwiftUI parses; ``cli.py`` translates
    the exit code.
    """
    import pytest

    from screencap.engine.lock_policy import ClaimLock
    from screencap.pidfile import LockContended

    owner_meta = {"pid": 4242, "claimant": "cli"}
    with (
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch(
            "screencap.pidfile.claim_lock",
            side_effect=LockContended(owner=owner_meta),
        ),
        mock.patch("screencap._stderr_events.emit_event") as emit,
    ):
        with pytest.raises(SystemExit) as excinfo:
            ClaimLock().claim(tmp_path, force_clean=False)

    assert excinfo.value.code == 2
    emit.assert_called_once()
    args, kwargs = emit.call_args
    # First positional is the event-name constant ("lock_contended").
    from screencap._stderr_events import EVENT_LOCK_CONTENDED
    assert args[0] == EVENT_LOCK_CONTENDED
    assert kwargs.get("owner") == owner_meta


def _make_request(
    *,
    name: str = "rec-1",
    cloud_intent: bool = False,
    keep_local: bool = True,
    intent_source: str = "flag",
    show_on_website: bool = True,
):
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import RecordingRequest

    return RecordingRequest(
        name=name,
        config=RecordingConfig(),
        cloud_intent=cloud_intent,
        keep_local=keep_local,
        intent_source=intent_source,
        show_on_website=show_on_website,
    )


def _assert_identity_files(capture_dir, *, expected_name, expected_destination,
                           expected_privacy_mode, expected_source,
                           expected_show_on_website):
    """Both ``ClaimLock`` and ``InheritLock`` write the same identity payload."""
    rid = (capture_dir / ".recording_id").read_text()
    assert rid == expected_name

    intent_raw = (capture_dir / ".recording_intent").read_text()
    intent = json.loads(intent_raw)
    assert intent["version"] == 1
    assert intent["destination"] == expected_destination
    assert intent["privacy_mode"] == expected_privacy_mode
    assert intent["show_on_website"] is expected_show_on_website
    assert intent["source"] == expected_source
    # ``created_at`` is timestamp-laden; assert presence + non-empty only.
    assert intent.get("created_at")


def test_claim_lock_write_identity_writes_recording_id_and_intent(tmp_path):
    """``ClaimLock.write_identity`` writes ``.recording_id`` and
    ``.recording_intent`` matching the wrapper-era payload schema."""
    from screencap.engine.lock_policy import ClaimLock

    request = _make_request(
        name="my-rec", cloud_intent=True, keep_local=True, intent_source="flag",
        show_on_website=False,
    )
    ClaimLock().write_identity(tmp_path, request=request, privacy_mode="public")

    _assert_identity_files(
        tmp_path,
        expected_name="my-rec",
        expected_destination="both",  # cloud_intent + keep_local
        expected_privacy_mode="public",
        expected_source="flag",
        expected_show_on_website=False,
    )


def test_inherit_lock_write_identity_writes_same_identity_files(tmp_path):
    """``InheritLock.write_identity`` writes the same files as ``ClaimLock``.

    Per-recording identity is owned by the recording, not by who holds
    the process lock. Workers under ``SessionController`` still need
    their own ``.recording_id`` + ``.recording_intent`` for scrubbing,
    upload, and recovery to find them later.
    """
    from screencap.engine.lock_policy import InheritLock

    request = _make_request(name="worker-rec", cloud_intent=False)
    InheritLock().write_identity(tmp_path, request=request, privacy_mode="internal")

    _assert_identity_files(
        tmp_path,
        expected_name="worker-rec",
        expected_destination="local",  # cloud_intent=False
        expected_privacy_mode="internal",
        expected_source="flag",
        expected_show_on_website=True,
    )


def test_write_identity_destination_cloud_only_when_keep_local_false(tmp_path):
    """``cloud_intent=True, keep_local=False`` → ``destination == "cloud"``."""
    from screencap.engine.lock_policy import ClaimLock

    request = _make_request(name="cloud-only", cloud_intent=True, keep_local=False)
    ClaimLock().write_identity(tmp_path, request=request, privacy_mode="internal")

    intent = json.loads((tmp_path / ".recording_intent").read_text())
    assert intent["destination"] == "cloud"


def test_claim_lock_register_children_writes_pidfile(tmp_path):
    """``ClaimLock.register_children`` calls ``write_pidfile``.

    The wrapper writes the pidfile *after* children spawn so SIGTERM
    cleanup can target the actual reader threads / writer processes,
    not the bare parent. This pin keeps that ordering observable to
    the seam.
    """
    from screencap.engine.lock_policy import ClaimLock

    children = [{"pid": 4242, "name": "writer"}, {"pid": 4243, "name": "reader"}]
    with mock.patch("screencap.pidfile.write_pidfile") as write:
        ClaimLock().register_children(tmp_path, children)

    write.assert_called_once_with(tmp_path, children)


def test_claim_lock_release_deletes_pidfile():
    """``ClaimLock.release`` calls ``delete_pidfile`` (matches the legacy
    ``finally`` block; required so a subsequent ``screencap start`` does
    not see a stale pidfile)."""
    from screencap.engine.lock_policy import ClaimLock

    with mock.patch("screencap.pidfile.delete_pidfile") as delete:
        ClaimLock().release()

    delete.assert_called_once_with()
