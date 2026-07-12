"""Catalog display-title resolution + user-set flag (U2).

Exercises how ``catalog.list_recordings`` resolves ``RecordingInfo.title`` /
``title_is_user_set``: a user-set rename (``recording.title``) wins; otherwise a
friendly date/time default derived from the capture start; and a legacy row with
no start timestamp falls back to the humanized directory name. A final invariant
test pins ``RecordingInfo`` and the daemon's ``RecordingSummary`` to the same
field set — the same parity the ``recording.list`` handler asserts at runtime.

Reuses the ``create_db`` + ``insert_recording`` DB-setup pattern from
``tests/test_recording_title_store.py`` and the temp recordings-dir listing
pattern from ``tests/test_catalog.py``. Vision-free.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from screencap.catalog import RecordingInfo, list_recordings
from screencap.recording_db import write_user_title


@pytest.fixture
def recordings_dir(tmp_path):
    return tmp_path / "recordings"


def _make_recording(base: Path, name: str, *, started_at: float) -> Path:
    """A recording dir with a real engine ``recording.db`` at a fixed start.

    Only the single authoritative ``recording`` row is needed to drive title
    resolution — ``list_recordings`` reads the start timestamp straight off it.
    A ``started_at`` of ``0.0`` models a legacy row with no usable start (the
    catalog reads it back as ``None``).
    """
    from screencap.engine.db import create_db, crud

    d = base / name
    d.mkdir(parents=True)

    engine, Session = create_db(str(d / "recording.db"))
    session = Session()
    crud.insert_recording(
        session,
        {
            "timestamp": started_at,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        },
    )
    session.close()
    engine.dispose()
    return d


def test_user_title_wins_and_flags_user_set(recordings_dir):
    """(a) A user-set rename becomes the title and marks ``title_is_user_set``."""
    d = _make_recording(
        recordings_dir, "rec-a", started_at=datetime(2026, 7, 12, 14, 30).timestamp()
    )
    write_user_title(d / "recording.db", "Stripe Webhook Debugging")

    info = list_recordings(recordings_dir)[0]
    assert info.title == "Stripe Webhook Debugging"
    assert info.title_is_user_set is True


def test_default_title_from_started_at(recordings_dir):
    """(b) With no user title, the title is the friendly start-time default.

    The label is ``"Recording · <Mon> <day>, <h>:<mm> <AM/PM>"`` — the hour's
    leading zero is stripped (14:30 -> ``2:30 PM``) and the day has no leading
    zero. ``timestamp()`` / ``fromtimestamp`` both use local time, so the round
    trip is timezone-independent; the month is derived via ``strftime`` so the
    assertion holds under any locale.
    """
    start = datetime(2026, 7, 12, 14, 30)
    d = _make_recording(recordings_dir, "rec-b", started_at=start.timestamp())

    info = list_recordings(recordings_dir)[0]
    assert info.title == f"Recording · {start.strftime('%b')} 12, 2:30 PM"
    assert info.title_is_user_set is False


def test_falls_back_to_humanized_dir_name_without_started_at(recordings_dir):
    """(c) No user title and no usable start -> humanized directory name."""
    _make_recording(recordings_dir, "stripe-webhook-debugging", started_at=0.0)

    info = list_recordings(recordings_dir)[0]
    assert info.title == "Stripe Webhook Debugging"
    assert info.title_is_user_set is False


def test_recording_info_and_summary_expose_same_field_set():
    """(d) ``RecordingInfo`` and ``RecordingSummary`` stay field-set identical.

    This is the exact invariant the daemon's ``recording.list`` handler asserts
    at runtime (the "fields diverged" RuntimeError). Pinning it here fails fast if
    ``title_is_user_set`` (or any future field) is added to one shape but not the
    other.
    """
    from screencap.daemon import schema

    assert set(RecordingInfo._fields) == set(schema.RecordingSummary.model_fields)
    # And the field under test is present on both.
    assert "title_is_user_set" in RecordingInfo._fields
    assert "title_is_user_set" in schema.RecordingSummary.model_fields
