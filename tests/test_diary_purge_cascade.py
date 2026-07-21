"""U5 — retroactive-disable purge cascade reaches the new day-diary sinks.

Extends the SCR-280 task-segment purge (whose row-level tests live in
``tests/segmentation/test_ondevice_pipeline.py``) so a "disable this app" action
also strips the disabled app's traces from the three day-diary sinks (AE5 / R13):

* bullets — including on a user-RENAMED block: the field-scoped protection split
  (KTD-3) keeps the user's NAME but purges the agent-written bullets in place;
* the day-narrative row — invalidated so the next tick recomposes it from the
  surviving blocks;
* ``diary_fts`` — the block name/bullet search rows in ``content_index.db``.

Characterization-first: the SCR-280 whole-row-delete + user/edited protection is
pinned unchanged before the field-scoped additions are asserted.

Reuses the real ``recording.db`` + Spotify-target fixture from the on-device
pipeline suite. Vision-free + privacy-marked so the CI ``pytest -m privacy`` lane
runs it. The content index is redirected to a per-test temp store — NEVER the real
``~/.screencap/content_index.db``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import screencap.content_index as ci
from screencap.content_index import ContentIndex, write_recording_diary
from screencap.enforcement.scrub_worker import ScrubWorker
from screencap.pipeline_state import (
    EDITED_FIELD_BULLETS,
    PipelineLedger,
    TaskSegmentRow,
    _parse_wire_bullets,
)

# Reuse the on-device suite's real-recording.db fixture + Spotify-target seeding
# (the same fixture the SCR-280 task-segment purge tests use).
from tests.segmentation.test_ondevice_pipeline import _make_rec, _seed_spotify_target

pytestmark = pytest.mark.privacy


# The Spotify target's active interval from ``_seed_spotify_target`` is
# ~[1298, 1400) (event at 1300 with the 2s URL-lag prelude, closed by the next
# benign window at 1400). Blocks straddling it overlap; the bookends do not.
_OVERLAP = (1300.0, 1350.0)
_BEFORE = (1000.0, 1100.0)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_content_index(tmp_path, monkeypatch):
    """Redirect the global content-index path (store + write-lock file) at a temp
    store so the scrub's ``purge_content_index_intervals`` NEVER touches the real
    ``~/.screencap/content_index.db``. Returned for tests that seed the diary."""
    store = tmp_path / "content_index.db"
    monkeypatch.setattr(ci, "default_index_path", lambda: store)
    return store


def _spotify_disable(worker: ScrubWorker) -> dict:
    entry = worker._handle({
        "kind": "app", "bundle_id": "com.spotify.client", "app_name": "Spotify",
        "root_domain": None, "ts_unix": 2000.0, "source": "menubar",
    })
    assert entry["scrub_status"] == "completed"
    return entry


def _worker(rec) -> ScrubWorker:
    return ScrubWorker(
        disable_q=None, recording_db_path=rec.db_path, capture_dir=rec.dir,
    )


def _meta(bullets: list[str], **extra) -> str:
    return json.dumps({"bullets": list(bullets), **extra})


def _rows_by_name(db_path: Path) -> dict[str, TaskSegmentRow]:
    return {r.name: r for r in PipelineLedger(db_path).read_task_segments()}


def _diary_block_ids(store_path: Path, term: str) -> set[str]:
    with ContentIndex(store_path) as store:
        return {h.block_id for h in store.search_diary(term, limit=500).hits}


# --------------------------------------------------------------------------
# Characterization — the SCR-280 behavior I must NOT regress
# --------------------------------------------------------------------------


def test_characterization_whole_row_delete_and_user_protection(tmp_path):
    """CURRENT behavior (pinned): a retroactive disable whole-row-DELETEs the
    unprotected agent row overlapping the interval, SKIPS (preserves the row of) a
    user-renamed agent block and a user-created block, and leaves a non-overlapping
    agent row alone. This holds before AND after the U5 field-scoped additions."""
    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=_BEFORE[0], end_ts=_BEFORE[1],
                       name="Before", block_id="blk-before"),
        TaskSegmentRow(task_index=1, start_ts=_OVERLAP[0], end_ts=_OVERLAP[1],
                       name="Overlapping agent", block_id="blk-agent"),
        TaskSegmentRow(task_index=2, start_ts=1305.0, end_ts=1345.0,
                       name="Overlapping renamed", block_id="blk-renamed"),
    ])
    # Rename block 2 → protected (name edited); re-homes into the HIGH range.
    ledger.update_task_segment(2, name="My focus block", mark_edited=True)
    # A user-created block overlapping the target.
    ledger.insert_task_segment(
        TaskSegmentRow(task_index=0, start_ts=1305.0, end_ts=1345.0, name="User block")
    )
    _seed_spotify_target(rec.db_path)

    _spotify_disable(_worker(rec))

    names = set(_rows_by_name(rec.db_path))
    # Unprotected overlapping agent row gone; everything protected / out-of-range
    # survives (row-level SCR-280 invariant).
    assert names == {"Before", "My focus block", "User block"}


# --------------------------------------------------------------------------
# AE5 — field-scoped bullet purge (KTD-3): renamed block keeps name, loses bullets
# --------------------------------------------------------------------------


def test_renamed_block_agent_bullets_purged_but_name_kept(tmp_path):
    """A user-RENAMED block over the disabled span keeps its NAME (name-protected)
    but its AGENT-written bullets are purged in place — the AE5 clause the
    field-scoped split (KTD-3) enables. The unprotected agent block is whole-row
    deleted as before."""
    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=_OVERLAP[0], end_ts=_OVERLAP[1],
                       name="Spotify listening",
                       metadata=_meta(["queued a playlist"], block_id="blk-agent"),
                       block_id="blk-agent"),
        TaskSegmentRow(task_index=1, start_ts=1305.0, end_ts=1345.0,
                       name="Music while coding",
                       metadata=_meta(["listened to the focus mix"], block_id="blk-renamed"),
                       block_id="blk-renamed"),
    ])
    # Rename only → EDITED_FIELD_NAME set, bullets stay agent-owned.
    ledger.update_task_segment(1, name="Deep work session", mark_edited=True)
    _seed_spotify_target(rec.db_path)

    _spotify_disable(_worker(rec))

    rows = _rows_by_name(rec.db_path)
    # Unprotected agent row: whole-row deleted.
    assert "Spotify listening" not in rows
    # Renamed block: NAME kept, bullets gone.
    kept = rows["Deep work session"]
    assert _parse_wire_bullets(kept.metadata) is None
    # The rename protection itself is untouched (still a kept/edited row).
    assert kept.edited is True


# --------------------------------------------------------------------------
# Protection — user-authored bullets survive the purge untouched
# --------------------------------------------------------------------------


def test_user_edited_and_user_created_bullets_survive(tmp_path):
    """A block whose BULLETS the user edited (``EDITED_FIELD_BULLETS``) and a
    ``source='user'`` block keep their bullets through the purge — user-authored
    content is never touched (AE5 protection half)."""
    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=_OVERLAP[0], end_ts=_OVERLAP[1],
                       name="Bullet edited",
                       metadata=_meta(["agent draft"]), block_id="blk-bulleted"),
    ])
    # Edit the bullets → EDITED_FIELD_BULLETS set (bullets user-owned now).
    new_idx = ledger.update_task_segment(
        0, metadata=_meta(["my own bullet"]), mark_edited=True
    )
    # A user-created block over the same span.
    ledger.insert_task_segment(
        TaskSegmentRow(task_index=0, start_ts=1305.0, end_ts=1345.0,
                       name="User note", metadata=_meta(["I wrote this note"]))
    )
    _seed_spotify_target(rec.db_path)

    # Sanity: the bullet-edit really flagged the bullets bit.
    edited_row = next(
        r for r in ledger.read_task_segments() if r.task_index == new_idx
    )
    assert edited_row.edited_fields & EDITED_FIELD_BULLETS

    _spotify_disable(_worker(rec))

    rows = _rows_by_name(rec.db_path)
    assert _parse_wire_bullets(rows["Bullet edited"].metadata) == ["my own bullet"]
    assert _parse_wire_bullets(rows["User note"].metadata) == ["I wrote this note"]


# --------------------------------------------------------------------------
# Narrative-row invalidation
# --------------------------------------------------------------------------


def test_day_narrative_invalidated_when_blocks_purged(tmp_path):
    """When the purge touches any block row, the stored day-narrative row is
    cleared so the next tick recomposes it from the surviving blocks (it may still
    describe the disabled app otherwise)."""
    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=_OVERLAP[0], end_ts=_OVERLAP[1],
                       name="Spotify listening",
                       metadata=_meta(["queued a playlist"]), block_id="blk-agent"),
    ])
    ledger.set_day_narrative(
        "Today you listened to Spotify playlists while coding.", "fp-1", None
    )
    assert ledger.get_day_narrative() is not None
    _seed_spotify_target(rec.db_path)

    _spotify_disable(_worker(rec))

    assert ledger.get_day_narrative() is None, "stale narrative survived the purge"


def test_day_narrative_untouched_when_no_blocks_touched(tmp_path):
    """A disable that overlaps NO block rows leaves the narrative alone (the
    narrative derives from blocks; nothing changed)."""
    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    # The only block is BEFORE the target interval → not touched by the purge.
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=_BEFORE[0], end_ts=_BEFORE[1],
                       name="Morning triage", block_id="blk-before"),
    ])
    ledger.set_day_narrative("A calm morning of triage.", "fp-1", None)
    _seed_spotify_target(rec.db_path)

    _spotify_disable(_worker(rec))

    assert ledger.get_day_narrative() is not None
    assert ledger.get_day_narrative().narrative == "A calm morning of triage."


# --------------------------------------------------------------------------
# diary_fts row deletion
# --------------------------------------------------------------------------


def test_diary_fts_rows_purged_for_disabled_interval(tmp_path, _hermetic_content_index):
    """The disabled span's block name/bullet text vanishes from ``diary_fts``
    search; a non-overlapping block stays searchable."""
    store = _hermetic_content_index
    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=_BEFORE[0], end_ts=_BEFORE[1],
                       name="Morning email",
                       metadata=_meta(["triaged the alphainbox queue"]),
                       block_id="blk-morning"),
        TaskSegmentRow(task_index=1, start_ts=_OVERLAP[0], end_ts=_OVERLAP[1],
                       name="Music break",
                       metadata=_meta(["queued the zephyrplaylist mix"]),
                       block_id="blk-spotify"),
    ])
    write_recording_diary(rec.dir.name, rec.db_path)
    # Baseline: both blocks are indexed and searchable.
    assert _diary_block_ids(store, "zephyrplaylist") == {"blk-spotify"}
    assert _diary_block_ids(store, "alphainbox") == {"blk-morning"}

    _seed_spotify_target(rec.db_path)
    _spotify_disable(_worker(rec))

    assert _diary_block_ids(store, "zephyrplaylist") == set(), "disabled block still searchable"
    assert _diary_block_ids(store, "alphainbox") == {"blk-morning"}


# --------------------------------------------------------------------------
# Idempotence — a second disable is a no-op
# --------------------------------------------------------------------------


def test_purge_twice_is_a_noop(tmp_path, _hermetic_content_index):
    """Running the disable twice leaves the same post-purge state: the surviving
    rows, cleared narrative, and purged diary rows are unchanged on the 2nd pass."""
    store = _hermetic_content_index
    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=_BEFORE[0], end_ts=_BEFORE[1],
                       name="Morning email",
                       metadata=_meta(["triaged the alphainbox queue"]),
                       block_id="blk-morning"),
        TaskSegmentRow(task_index=1, start_ts=_OVERLAP[0], end_ts=_OVERLAP[1],
                       name="Music break",
                       metadata=_meta(["queued the zephyrplaylist mix"]),
                       block_id="blk-spotify"),
    ])
    write_recording_diary(rec.dir.name, rec.db_path)
    ledger.set_day_narrative("A day.", "fp-1", None)
    _seed_spotify_target(rec.db_path)

    worker = _worker(rec)
    _spotify_disable(worker)
    after_first = _rows_by_name(rec.db_path)
    assert set(after_first) == {"Morning email"}
    assert ledger.get_day_narrative() is None
    assert _diary_block_ids(store, "zephyrplaylist") == set()

    # Second disable of the same (now-absent) target: clean no-op.
    _spotify_disable(worker)
    assert set(_rows_by_name(rec.db_path)) == {"Morning email"}
    assert ledger.get_day_narrative() is None
    assert _diary_block_ids(store, "zephyrplaylist") == set()
    assert _diary_block_ids(store, "alphainbox") == {"blk-morning"}


# --------------------------------------------------------------------------
# _strip_metadata_bullets — the shared metadata helper (preserves other fields)
# --------------------------------------------------------------------------


def test_strip_metadata_bullets_preserves_other_fields():
    from screencap.pipeline_state import _strip_metadata_bullets

    blob = json.dumps({
        "bullets": ["one", "two"], "bullets_fallback": "app-level",
        "description": "kept", "block_id": "blk", "thread_id": "th",
    })
    out = json.loads(_strip_metadata_bullets(blob))
    assert "bullets" not in out and "bullets_fallback" not in out
    assert out == {"description": "kept", "block_id": "blk", "thread_id": "th"}

    # Idempotent: no bullet keys → returned unchanged (so the caller can skip).
    no_bullets = json.dumps({"description": "kept"})
    assert _strip_metadata_bullets(no_bullets) == no_bullets
    # None / empty stay as-is; a blob that empties out collapses to None.
    assert _strip_metadata_bullets(None) is None
    assert _strip_metadata_bullets(json.dumps({"bullets": ["x"]})) is None
