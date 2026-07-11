"""U1 — framed-AEAD core + cloud KEK (E2EE slice).

Security-critical: these tests pin the framing scheme's correctness
(round-trip across frame boundaries) and its integrity guarantees
(truncation / reorder / tamper / wrong-key all fail closed).
"""

from __future__ import annotations

import io

import keyring
import keyring.errors
import pytest
from cryptography.exceptions import InvalidTag

from screencap import cloud_crypto as cc

pytestmark = pytest.mark.privacy

KEY_A = b"A" * 32
KEY_B = b"B" * 32
FS = 16  # tiny frame size to exercise multi-frame paths with small inputs


def _enc(data: bytes, key: bytes = KEY_A, frame_size: int = FS) -> bytes:
    return b"".join(cc.encrypt_stream(io.BytesIO(data), key, frame_size))


def _dec(blob: bytes, key: bytes = KEY_A) -> bytes:
    out = io.BytesIO()
    cc.decrypt_to(io.BytesIO(blob), out, key)
    return out.getvalue()


# --------------------------------------------------------------------------
# Round-trip across every frame-boundary shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 31, 32, 33, 64, 100])
def test_round_trip_frame_boundaries(size):
    data = bytes((i * 7) % 256 for i in range(size))
    assert _dec(_enc(data)) == data


def test_round_trip_default_frame_size_large():
    data = bytes((i * 131) % 256 for i in range(cc.DEFAULT_FRAME_SIZE * 2 + 123))
    blob = b"".join(cc.encrypt_stream(io.BytesIO(data), KEY_A))
    out = io.BytesIO()
    cc.decrypt_to(io.BytesIO(blob), out, KEY_A)
    assert out.getvalue() == data


# --------------------------------------------------------------------------
# Length helper is exact (covers the empty case explicitly)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 48, 100])
def test_ciphertext_length_matches_actual(size):
    data = b"x" * size
    assert cc.ciphertext_length(size, FS) == len(_enc(data))


def test_frame_count_empty_is_one():
    assert cc.frame_count(0, FS) == 1
    assert cc.frame_count(FS, FS) == 1
    assert cc.frame_count(FS + 1, FS) == 2


# --------------------------------------------------------------------------
# Integrity: truncation / reorder / tamper / wrong key all fail closed
# --------------------------------------------------------------------------


def test_truncation_dropping_final_frame_fails():
    # size=17, FS=16 -> 2 frames: frame0 ct = 16+16, frame1 ct = 1+16.
    blob = _enc(b"z" * 17)
    frame0_end = cc.HEADER_LEN + (FS + 16)
    truncated = blob[:frame0_end]  # drop the real final frame
    with pytest.raises(InvalidTag):
        _dec(truncated)


def test_reordered_frames_fail():
    blob = _enc(b"a" * 48)  # 3 full frames of 16
    fct = FS + 16
    h = blob[: cc.HEADER_LEN]
    f0 = blob[cc.HEADER_LEN : cc.HEADER_LEN + fct]
    f1 = blob[cc.HEADER_LEN + fct : cc.HEADER_LEN + 2 * fct]
    f2 = blob[cc.HEADER_LEN + 2 * fct :]
    swapped = h + f1 + f0 + f2  # swap interior frames
    with pytest.raises(InvalidTag):
        _dec(swapped)


def test_bit_flip_in_body_fails():
    blob = bytearray(_enc(b"hello world this is longer than one frame"))
    blob[cc.HEADER_LEN + 3] ^= 0x01
    with pytest.raises(InvalidTag):
        _dec(bytes(blob))


def test_tampered_nonce_prefix_header_byte_fails():
    # Flipping a nonce-prefix byte parses fine (header is AAD-bound) but
    # every frame's tag then fails.
    blob = bytearray(_enc(b"payload"))
    blob[cc.HEADER_LEN - 1] ^= 0x01  # last header byte = nonce prefix
    with pytest.raises(InvalidTag):
        _dec(bytes(blob))


