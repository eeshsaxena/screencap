"""Encrypted still write path (search guardrails U2).

Drives the engine ``write_screen_event`` through the ``recording_db`` fixture and
asserts the encrypted-format invariants: only ``*.jpg.enc`` on disk (never
plaintext), encrypted ``png_data`` blobs, byte-identical plaintext behavior when the
flag is off, fail-closed on a missing key, and no key material in the child config.

Vision-free; the corpus key comes from the env-file channel so it runs in CI's
``pytest -m privacy`` lane on any host.
"""

from __future__ import annotations

import io
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from screencap import corpus_crypto, still_io
from screencap.engine import recorder
from screencap.engine.config import (
    RecordingConfig,
    build_config_overrides,
    config,
)
from screencap.engine.recorder import Event, write_screen_event

pytestmark = pytest.mark.privacy


def _img():
    return Image.new("RGB", (64, 64), "blue")


@pytest.fixture(autouse=True)
def _reset_key_cache():
    """Force a clean per-process corpus-key cache around every test."""
    recorder._CORPUS_KEY_LOADED = False
    recorder._CORPUS_KEY_CACHE = None
    yield
    recorder._CORPUS_KEY_LOADED = False
    recorder._CORPUS_KEY_CACHE = None


@pytest.fixture
def images_encrypted():
    orig_i, orig_e = config.RECORD_IMAGES, config.RECORD_IMAGES_ENCRYPTED
    object.__setattr__(config, "RECORD_IMAGES", True)
    object.__setattr__(config, "RECORD_IMAGES_ENCRYPTED", True)
    yield
    object.__setattr__(config, "RECORD_IMAGES", orig_i)
    object.__setattr__(config, "RECORD_IMAGES_ENCRYPTED", orig_e)


@pytest.fixture
def images_plaintext():
    orig_i, orig_e = config.RECORD_IMAGES, config.RECORD_IMAGES_ENCRYPTED
    object.__setattr__(config, "RECORD_IMAGES", True)
    object.__setattr__(config, "RECORD_IMAGES_ENCRYPTED", False)
    yield
    object.__setattr__(config, "RECORD_IMAGES", orig_i)
    object.__setattr__(config, "RECORD_IMAGES_ENCRYPTED", orig_e)


@pytest.fixture
def corpus_key(monkeypatch, tmp_path):
    key_file = tmp_path / "corpus.key"
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(key_file))
    monkeypatch.setattr(corpus_crypto, "_lock_path", lambda: tmp_path / "corpus.lock")
    return corpus_crypto.get_or_create_corpus_key()


def _plaintext_jpgs(screenshots_dir: Path) -> list[Path]:
    return [p for p in screenshots_dir.iterdir() if p.name.endswith(".jpg")]


# ---------------------------------------------------------------------------
# Encrypted file path
# ---------------------------------------------------------------------------


def test_encrypted_still_written_and_no_plaintext(recording_db, corpus_key, images_encrypted):
    from screencap.engine.db.models import Screenshot

    screenshots_dir = Path(recording_db.db_path).parent / "screenshots"
    screenshots_dir.mkdir()
    ts = 1709641234.567000
    event = Event(timestamp=ts, type="screen", data=_img())

    write_screen_event(
        recording_db.session, recording_db.recording, event, MagicMock(),
        screenshots_dir=str(screenshots_dir),
    )

    enc = screenshots_dir / f"{ts:.6f}.jpg.enc"
    assert enc.exists()
    assert _plaintext_jpgs(screenshots_dir) == []  # no plaintext .jpg
    assert list(screenshots_dir.glob("*.part")) == []  # no leftover temp
    assert stat.S_IMODE(enc.stat().st_mode) == 0o600

    # Decryptable via the shared seam, back to a valid JPEG.
    plain = still_io.open_still(enc, corpus_key)
    Image.open(io.BytesIO(plain)).verify()

    row = recording_db.session.query(Screenshot).one()
    assert row.image_path == f"screenshots/{ts:.6f}.jpg.enc"
    assert row.png_data is None


