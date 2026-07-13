"""Tests for the ambient recording intent + per-day identity (SCR-214 U1).

Covers three seams:

1. The frozen ``.recording_intent`` payload — an ambient recording pins
   ``destination=local`` and freezes ``ambient: true`` regardless of the global
   ``upload_default``, and an ambient request carrying ``cloud_intent`` is
   rejected at the freeze point (local-only is an enforced invariant, KTD2).
2. The audio-on force — ambient forces the audio substream on so R3
   (screen + audio + transcript) holds.
3. The deterministic per-day capture dir — ``_allocate_capture_dir`` resolves
   ``ambient-YYYYMMDD`` and reopens the same dir on same-day re-entry (no ``-2``
   fork), forking only when the existing dir was already finalized.
"""

from __future__ import annotations

import json
import os
import time
from unittest import mock

import pytest


def _make_request(*, name="rec-1", ambient=False, cloud_intent=False, keep_local=True):
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import RecordingRequest

    return RecordingRequest(
        name=name,
        config=RecordingConfig(),
        cloud_intent=cloud_intent,
        keep_local=keep_local,
        ambient=ambient,
    )


# --- frozen intent payload -------------------------------------------------


def test_ambient_intent_round_trips_local(tmp_path):
    """An ambient recording freezes ``destination=local`` + ``ambient: true``."""
    from screencap import catalog
    from screencap.engine.lock_policy import InheritLock

    request = _make_request(name="ambient-20260713", ambient=True)
    InheritLock().write_identity(tmp_path, request=request, privacy_mode="internal")

    intent = json.loads((tmp_path / ".recording_intent").read_text())
    assert intent["destination"] == "local"
    assert intent["ambient"] is True

    # The catalog reader round-trips the frozen flag.
    assert catalog.read_ambient(tmp_path) is True
    assert catalog.read_intent(tmp_path) == "local"


def test_non_ambient_intent_freezes_ambient_false(tmp_path):
    """A normal recording freezes ``ambient: false``; the reader defaults safe."""
    from screencap import catalog
    from screencap.engine.lock_policy import InheritLock

    request = _make_request(name="explicit-rec", ambient=False)
    InheritLock().write_identity(tmp_path, request=request, privacy_mode="internal")

    intent = json.loads((tmp_path / ".recording_intent").read_text())
    assert intent["ambient"] is False
    assert catalog.read_ambient(tmp_path) is False


def test_read_ambient_defaults_false_when_field_absent(tmp_path):
    """A legacy intent without the ``ambient`` field reads as non-ambient."""
    from screencap import catalog

    (tmp_path / ".recording_intent").write_text(
        json.dumps({"version": 2, "destination": "local"})
    )
    assert catalog.read_ambient(tmp_path) is False
    # Missing file → False (not an error).
    assert catalog.read_ambient(tmp_path / "nope") is False


@pytest.mark.privacy
def test_ambient_under_upload_default_cloud_still_local(tmp_path):
    """Local-only is an ENFORCED invariant: even with ``upload_default=cloud`` in
    the environment, an ambient recording (built with ``cloud_intent=False``)
    freezes ``destination=local`` and carries no cloud intent — the always-on
    stream must never upload."""
    from screencap import catalog
    from screencap.engine.lock_policy import InheritLock

    request = _make_request(name="ambient-day", ambient=True, cloud_intent=False)
    with mock.patch.dict(os.environ, {"SCREENCAP_UPLOAD_DEFAULT": "cloud"}):
        InheritLock().write_identity(
            tmp_path, request=request, privacy_mode="public",
        )

    intent = json.loads((tmp_path / ".recording_intent").read_text())
    assert intent["destination"] == "local"
    assert intent["ambient"] is True
    assert catalog.read_ambient(tmp_path) is True


@pytest.mark.privacy
def test_ambient_request_with_cloud_intent_is_rejected(tmp_path):
    """An ambient request carrying ``cloud_intent=True`` is rejected at the freeze
    point — the assertion makes local-only enforced, not conventional (KTD2)."""
    from screencap.engine.lock_policy import InheritLock

    request = _make_request(name="bad-ambient", ambient=True, cloud_intent=True)
    with pytest.raises(ValueError, match="local-only"):
        InheritLock().write_identity(
            tmp_path, request=request, privacy_mode="public",
        )
    # Nothing durable was written for the rejected request.
    assert not (tmp_path / ".recording_intent").exists()


# --- audio-on force (R3) ---------------------------------------------------


