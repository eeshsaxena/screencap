"""Local-only guard for the editable title (KTD6 / SCR-223).

The user-set ``recording.title`` lives only in ``recording.db``, which is
local-only by rule (R8) and never uploaded. This pins that invariant so a future
change that routes the title into a cloud-bound path — or weakens the
``recording.db`` upload exclusion — fails a test rather than shipping a leak.
STRATEGY tracks "leaked titles in cloud-bound paths" as a privacy metric, so
this is the regression gate that keeps it at zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from screencap.recording_db import write_user_title
from screencap.upload import FileInfo, assert_uploadable, list_recording_files

pytestmark = pytest.mark.privacy

# A title containing content that would be a genuine leak if it escaped.
_SENSITIVE_TITLE = "Payroll SSN review 123-45-6789"


def _make_recording_with_title(rec_dir: Path, title: str) -> Path:
    """A recording dir with a title-bearing recording.db plus an uploadable file."""
    from screencap.engine.db import create_db, crud

    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"
    _engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(
        session,
        {
            "timestamp": 1000.0,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        },
    )
    session.close()
    write_user_title(db_path, title)
    # A normal, genuinely-uploadable artifact so the recording isn't empty.
    (rec_dir / "manifest.json").write_text('{"chunk": 0}')
    return db_path


def test_title_bearing_recording_db_is_excluded_from_upload(tmp_path):
    rec_dir = tmp_path / "rec-20260712T143000"
    _make_recording_with_title(rec_dir, _SENSITIVE_TITLE)

    files = list_recording_files(rec_dir)
    names = {f.name for f in files}

    # recording.db (the title's only home) and its sidecars never appear.
    assert "recording.db" not in names
    assert not any(n.startswith("recording.db") for n in names)
    # The genuinely-uploadable file is still listed (the exclusion is targeted).
    assert "manifest.json" in names


def test_assert_uploadable_rejects_recording_db(tmp_path):
    db_path = tmp_path / "recording.db"
    db_path.write_bytes(b"")
    fi = FileInfo(
        name="recording.db",
        path=db_path,
        content_type="application/octet-stream",
        size=0,
    )
    with pytest.raises(ValueError):
        assert_uploadable(fi)


def test_no_uploadable_file_contains_the_title(tmp_path):
    rec_dir = tmp_path / "rec-20260712T143000"
    _make_recording_with_title(rec_dir, _SENSITIVE_TITLE)

    # Every file the upload path would enqueue must be free of the title string:
    # the title lives only in the excluded recording.db.
    for f in list_recording_files(rec_dir):
        blob = f.path.read_bytes()
        assert _SENSITIVE_TITLE.encode() not in blob, f"title leaked into {f.name}"