def test_encrypted_blob_is_not_jpeg_until_decrypted(recording_db, corpus_key, images_encrypted):
    from screencap.engine.db.models import Screenshot

    ts = 1709641234.567
    event = Event(timestamp=ts, type="screen", data=_img())
    write_screen_event(
        recording_db.session, recording_db.recording, event, MagicMock(),
        screenshots_dir=None,
    )

    row = recording_db.session.query(Screenshot).one()
    blob = row.png_data
    assert blob is not None
    assert corpus_crypto.is_encrypted(blob)
    assert not blob.startswith(b"\xff\xd8")  # not a valid JPEG until decrypted

    aad = still_io.png_blob_aad(recording_db.recording.timestamp, ts)
    decrypted = corpus_crypto.decrypt(blob, corpus_key, aad)
    assert decrypted.startswith(b"\xff\xd8")


# ---------------------------------------------------------------------------
# Plaintext path unchanged when the flag is off (pre-flip / opt-in)
# ---------------------------------------------------------------------------


def test_flag_off_writes_plaintext_exactly_as_today(recording_db, images_plaintext):
    from screencap.engine.db.models import Screenshot

    screenshots_dir = Path(recording_db.db_path).parent / "screenshots"
    screenshots_dir.mkdir()
    ts = 1709641234.567000
    event = Event(timestamp=ts, type="screen", data=_img())

    write_screen_event(
        recording_db.session, recording_db.recording, event, MagicMock(),
        screenshots_dir=str(screenshots_dir),
    )

    jpg = screenshots_dir / f"{ts:.6f}.jpg"
    assert jpg.exists()
    assert Image.open(jpg).format == "JPEG"
    assert list(screenshots_dir.glob("*.enc")) == []
    assert stat.S_IMODE(jpg.stat().st_mode) == 0o600

    row = recording_db.session.query(Screenshot).one()
    assert row.image_path == f"screenshots/{ts:.6f}.jpg"


# ---------------------------------------------------------------------------
# Fail-closed + crash safety
# ---------------------------------------------------------------------------


def test_encryption_on_but_key_missing_fails_closed(
    recording_db, monkeypatch, tmp_path, images_encrypted
):
    from screencap.engine.db.models import Screenshot

    # Env file points at a nonexistent key → load_corpus_key() returns None.
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(tmp_path / "absent.key"))
    screenshots_dir = Path(recording_db.db_path).parent / "screenshots"
    screenshots_dir.mkdir()
    event = Event(timestamp=1709641234.567, type="screen", data=_img())

    write_screen_event(
        recording_db.session, recording_db.recording, event, MagicMock(),
        screenshots_dir=str(screenshots_dir),
    )

    # No plaintext, no ciphertext — nothing written at all (fail closed).
    assert list(screenshots_dir.iterdir()) == []
    row = recording_db.session.query(Screenshot).one()
    assert row.image_path is None
    assert row.png_data is None


def test_crash_mid_encrypted_write_leaves_no_plaintext(
    recording_db, corpus_key, images_encrypted, monkeypatch
):
    from screencap.engine.db.models import Screenshot

    screenshots_dir = Path(recording_db.db_path).parent / "screenshots"
    screenshots_dir.mkdir()
    ts = 1709641234.567

    # Simulate a crash at the atomic rename step.
    def _boom(_src, _dst):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(still_io.os, "replace", _boom)

    event = Event(timestamp=ts, type="screen", data=_img())
    write_screen_event(
        recording_db.session, recording_db.recording, event, MagicMock(),
        screenshots_dir=str(screenshots_dir),
    )

    # No plaintext .jpg, no final .enc, and the ciphertext temp is cleaned up.
    assert _plaintext_jpgs(screenshots_dir) == []
    assert list(screenshots_dir.glob("*.jpg.enc")) == []
    assert list(screenshots_dir.glob("*.part")) == []
    row = recording_db.session.query(Screenshot).one()
    assert row.image_path is None  # write failed → no path recorded


# ---------------------------------------------------------------------------
# Key never rides the child config (secrets go via the env file only)
# ---------------------------------------------------------------------------


def test_encrypted_flag_maps_to_config_without_key_material(corpus_key):
    overrides = build_config_overrides(
        RecordingConfig(capture_images=True, capture_images_encrypted=True)
    )
    assert overrides["RECORD_IMAGES_ENCRYPTED"] is True
    # The corpus key bytes never appear in any override value.
    for value in overrides.values():
        assert corpus_key not in repr(value).encode("latin-1", "ignore")
        assert not (isinstance(value, (bytes, bytearray)) and corpus_key in value)
