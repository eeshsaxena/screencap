"""U1 — per-recording share-key crypto for share-by-link (SCR-229).

Privacy-bearing (client-side crypto), Vision-free → runs on CI's privacy lane.
"""

import io

import pytest

from screencap import cloud_crypto, share_crypto

pytestmark = pytest.mark.privacy


def _decrypt(ciphertext: bytes, key: bytes) -> bytes:
    out = io.BytesIO()
    cloud_crypto.decrypt_to(io.BytesIO(ciphertext), out, key)
    return out.getvalue()


def _kek_encrypt(plaintext: bytes, key: bytes) -> bytes:
    return b"".join(cloud_crypto.encrypt_stream(io.BytesIO(plaintext), key))


def test_mint_share_key_is_256bit_and_unique():
    a = share_crypto.mint_share_key()
    b = share_crypto.mint_share_key()
    assert len(a) == share_crypto.SHARE_KEY_LEN == 32
    assert a != b  # CSPRNG, not a constant


def test_fragment_round_trip_is_url_safe():
    key = share_crypto.mint_share_key()
    frag = share_crypto.share_key_to_fragment(key)
    # URL-fragment safe: no +, /, or = padding.
    assert not (set("+/=") & set(frag))
    assert share_crypto.fragment_to_share_key(frag) == key


def test_fragment_rejects_wrong_length():
    with pytest.raises(ValueError):
        share_crypto.fragment_to_share_key("AAAA")  # decodes to 3 bytes, not 32


def test_encrypt_for_share_round_trips():
    plaintext = b"masked recording bytes " * 5000  # multi-frame
    share_key = share_crypto.mint_share_key()
    dst = io.BytesIO()
    share_crypto.encrypt_for_share(io.BytesIO(plaintext), share_key, dst)
    assert _decrypt(dst.getvalue(), share_key) == plaintext


def test_reencrypt_from_kek_ciphertext_source():
    """E2EE was on: the masked cloud copy is KEK-ciphertext (Covers R10 source)."""
    plaintext = b"masked scrubbed copy " * 4000
    kek = b"k" * 32
    kek_ciphertext = _kek_encrypt(plaintext, kek)
    share_key = share_crypto.mint_share_key()

    dst = io.BytesIO()
    share_crypto.reencrypt_for_share(io.BytesIO(kek_ciphertext), kek, share_key, dst)
    share_ciphertext = dst.getvalue()

    # Decrypts with the share key back to the original masked bytes...
    assert _decrypt(share_ciphertext, share_key) == plaintext
    # ...but the KEK and the share key are cross-incompatible (no corpus exposure).
    with pytest.raises(cloud_crypto.CloudKeyMismatch):
        _decrypt(kek_ciphertext, share_key)
    with pytest.raises(cloud_crypto.CloudKeyMismatch):
        _decrypt(share_ciphertext, kek)


def test_reencrypt_from_plaintext_source_when_e2ee_off():
    """E2EE was off: the masked cloud copy is server-readable plaintext, no KEK."""
    plaintext = b"server-readable masked copy " * 3000
    share_key = share_crypto.mint_share_key()
    dst = io.BytesIO()
    share_crypto.reencrypt_for_share(io.BytesIO(plaintext), None, share_key, dst)
    assert _decrypt(dst.getvalue(), share_key) == plaintext


def test_reencrypt_ciphertext_source_without_key_fails_loudly():
    kek_ciphertext = _kek_encrypt(b"x" * 100, b"k" * 32)
    with pytest.raises(ValueError):
        share_crypto.reencrypt_for_share(
            io.BytesIO(kek_ciphertext), None, share_crypto.mint_share_key(), io.BytesIO()
        )
