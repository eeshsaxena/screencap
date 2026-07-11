"""Corpus plaintext→encrypted migration + compat (search guardrails U7 / R5 / R7).

Covers the still conversion (byte-preserved, idempotent, resumable), the SQLCipher
index rekey, and the read-compat seams (frame_resolve lists both forms; scrub_worker
purge unlinks both forms; the cloud copy decrypts encrypted stills to plaintext).
Vision-free; the index-rekey test importorskips pysqlcipher3.
"""

from __future__ import annotations

import os

import pytest

from screencap import corpus_crypto, corpus_migrate, frame_resolve, still_io

pytestmark = pytest.mark.privacy


def _rec(tmp_path, name="rec-1"):
    d = tmp_path / "recordings" / name
    (d / "screenshots").mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# Still conversion
# ---------------------------------------------------------------------------


def test_migrate_stills_byte_preserved_no_plaintext_left(tmp_path):
    key = os.urandom(32)
    rec = _rec(tmp_path)
    ss = rec / "screenshots"
    originals = {}
    for ts in (100.0, 200.5, 300.25):
        name = f"{ts:.6f}.jpg"
        data = os.urandom(400)
        (ss / name).write_bytes(data)
        originals[name] = data

    n = corpus_migrate.migrate_recording_stills(rec, key)

    assert n == 3
    assert [p for p in ss.iterdir() if p.name.endswith(".jpg")] == []  # no plaintext
    for name, data in originals.items():
        enc = ss / (name + ".enc")
        assert enc.exists()
        assert still_io.open_still(enc, key) == data  # decrypt == original


def test_migrate_stills_idempotent_and_resumable(tmp_path):
    key = os.urandom(32)
    rec = _rec(tmp_path)
    ss = rec / "screenshots"

    (ss / "100.000000.jpg").write_bytes(b"frame-a")
    assert corpus_migrate.migrate_recording_stills(rec, key) == 1

    # Simulate a crash that wrote the .enc but never unlinked the plaintext.
    still_io.write_encrypted_still(ss / "200.000000.jpg.enc", b"frame-b", key)
    (ss / "200.000000.jpg").write_bytes(b"frame-b")

    # Re-run finishes 200 (drops the stray plaintext) and does not touch 100.
    corpus_migrate.migrate_recording_stills(rec, key)
    assert not (ss / "200.000000.jpg").exists()
    assert [p for p in ss.iterdir() if p.name.endswith(".jpg")] == []
    assert still_io.open_still(ss / "200.000000.jpg.enc", key) == b"frame-b"

    # A third pass is a clean no-op (no double-encryption, no duplicates).
    assert corpus_migrate.migrate_recording_stills(rec, key) == 0


def test_migrate_corpus_skips_scrubbed_siblings(tmp_path):
    key = os.urandom(32)
    _rec(tmp_path, "rec-1")
    (tmp_path / "recordings" / "rec-1" / "screenshots" / "100.000000.jpg").write_bytes(b"x")
    scrubbed_ss = tmp_path / "recordings" / "rec-1-scrubbed" / "screenshots"
    scrubbed_ss.mkdir(parents=True)
    (scrubbed_ss / "100.000000.jpg").write_bytes(b"masked")

    report = corpus_migrate.migrate_corpus(
        tmp_path / "recordings", tmp_path / "content_index.db", key
    )
    assert report.stills_encrypted == 1  # only the source recording
    assert (scrubbed_ss / "100.000000.jpg").exists()  # scrubbed copy untouched
    assert list(scrubbed_ss.glob("*.enc")) == []


# ---------------------------------------------------------------------------
# Index rekey
# ---------------------------------------------------------------------------


def test_rekey_content_index_preserves_search(tmp_path):
    pytest.importorskip("pysqlcipher3")

    from screencap.content_index import ContentIndex, IndexFrame

    key = os.urandom(32)
    idx = tmp_path / "content_index.db"
    with ContentIndex(idx) as ix:  # plaintext
        ix.write_frames("rec", [IndexFrame(timestamp_ms=1000, text="findmetoken")])

    assert corpus_migrate._is_plaintext_sqlite(idx)
    assert corpus_migrate.rekey_content_index(idx, key) is True
    assert not corpus_migrate._is_plaintext_sqlite(idx)  # now encrypted

    with ContentIndex(idx, encrypted=True, key=key) as ix:
        assert ix.available
        assert ix.search("findmetoken").hits

    # Idempotent — already encrypted → no-op.
    assert corpus_migrate.rekey_content_index(idx, key) is False


