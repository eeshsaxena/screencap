"""U5: user-set recording titles are searchable via the ``content.search`` union.

A renamed recording is surfaced by a term in its title even when the content
index is absent (the default — content indexing defaults OFF) or holds no frame
match for it, including privacy-blocked / never-indexed recordings (Acceptance
Example AE3). Only USER-SET titles are searchable — the derived date/time default
is not. Vision-free: titles are set directly via ``recording_db.write_user_title``,
never through OCR, so this runs in the CI privacy lane without Apple Vision.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_recording(
    base: Path,
    name: str,
    *,
    title: str | None = None,
    started: float = 1778198400.0,
) -> Path:
    """Create a minimal real recording.db catalog entry, optionally user-titled.

    ``title=None`` leaves the recording with only its derived date/time default
    (no user rename) — the case that must NOT be searchable.
    """
    from screencap.engine.db import create_db, crud

    rec_dir = base / name
    rec_dir.mkdir(parents=True)
    db_path = rec_dir / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(
        session,
        {
            "timestamp": started,
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
    if title is not None:
        from screencap.recording_db import write_user_title

        write_user_title(db_path, title)
    return rec_dir


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate the recordings dir and point the content-index path at tmp.

    The index path is returned but the file is NOT created — the default (no
    content index) — so a test opts INTO an index by writing frames to it.
    """
    import screencap.content_index as content_index

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings))
    index_path = tmp_path / "content_index.db"
    monkeypatch.setattr(content_index, "default_index_path", lambda: index_path)
    return recordings, index_path


def _search(query: str, recording: str | None = None, limit: int | None = 50) -> dict:
    """Drive the changed code path directly (the daemon's title-union search)."""
    from screencap.daemon.app import _run_content_search

    return _run_content_search(query, recording, limit)


@pytest.mark.privacy
def test_ae3_title_match_surfaces_without_content_index(isolated) -> None:
    """AE3: a title match returns even with no content index / no indexed frames.

    Covers both "no content index at all" (the default config) and a title hit's
    pointer-only shape (sentinel timestamp + ``match_source``).
    """
    recordings, index_path = isolated
    _make_recording(recordings, "rec-a", title="Quarterly Budget Review")

    result = _search("budget")

    # The content index never existed and the read never created an empty store.
    assert not index_path.exists()
    assert result["index_state"] == "not_indexed"

    hits = result["hits"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["recording"] == "rec-a"
    assert hit["snippet"] == "Quarterly Budget Review"
    # A title hit is NOT a frame pointer: the sentinel timestamp and match_source
    # steer a caller away from resolving it as one.
    assert hit["match_source"] == "title"
    assert hit["timestamp_ms"] == 0


@pytest.mark.privacy
def test_content_and_title_hits_deduped_by_recording(isolated) -> None:
    """When a content hit and a title hit both match, dedupe by recording.

    Also exercises the union path when a content index DOES exist: the richer
    content hit wins for a recording that has both, and a title-only recording is
    still unioned in.
    """
    import screencap.content_index as content_index

    recordings, index_path = isolated
    # rec-1 has BOTH a matching on-screen frame AND a matching user title.
    _make_recording(recordings, "rec-1", title="Budget deck")
    # rec-2 has only a matching user title (no indexed frame).
    _make_recording(recordings, "rec-2", title="Budget planning")

    with content_index.ContentIndex(index_path) as store:
        store.write_frames(
            "rec-1",
            [content_index.IndexFrame(timestamp_ms=90_000, text="the budget spreadsheet")],
        )

    hits = _search("budget")["hits"]
    recs = [h["recording"] for h in hits]

    # rec-1 appears exactly once — as the richer content hit (a real frame
    # pointer), NOT duplicated by its title.
    assert recs.count("rec-1") == 1
    by_rec = {h["recording"]: h for h in hits}
    assert set(by_rec) == {"rec-1", "rec-2"}
    assert by_rec["rec-1"]["match_source"] == "content"
    assert by_rec["rec-1"]["timestamp_ms"] == 90_000
    # rec-2 has no content hit, so its title-only hit is unioned in.
    assert by_rec["rec-2"]["match_source"] == "title"
    assert by_rec["rec-2"]["timestamp_ms"] == 0


@pytest.mark.privacy
def test_case_insensitive_match_and_recording_filter(isolated) -> None:
    """Matching is case-insensitive and the ``recording`` filter is honored."""
    recordings, _ = isolated
    _make_recording(recordings, "rec-standup", title="Weekly Standup Notes")
    _make_recording(recordings, "rec-other", title="Weekly Standup Notes")

    # Upper-case query term matches a mixed-case title (case-insensitive).
    all_hits = _search("STANDUP")["hits"]
    assert {h["recording"] for h in all_hits} == {"rec-standup", "rec-other"}

    # The recording filter restricts the union to a single recording.
    filtered = _search("standup", recording="rec-standup")["hits"]
    assert [h["recording"] for h in filtered] == ["rec-standup"]

    # A filter that names a recording with no title match yields nothing.
    assert _search("standup", recording="rec-missing")["hits"] == []


@pytest.mark.privacy
def test_derived_default_title_is_not_searchable(isolated) -> None:
    """A recording with only a DERIVED default title is not matched by its terms."""
    recordings, _ = isolated
    # No user rename → the catalog shows a derived "Recording · <date>" default.
    _make_recording(recordings, "rec-plain", title=None)

    # "Recording" is a word from that derived default label, but the default is
    # NOT a search source — only user-set titles are.
    assert _search("Recording")["hits"] == []
    # The directory name itself is likewise not a title match source.
    assert _search("plain")["hits"] == []


@pytest.mark.privacy
def test_title_hits_respect_the_requested_limit(isolated) -> None:
    """Title hits are capped to the requested limit.

    Title hits are appended after the ranked content hits, so without a cap the
    union could overrun the caller's page size. With no content index, five
    matching titles must still be capped to the requested limit.
    """
    recordings, _ = isolated
    for i in range(5):
        _make_recording(recordings, f"rec-{i}", title=f"Weekly Report {i}")

    hits = _search("report", limit=2)["hits"]
    assert len(hits) == 2
