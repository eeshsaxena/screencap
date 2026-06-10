"""Tests for ``screencap.content_index`` — the global FTS5 sidecar store.

Covers U1 of SCR-118: idempotent writes, ranked snippet+pointer search, FTS5
injection-safety, the escaped-LIKE fallback, deletion APIs, hardened perms, the
symlink guard, corruption fail-soft, and cross-connection write isolation.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from screencap.content_index import (
    ContentIndex,
    IndexFrame,
    IndexState,
)


def _idx(tmp_path: Path, name: str = "content_index.db") -> ContentIndex:
    return ContentIndex(tmp_path / name)


def _frames(*pairs: tuple[int, str]) -> list[IndexFrame]:
    return [IndexFrame(timestamp_ms=ts, text=text) for ts, text in pairs]


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


def test_search_returns_pointer_and_snippet(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("rec-a", _frames((1000, "the quarterly invoice was overdue")))
        idx.write_frames("rec-b", _frames((2000, "lunch with the design team")))

        res = idx.search("invoice")

    assert res.index_state is IndexState.OK
    assert len(res.hits) == 1
    hit = res.hits[0]
    assert hit.recording == "rec-a"
    assert hit.timestamp_ms == 1000
    assert "invoice" in hit.snippet.lower()


def test_bm25_ranks_stronger_match_first(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        # A frame where the term dominates a short document ranks above a frame
        # where it is one word in a long document (bm25, best == most negative).
        idx.write_frames("rec-strong", _frames((10, "invoice invoice invoice")))
        idx.write_frames(
            "rec-weak",
            _frames((20, "invoice " + " ".join(["filler"] * 80))),
        )

        res = idx.search("invoice")

    assert [h.recording for h in res.hits][0] == "rec-strong"
    # bm25 sorts ascending (most negative first).
    assert res.hits[0].score <= res.hits[1].score


def test_cross_recording_search_attributes_each_hit(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("morning", _frames((1, "standup notes budget")))
        idx.write_frames("afternoon", _frames((2, "budget review meeting")))

        res = idx.search("budget")

    assert {h.recording for h in res.hits} == {"morning", "afternoon"}


def test_unicode_matched_accent_insensitively(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("rec", _frames((1, "café résumé naïve")))

        # remove_diacritics 2 → accent-insensitive matching.
        assert idx.search("cafe").hits
        assert idx.search("resume").hits


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_write_is_idempotent_per_frame(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("rec", _frames((1000, "first pass with secret token")))
        # Re-processing the same chunk (--force / reconcile) with more-redacted
        # text must replace, not accumulate.
        idx.write_frames("rec", _frames((1000, "first pass redacted")))

        assert idx.search("secret").hits == []
        hits = idx.search("redacted").hits
        assert len(hits) == 1
        assert hits[0].timestamp_ms == 1000


def test_empty_text_frames_are_dropped(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        written = idx.write_frames("rec", _frames((1, "   "), (2, ""), (3, "real")))

    assert written == 1


# --------------------------------------------------------------------------
# Empty / no-match
# --------------------------------------------------------------------------


def test_empty_store_reports_no_match(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        res = idx.search("anything")
    assert res.hits == []
    assert res.index_state is IndexState.NO_MATCH


def test_blank_query_is_no_match(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("rec", _frames((1, "hello world")))
        assert idx.search("   ").index_state is IndexState.NO_MATCH


# --------------------------------------------------------------------------
# FTS injection safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize("query", ['"', "*", "NEAR", "recording: foo", "text:bar", "a AND b"])
def test_fts_syntax_is_treated_as_literal(tmp_path: Path, query: str) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("rec", _frames((1, "ordinary indexed words")))
        # Must not raise and must not escape into column-filter / operator syntax.
        res = idx.search(query)
    assert res.index_state in (IndexState.NO_MATCH, IndexState.OK)


def test_quoted_token_matches_literally(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("rec", _frames((1, "the value is col:42 today")))
        # A user searching the literal token finds it without it being parsed
        # as an FTS5 column filter.
        assert idx.search("col:42").hits


# --------------------------------------------------------------------------
# Deletion APIs
# --------------------------------------------------------------------------


def test_delete_recording_purges_all_rows(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("rec-a", _frames((1, "alpha"), (2, "beta")))
        idx.write_frames("rec-b", _frames((3, "alpha")))

        deleted = idx.delete_recording("rec-a")

    assert deleted == 2
    with _idx(tmp_path) as idx:
        hits = idx.search("alpha").hits
        assert {h.recording for h in hits} == {"rec-b"}


def test_delete_interval_purges_only_in_range(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames(
            "rec", _frames((1000, "keep early"), (2000, "purge mid"), (3000, "keep late"))
        )

        # Half-open [2000, 3000): only the 2000 frame goes.
        idx.delete_recording_interval("rec", 2000, 3000)

        remaining = {h.timestamp_ms for h in idx.search("keep").hits}
        assert remaining == {1000, 3000}
        assert idx.search("purge").hits == []


def test_delete_interval_open_ended(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        idx.write_frames("rec", _frames((1000, "before"), (5000, "after")))
        # end_ms=None → the scrub_worker trailing float('inf') interval.
        idx.delete_recording_interval("rec", 5000, None)

        assert {h.timestamp_ms for h in idx.search("before").hits} == {1000}
        assert idx.search("after").hits == []


# --------------------------------------------------------------------------
# FTS5 absent → escaped-LIKE fallback
# --------------------------------------------------------------------------


def test_like_fallback_when_fts5_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ContentIndex, "_probe_fts5", staticmethod(lambda conn: False))
    with _idx(tmp_path) as idx:
        assert idx.fts_available is False
        idx.write_frames("rec", _frames((1, "the invoice total is wrong")))

        res = idx.search("invoice")

    assert res.index_state is IndexState.INDEX_DEGRADED
    assert len(res.hits) == 1
    assert "invoice" in res.hits[0].snippet.lower()


def test_like_fallback_escapes_metacharacters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ContentIndex, "_probe_fts5", staticmethod(lambda conn: False))
    with _idx(tmp_path) as idx:
        idx.write_frames("rec", _frames((1, "100% complete"), (2, "100 done")))
        # '%' is a LIKE wildcard — must match literally, so '100%' does not
        # match the '100 done' row.
        hits = idx.search("100%").hits
    assert {h.timestamp_ms for h in hits} == {1}


# --------------------------------------------------------------------------
# Corruption / missing store → fail-soft
# --------------------------------------------------------------------------


def test_corrupt_store_surfaces_store_unavailable(tmp_path: Path) -> None:
    db = tmp_path / "content_index.db"
    db.write_bytes(b"this is definitely not a sqlite database" * 50)

    idx = ContentIndex(db)
    try:
        assert idx.available is False
        res = idx.search("anything")
        assert res.index_state is IndexState.STORE_UNAVAILABLE
        assert res.hits == []
    finally:
        idx.close()


# --------------------------------------------------------------------------
# Hardened perms + symlink guard
# --------------------------------------------------------------------------


def test_created_files_are_owner_only(tmp_path: Path) -> None:
    db = tmp_path / "content_index.db"
    with ContentIndex(db) as idx:
        idx.write_frames("rec", _frames((1, "perm check")))

        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.exists():
                assert stat.S_IMODE(side.stat().st_mode) == 0o600
        # Parent dir locked to owner-only.
        assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


def test_symlinked_db_path_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real_target.db"
    real.write_bytes(b"")
    link = tmp_path / "content_index.db"
    link.symlink_to(real)

    idx = ContentIndex(link)
    try:
        assert idx.available is False
        assert idx.search("x").index_state is IndexState.STORE_UNAVAILABLE
    finally:
        idx.close()


def test_symlinked_parent_is_refused(tmp_path: Path) -> None:
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    idx = ContentIndex(link_dir / "content_index.db")
    try:
        assert idx.available is False
    finally:
        idx.close()


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------


def test_limit_is_clamped(tmp_path: Path) -> None:
    with _idx(tmp_path) as idx:
        for i in range(30):
            idx.write_frames("rec", _frames((i, f"match number {i}")))

        assert len(idx.search("match", limit=5).hits) == 5
        # Oversized requests can't drive an unbounded payload.
        assert len(idx.search("match", limit=10_000).hits) <= 100


def test_snippet_is_bounded(tmp_path: Path) -> None:
    long_text = "lead " + " ".join(["word"] * 500) + " needle " + " ".join(["tail"] * 500)
    with _idx(tmp_path) as idx:
        idx.write_frames("rec", _frames((1, long_text)))
        snippet = idx.search("needle").hits[0].snippet
    # Far shorter than the full document.
    assert len(snippet) < len(long_text) // 2


# --------------------------------------------------------------------------
# Concurrency — a reader never sees a half-written chunk
# --------------------------------------------------------------------------


def test_reader_never_sees_uncommitted_write(tmp_path: Path) -> None:
    db = tmp_path / "content_index.db"
    with ContentIndex(db) as writer:
        writer.write_frames("rec", _frames((1, "committed row")))

        # Hold an open IMMEDIATE transaction with an un-committed insert.
        conn = writer._conn
        assert conn is not None
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO content_fts (recording, timestamp_ms, text) VALUES (?, ?, ?)",
            ("rec", 2, "pending row"),
        )

        # A separate reader connection sees only the committed state.
        with ContentIndex(db) as reader:
            ts = {h.timestamp_ms for h in reader.search("row").hits}
            assert ts == {1}

        conn.rollback()
