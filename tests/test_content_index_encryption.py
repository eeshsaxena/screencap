"""Encrypted ``content_index.db`` (search guardrails U4 / R3).

The FTS5 sidecar is SQLCipher-encrypted at rest with the corpus key, with search
behavior identical to the plaintext store. Requires the ``pysqlcipher3`` binding
(the ``sqlcipher`` extra) — skipped where it is absent (e.g. the Linux CI privacy
lane), which is the fail-closed posture the plan expects on un-provisioned hosts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("pysqlcipher3")

from screencap.content_index import ContentIndex, IndexFrame, IndexState  # noqa: E402

pytestmark = pytest.mark.privacy


def _frames(*pairs: tuple[int, str]) -> list[IndexFrame]:
    return [IndexFrame(timestamp_ms=ts, text=text) for ts, text in pairs]


def _enc(path: Path, key: bytes) -> ContentIndex:
    return ContentIndex(path, encrypted=True, key=key)


def test_encrypted_index_search_matches_plaintext_baseline(tmp_path):
    key = os.urandom(32)
    rows = [
        ("rec-a", (1000, "the quarterly invoice was overdue")),
        ("rec-b", (2000, "lunch with the design team")),
    ]

    # Plaintext baseline.
    plain_path = tmp_path / "plain.db"
    with ContentIndex(plain_path) as idx:
        for rec, frame in rows:
            idx.write_frames(rec, _frames(frame))
        base = idx.search("invoice")

    # Encrypted store — same data, same query.
    enc_path = tmp_path / "content_index.db"
    with _enc(enc_path, key) as idx:
        assert idx.available
        for rec, frame in rows:
            idx.write_frames(rec, _frames(frame))

    # Reopen with the key: hits identical to the plaintext baseline.
    with _enc(enc_path, key) as idx:
        res = idx.search("invoice")

    assert res.index_state is base.index_state is IndexState.OK
    assert [(h.recording, h.timestamp_ms) for h in res.hits] == [
        (h.recording, h.timestamp_ms) for h in base.hits
    ]
    assert res.hits and res.hits[0].recording == "rec-a"


def test_raw_file_holds_no_plaintext_terms(tmp_path):
    key = os.urandom(32)
    enc_path = tmp_path / "content_index.db"
    with _enc(enc_path, key) as idx:
        idx.write_frames("rec", _frames((1000, "supersecretsentinelword appears here")))

    raw = enc_path.read_bytes()
    assert b"supersecretsentinelword" not in raw
    assert b"appears here" not in raw  # no indexed plaintext survives at rest


def test_open_without_key_or_wrong_key_fails_closed(tmp_path):
    key = os.urandom(32)
    enc_path = tmp_path / "content_index.db"
    with _enc(enc_path, key) as idx:
        idx.write_frames("rec", _frames((1000, "findable text token")))

    # No key at all → store unavailable, search returns STORE_UNAVAILABLE (no crash).
    with ContentIndex(enc_path, encrypted=True, key=None) as idx:
        assert not idx.available
        assert idx.search("findable").index_state is IndexState.STORE_UNAVAILABLE

    # Wrong key → same fail-closed outcome.
    with _enc(enc_path, os.urandom(32)) as idx:
        assert not idx.available
        assert idx.search("findable").index_state is IndexState.STORE_UNAVAILABLE


def test_delete_recording_behaves_identically_encrypted(tmp_path):
    key = os.urandom(32)
    enc_path = tmp_path / "content_index.db"
    with _enc(enc_path, key) as idx:
        idx.write_frames("rec-a", _frames((1000, "alpha token")))
        idx.write_frames("rec-b", _frames((2000, "beta token")))
        assert {h.recording for h in idx.search("token").hits} == {"rec-a", "rec-b"}

        idx.delete_recording("rec-a")
        assert {h.recording for h in idx.search("token").hits} == {"rec-b"}


def test_write_chunk_replace_range_encrypted(tmp_path):
    key = os.urandom(32)
    enc_path = tmp_path / "content_index.db"
    with _enc(enc_path, key) as idx:
        idx.write_chunk("rec", 1000, 3000, _frames((1000, "alpha"), (2000, "beta secret")))
        assert idx.search("secret").hits
        # Re-process the same range with the frame now redacted → stale row dropped.
        idx.write_chunk("rec", 1000, 3000, _frames((1000, "alpha redacted")))
        assert idx.search("secret").hits == []
        assert idx.search("alpha").hits