def test_ambient_forces_audio_on_over_default_off():
    """Ambient forces the audio substream on even when ``audio_default`` is off
    and the request explicitly asked for no audio (R3)."""
    from screencap.engine.screen_recorder import resolve_capture_audio

    # Ambient overrides an explicit ``requested=False`` and a ``default=False``.
    assert resolve_capture_audio(ambient=True, requested=False, default=False) is True
    assert resolve_capture_audio(ambient=True, requested=None, default=False) is True


def test_non_ambient_audio_honors_request_then_default():
    from screencap.engine.screen_recorder import resolve_capture_audio

    assert resolve_capture_audio(ambient=False, requested=False, default=True) is False
    assert resolve_capture_audio(ambient=False, requested=True, default=False) is True
    assert resolve_capture_audio(ambient=False, requested=None, default=True) is True
    assert resolve_capture_audio(ambient=False, requested=None, default=False) is False


# --- deterministic per-day capture dir -------------------------------------


class _AllocOnly:
    """Bind only the dir-allocation methods off ``Supervisor`` — they use no
    instance state beyond ``get_recordings_dir()``, so a full Supervisor (with
    its event loop + reconcile) is unnecessary to exercise them."""

    from screencap.daemon.supervisor import Supervisor as _Sup

    _allocate_capture_dir = _Sup._allocate_capture_dir
    _allocate_ambient_capture_dir = _Sup._allocate_ambient_capture_dir


def _ambient_request():
    from screencap.daemon import schema

    return schema.RecordingStartRequest(ambient=True)


def test_allocate_ambient_dir_is_deterministic_per_day(tmp_path):
    sup = _AllocOnly()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        name, path = sup._allocate_capture_dir(_ambient_request())

    expected = time.strftime("ambient-%Y%m%d")
    assert name == expected
    assert path == tmp_path / expected


def test_allocate_ambient_dir_reopens_same_day_no_fork(tmp_path):
    """Same-day re-entry over an unfinalized dir reopens it — never ``-2``."""
    sup = _AllocOnly()
    expected = time.strftime("ambient-%Y%m%d")
    # An existing, unfinalized per-day dir (a daemon restart mid-day).
    (tmp_path / expected).mkdir()

    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        name, path = sup._allocate_capture_dir(_ambient_request())

    assert name == expected  # reopened, not forked
    assert path == tmp_path / expected


def test_allocate_ambient_dir_forks_when_finalized(tmp_path):
    """A finalized day dir (completeness sentinel present) is NOT reopened —
    reopening would corrupt the frozen ledger — so a suffixed dir is forked."""
    sup = _AllocOnly()
    expected = time.strftime("ambient-%Y%m%d")
    day_dir = tmp_path / expected
    day_dir.mkdir()
    # Terminal-stage completeness sentinel → the day is finalized.
    (day_dir / "recording_complete.json").write_text("{}")

    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        name, path = sup._allocate_capture_dir(_ambient_request())

    assert name == f"{expected}-2"
    assert path == tmp_path / f"{expected}-2"


def test_allocate_ambient_dir_finalized_via_frozen_chunks_expected(tmp_path):
    """Finalization is also detected via a frozen ``chunks_expected`` in
    ``recording.db`` (the local ambient path never writes the cloud sentinel)."""
    import sqlite3

    sup = _AllocOnly()
    expected = time.strftime("ambient-%Y%m%d")
    day_dir = tmp_path / expected
    day_dir.mkdir()
    db = day_dir / "recording.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, chunks_expected INTEGER)")
    conn.execute("INSERT INTO recording (id, chunks_expected) VALUES (1, 5)")
    conn.commit()
    conn.close()

    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        name, _ = sup._allocate_capture_dir(_ambient_request())

    assert name == f"{expected}-2"


def test_allocate_ambient_dir_reopens_when_chunks_expected_null(tmp_path):
    """An in-progress dir (``chunks_expected`` still NULL / unfrozen) is reopened."""
    import sqlite3

    sup = _AllocOnly()
    expected = time.strftime("ambient-%Y%m%d")
    day_dir = tmp_path / expected
    day_dir.mkdir()
    db = day_dir / "recording.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, chunks_expected INTEGER)")
    conn.execute("INSERT INTO recording (id, chunks_expected) VALUES (1, NULL)")
    conn.commit()
    conn.close()

    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        name, _ = sup._allocate_capture_dir(_ambient_request())

    assert name == expected  # reopened, not forked