def test_migrate_corpus_converts_stills_and_index(tmp_path):
    pytest.importorskip("pysqlcipher3")
    from screencap.content_index import ContentIndex, IndexFrame

    key = os.urandom(32)
    rec = _rec(tmp_path, "rec-1")
    (rec / "screenshots" / "100.000000.jpg").write_bytes(os.urandom(200))
    idx = tmp_path / "content_index.db"
    with ContentIndex(idx) as ix:
        ix.write_frames("rec-1", [IndexFrame(timestamp_ms=100000, text="corpustoken")])

    report = corpus_migrate.migrate_corpus(tmp_path / "recordings", idx, key)

    assert report.stills_encrypted == 1
    assert report.index_rekeyed is True
    with ContentIndex(idx, encrypted=True, key=key) as ix:
        assert ix.search("corpustoken").hits


# ---------------------------------------------------------------------------
# Read-compat seams
# ---------------------------------------------------------------------------


def test_frame_resolve_lists_both_forms_deduped(tmp_path):
    ss = tmp_path / "screenshots"
    ss.mkdir()
    (ss / "100.000000.jpg").write_bytes(b"a")
    (ss / "200.000000.jpg.enc").write_bytes(b"b")  # parsed by name only
    # A frame present as BOTH forms during the migration window → listed once.
    (ss / "300.000000.jpg").write_bytes(b"c")
    (ss / "300.000000.jpg.enc").write_bytes(b"d")

    stems = sorted(f.stem for f in frame_resolve.load_frames(ss))
    assert stems == ["100.000000", "200.000000", "300.000000"]


def test_both_still_forms_helper():
    from screencap.enforcement.scrub_worker import _both_still_forms

    assert _both_still_forms("screenshots/1.jpg") == (
        "screenshots/1.jpg",
        "screenshots/1.jpg.enc",
    )
    assert _both_still_forms("screenshots/1.jpg.enc") == (
        "screenshots/1.jpg.enc",
        "screenshots/1.jpg",
    )


# ---------------------------------------------------------------------------
# Cloud copy decrypts encrypted stills to plaintext (R7)
# ---------------------------------------------------------------------------


def test_cloud_copy_decrypts_encrypted_stills(tmp_path, monkeypatch):
    from screencap.scrubber import _decrypt_encrypted_stills_for_masking

    key = os.urandom(32)
    key_file = tmp_path / "corpus.key"
    key_file.write_text(corpus_crypto._encode_key(key))
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(key_file))

    # Scrubbed cloud copy: <name>-scrubbed/screenshots/<ts>.jpg.enc, AAD bound to
    # the SOURCE recording name "rec-1".
    scrubbed_ss = tmp_path / "rec-1-scrubbed" / "screenshots"
    scrubbed_ss.mkdir(parents=True)
    name = "150.000000.jpg"
    data = os.urandom(300)
    token = corpus_crypto.encrypt(data, key, corpus_crypto.corpus_aad("rec-1", name))
    (scrubbed_ss / (name + ".enc")).write_bytes(token)

    _decrypt_encrypted_stills_for_masking(scrubbed_ss)

    assert (scrubbed_ss / name).read_bytes() == data  # plaintext for upload
    assert list(scrubbed_ss.glob("*.enc")) == []


def test_cloud_copy_drops_undecryptable_still_fail_closed(tmp_path, monkeypatch):
    from screencap.scrubber import _decrypt_encrypted_stills_for_masking

    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(tmp_path / "absent.key"))
    scrubbed_ss = tmp_path / "rec-1-scrubbed" / "screenshots"
    scrubbed_ss.mkdir(parents=True)
    (scrubbed_ss / "150.000000.jpg.enc").write_bytes(b"cipher-no-key")

    _decrypt_encrypted_stills_for_masking(scrubbed_ss)

    # No key → the un-maskable still is dropped, not left for upload.
    assert list(scrubbed_ss.iterdir()) == []


# ---------------------------------------------------------------------------
# Inline png_data blob conversion (R5) + done-marker gating + active-write defer
# ---------------------------------------------------------------------------


def _seed_recording_db(rec_dir, rows):
    """rows: list of (recording_ts, screenshot_ts, png_bytes). Raw sqlite3 (local-only)."""
    import sqlite3

    db = rec_dir / "recording.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE screenshot (id INTEGER PRIMARY KEY, recording_timestamp REAL, "
        "timestamp REAL, png_data BLOB)"
    )
    for rec_ts, shot_ts, blob in rows:
        conn.execute(
            "INSERT INTO screenshot (recording_timestamp, timestamp, png_data) VALUES (?,?,?)",
            (rec_ts, shot_ts, sqlite3.Binary(blob)),
        )
    conn.commit()
    conn.close()
    return db