def test_tampered_version_raises_crypto_error():
    blob = bytearray(_enc(b"payload"))
    blob[len(cc.MAGIC)] = 0x7F  # version byte
    with pytest.raises(cc.CloudCryptoError):
        _dec(bytes(blob))


def test_wrong_key_raises_key_mismatch_not_invalid_tag():
    blob = _enc(b"payload", key=KEY_A)
    with pytest.raises(cc.CloudKeyMismatch):
        _dec(blob, key=KEY_B)


def test_key_mismatch_message_does_not_leak_key():
    blob = _enc(b"secret", key=KEY_A)
    try:
        _dec(blob, key=KEY_B)
    except cc.CloudKeyMismatch as exc:
        assert KEY_A not in str(exc).encode()
        assert KEY_B not in str(exc).encode()
    else:  # pragma: no cover
        pytest.fail("expected CloudKeyMismatch")


def test_over_max_frame_count_rejected(monkeypatch):
    monkeypatch.setattr(cc, "MAX_FRAMES", 2)
    with pytest.raises(cc.CloudCryptoError):
        _enc(b"a" * 48)  # needs 3 frames at FS=16


# --------------------------------------------------------------------------
# Magic is collision-free vs real artifact prefixes (KTD-3 / KTD-6)
# --------------------------------------------------------------------------


def test_encrypted_object_detected_as_encrypted():
    assert cc.is_encrypted_prefix(_enc(b"anything"))


@pytest.mark.parametrize(
    "prefix",
    [
        b"\x89PNG\r\n\x1a\n",  # png
        b"\x00\x00\x00\x18ftypmp42",  # mp4
        b"fLaC\x00\x00\x00\x22",  # flac
        b'{"schema_version": 1}',  # json / jsonl
        b"\x00\x00\x00\x20ftypisom",  # mp4 variant
    ],
)
def test_plaintext_artifact_prefixes_not_detected_as_encrypted(prefix):
    assert not cc.is_encrypted_prefix(prefix)


# --------------------------------------------------------------------------
# Cloud KEK lifecycle — un-entitled legacy fork (mocked Keychain)
#
# The shared-group home + migration are covered in tests/test_cloud_kek_group.py;
# these pin the legacy-`keyring` fallback, so the group is forced un-entitled
# (never the real Keychain).
# --------------------------------------------------------------------------


@pytest.fixture
def fake_keyring(monkeypatch):
    from screencap import keychain_group as kg

    def _unentitled(*_a, **_k):
        raise kg.MissingEntitlement(kg.errSecMissingEntitlement, "test")

    monkeypatch.setattr(kg, "load", _unentitled)
    monkeypatch.setattr(kg, "store", _unentitled)
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))
    monkeypatch.setattr(
        keyring, "set_password", lambda s, a, v: store.__setitem__((s, a), v)
    )
    return store


def test_get_cloud_kek_absent_returns_none(fake_keyring):
    assert cc.get_cloud_kek() is None


def test_get_or_create_creates_then_reads_same(fake_keyring):
    created = cc.get_or_create_cloud_kek()
    assert len(created) == 32
    assert cc.get_or_create_cloud_kek() == created  # not re-minted
    assert cc.get_cloud_kek() == created


def test_keyring_error_surfaces(fake_keyring, monkeypatch):
    def boom(_s, _a):
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr(keyring, "get_password", boom)
    with pytest.raises(keyring.errors.KeyringError):
        cc.get_cloud_kek()


def test_cloud_key_id_is_stable_and_not_the_key():
    kid = cc.cloud_key_id(KEY_A)
    assert kid == cc.cloud_key_id(KEY_A)
    assert kid != cc.cloud_key_id(KEY_B)
    assert len(kid) == 8
    assert KEY_A[:8] != kid  # a hash prefix, not the key
