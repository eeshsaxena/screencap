"""Tests for the ``LockPolicy`` seam (SCR-40, slice 4 of SCR-31).

Post-Phase-2 the daemon supervisor owns the process-exclusive pidfile
claim (see ``daemon/supervisor.py``); the engine subprocess runs with
``InheritLock`` — claim / register_children / release are no-ops, but
per-recording identity files are still written so downstream catalog /
upload / scrubber / recovery consumers find them.

These tests pin the seam-level behavioural contract for ``InheritLock``
and the shared ``_write_identity_files`` payload writer. The wiring
into ``_run_screen_recorder`` is exercised by
``test_screen_recorder_parity``.
"""

from __future__ import annotations

import json
from unittest import mock


def test_lock_policy_module_exposes_protocol_and_inherit_lock():
    """Smoke test: the module exposes ``LockPolicy`` + ``InheritLock``.

    ``InheritLock`` must be constructible with no args — ``RecordingPolicies``
    is built by the CLI adapter and the SessionController worker without
    any per-recording context at the construction site.
    """
    from screencap.engine.lock_policy import InheritLock, LockPolicy  # noqa: F401

    InheritLock()


def test_inherit_lock_claim_and_release_are_noops(tmp_path):
    """``InheritLock`` must not touch the process-exclusive lock.

    The daemon supervisor already claimed the pidfile at engine-spawn
    time; if the engine subprocess re-claimed it would self-deadlock,
    and if it released it would unlock the supervisor's claim early.
    Tested via mocks on the pidfile module surface — any non-zero
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
        policy.register_children(tmp_path, [{"pid": 1, "name": "x"}])
        policy.release()

    orphan_check.assert_not_called()
    claim.assert_not_called()
    write.assert_not_called()
    delete.assert_not_called()


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
    """Identity-file payload assertions for ``InheritLock.write_identity``."""
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


def test_inherit_lock_write_identity_writes_recording_id_and_intent(tmp_path):
    """``InheritLock.write_identity`` writes ``.recording_id`` and
    ``.recording_intent`` matching the wrapper-era payload schema.

    Per-recording identity is owned by the recording, not by who holds
    the process lock. The daemon's engine subprocess still needs its
    own ``.recording_id`` + ``.recording_intent`` for scrubbing, upload,
    and recovery to find them later.
    """
    from screencap.engine.lock_policy import InheritLock

    request = _make_request(
        name="my-rec", cloud_intent=True, keep_local=True, intent_source="flag",
        show_on_website=False,
    )
    InheritLock().write_identity(tmp_path, request=request, privacy_mode="public")

    _assert_identity_files(
        tmp_path,
        expected_name="my-rec",
        expected_destination="both",  # cloud_intent + keep_local
        expected_privacy_mode="public",
        expected_source="flag",
        expected_show_on_website=False,
    )


def test_write_identity_destination_cloud_only_when_keep_local_false(tmp_path):
    """``cloud_intent=True, keep_local=False`` → ``destination == "cloud"``."""
    from screencap.engine.lock_policy import InheritLock

    request = _make_request(name="cloud-only", cloud_intent=True, keep_local=False)
    InheritLock().write_identity(tmp_path, request=request, privacy_mode="internal")

    intent = json.loads((tmp_path / ".recording_intent").read_text())
    assert intent["destination"] == "cloud"


def test_write_identity_destination_local_when_no_cloud_intent(tmp_path):
    """``cloud_intent=False`` → ``destination == "local"`` (the default path)."""
    from screencap.engine.lock_policy import InheritLock

    request = _make_request(name="local-only", cloud_intent=False)
    InheritLock().write_identity(tmp_path, request=request, privacy_mode="internal")

    intent = json.loads((tmp_path / ".recording_intent").read_text())
    assert intent["destination"] == "local"