def test_migrate_db_blobs_encrypts_and_roundtrips(tmp_path):
    import sqlite3

    key = os.urandom(32)
    rec = _rec(tmp_path, "rec-1")
    blob = os.urandom(500)
    _seed_recording_db(rec, [(100.0, 150.25, blob)])

    assert corpus_migrate.migrate_recording_db_blobs(rec, key) == 1

    conn = sqlite3.connect(str(rec / "recording.db"))
    stored = bytes(conn.execute("SELECT png_data FROM screenshot").fetchone()[0])
    conn.close()
    assert corpus_crypto.is_encrypted(stored)  # no plaintext blob left in recording.db
    # Round-trips under the SAME AAD the capture write path binds.
    assert corpus_crypto.decrypt(stored, key, still_io.png_blob_aad(100.0, 150.25)) == blob
    # Idempotent — an already-encrypted blob is not re-encrypted.
    assert corpus_migrate.migrate_recording_db_blobs(rec, key) == 0


def test_namer_decrypts_migrated_blob(tmp_path, monkeypatch):
    import io as _io
    import sqlite3

    from PIL import Image

    from screencap import namer

    key = os.urandom(32)
    key_file = tmp_path / "corpus.key"
    key_file.write_text(corpus_crypto._encode_key(key))
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(key_file))

    buf = _io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="JPEG")
    jpeg = buf.getvalue()
    rec = _rec(tmp_path, "rec-1")
    _seed_recording_db(rec, [(100.0, 150.25, jpeg)])
    corpus_migrate.migrate_recording_db_blobs(rec, key)

    conn = sqlite3.connect(str(rec / "recording.db"))
    stored = bytes(conn.execute("SELECT png_data FROM screenshot").fetchone()[0])
    conn.close()
    # namer transparently decrypts the encrypted blob back to the original JPEG.
    assert namer._decrypt_blob_if_needed(stored, 100.0, 150.25) == jpeg
    # A plaintext blob passes through unchanged.
    assert namer._decrypt_blob_if_needed(jpeg, 100.0, 150.25) == jpeg


def _stub_flip_config(monkeypatch, tmp_path, recs, key):
    from screencap import config

    key_file = tmp_path / "corpus.key"
    key_file.write_text(corpus_crypto._encode_key(key))
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(key_file))
    state = {"requested": False, "encrypted": False}
    monkeypatch.setattr(config, "get_recordings_dir", lambda: recs)
    monkeypatch.setattr(
        config, "set_corpus_encryption_requested", lambda v: state.__setitem__("requested", v)
    )
    monkeypatch.setattr(config, "set_corpus_encrypted", lambda v: state.__setitem__("encrypted", v))
    monkeypatch.setattr(
        "screencap.content_index.default_index_path", lambda: tmp_path / "content_index.db"
    )
    return state


def test_flip_defers_active_writes_and_gates_marker(tmp_path, monkeypatch):
    # A freshly-written still (mtime within the active-write grace) is deferred, so
    # plaintext remains, so the corpus_encrypted done marker must NOT flip.
    key = os.urandom(32)
    recs = tmp_path / "recordings"
    rec = _rec(tmp_path, "rec-1")
    (rec / "screenshots" / "100.000000.jpg").write_bytes(b"just-written")  # mtime == now
    state = _stub_flip_config(monkeypatch, tmp_path, recs, key)

    corpus_migrate.flip_corpus_to_encrypted()

    assert state["requested"] is True  # intent recorded
    assert state["encrypted"] is False  # #5: not flipped — plaintext remained
    assert (rec / "screenshots" / "100.000000.jpg").exists()  # #9: fresh still deferred


def test_flip_sets_marker_when_pass_is_clean(tmp_path, monkeypatch):
    key = os.urandom(32)
    recs = tmp_path / "recordings"
    rec = _rec(tmp_path, "rec-1")
    jpg = rec / "screenshots" / "100.000000.jpg"
    jpg.write_bytes(b"idle-still")
    os.utime(jpg, (1_000.0, 1_000.0))  # backdate well past the active-write grace
    state = _stub_flip_config(monkeypatch, tmp_path, recs, key)

    corpus_migrate.flip_corpus_to_encrypted()

    assert state["encrypted"] is True  # clean pass → done marker set
    assert not jpg.exists()
    assert (rec / "screenshots" / "100.000000.jpg.enc").exists()
