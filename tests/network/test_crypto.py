"""Tests for screencap.network.crypto (V1.5).

Covers:

* :func:`get_or_create_kek` -- happy-path generate-and-store, second-call
  silent read, and the missing-stored-KEK regen path. ``keyring`` is
  mocked at the function boundary; tests NEVER touch the real macOS
  Keychain.
* :func:`generate_dek` -- length and uniqueness sanity.
* :func:`wrap_dek` / :func:`unwrap_dek` -- round-trip, byte-flip
  detection, AAD-mismatch detection (uses internal ``AESGCM`` to
  construct the mismatched-AAD test since ``unwrap_dek``'s public
  signature does not accept an aad arg).
* :func:`encrypt_body` / :func:`decrypt_body` -- round-trip across
  several body sizes, byte-flip detection, AAD-mismatch detection (the
  cross-flow-id binding check).
* :func:`aad_bytes` -- the LOCKED byte format. Hand-encoded fixture
  guards against accidental drift; type-invariant rejections (non-int
  recording_id, bool recording_id, non-ASCII flow_id / event_type).
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from screencap.network import crypto
from screencap.network.crypto import (
    DEK_WRAP_AAD,
    KEK_ACCOUNT,
    SERVICE,
    aad_bytes,
    decrypt_body,
    encrypt_body,
    generate_dek,
    get_or_create_kek,
    unwrap_dek,
    wrap_dek,
)

# ---------------------------------------------------------------------------
# get_or_create_kek
# ---------------------------------------------------------------------------


class TestGetKek:
    """``get_kek`` is the read-only counterpart to ``get_or_create_kek``.
    Export-time paths use this so a missing KEK surfaces as ``None``
    rather than triggering silent re-creation. Critical for keeping
    ``screencap network remove-kek`` sticky.
    """

    def test_returns_bytes_when_present(self):
        existing = base64.b64encode(b"\xcd" * 32).decode("ascii")
        with patch("keyring.get_password", return_value=existing) as mock_get:
            kek = crypto.get_kek()
        assert kek == b"\xcd" * 32
        mock_get.assert_called_once_with(SERVICE, KEK_ACCOUNT)

    def test_returns_none_when_missing(self):
        with patch("keyring.get_password", return_value=None) as mock_get, \
             patch("keyring.set_password") as mock_set:
            kek = crypto.get_kek()
        assert kek is None
        mock_get.assert_called_once_with(SERVICE, KEK_ACCOUNT)
        # The critical invariant: read-only — never writes a fresh KEK.
        mock_set.assert_not_called()

    def test_does_not_regenerate_after_remove_kek(self):
        """Sequence: get_kek -> None (entry removed) -> caller's choice.
        Calling get_kek a second time must STILL return None (no silent
        regeneration). This is what makes `network remove-kek` sticky.
        """
        with patch("keyring.get_password", return_value=None), \
             patch("keyring.set_password") as mock_set:
            first = crypto.get_kek()
            second = crypto.get_kek()
        assert first is None
        assert second is None
        mock_set.assert_not_called()


class TestGetOrCreateKek:
    def test_first_call_generates_and_stores(self):
        """First call: keyring.get_password returns None -> set_password
        called with base64'd 32 random bytes -> returns 32 bytes."""
        stored: dict[tuple[str, str], str] = {}

        def fake_get(service: str, account: str) -> str | None:
            return stored.get((service, account))

        def fake_set(service: str, account: str, value: str) -> None:
            stored[(service, account)] = value

        with patch("keyring.get_password", side_effect=fake_get) as mock_get, \
             patch("keyring.set_password", side_effect=fake_set) as mock_set:
            kek = get_or_create_kek()

        assert isinstance(kek, bytes)
        assert len(kek) == 32

        # Single get + single set on the locked service/account pair.
        mock_get.assert_called_once_with(SERVICE, KEK_ACCOUNT)
        assert mock_set.call_count == 1
        set_call = mock_set.call_args
        assert set_call.args[0] == SERVICE
        assert set_call.args[1] == KEK_ACCOUNT
        # The persisted value is base64-encoded 32-byte material.
        assert base64.b64decode(set_call.args[2].encode("ascii")) == kek

    def test_second_call_returns_same_bytes_silently(self):
        """Second call: get_password returns the stored value -> returns
        identical bytes; set_password not called again."""
        stored: dict[tuple[str, str], str] = {}

        def fake_get(service: str, account: str) -> str | None:
            return stored.get((service, account))

        def fake_set(service: str, account: str, value: str) -> None:
            stored[(service, account)] = value

        with patch("keyring.get_password", side_effect=fake_get), \
             patch("keyring.set_password", side_effect=fake_set):
            first = get_or_create_kek()
            second = get_or_create_kek()

        assert first == second
        # Storage was written exactly once across both calls.
        assert len(stored) == 1

    def test_second_call_does_not_set_again(self):
        """When get_password returns a value on the first call, no
        set_password call is made (silent read path)."""
        existing = base64.b64encode(b"\xab" * 32).decode("ascii")

        with patch("keyring.get_password", return_value=existing) as mock_get, \
             patch("keyring.set_password") as mock_set:
            kek = get_or_create_kek()

        assert kek == b"\xab" * 32
        mock_get.assert_called_once_with(SERVICE, KEK_ACCOUNT)
        mock_set.assert_not_called()

    def test_missing_kek_regenerates(self):
        """``get_password`` returning None twice in succession (e.g. the
        Keychain entry was wiped between recordings) -> the second call
        also generates a fresh KEK."""
        with patch("keyring.get_password", return_value=None), \
             patch("keyring.set_password") as mock_set:
            first = get_or_create_kek()
            second = get_or_create_kek()

        assert len(first) == 32
        assert len(second) == 32
        # Two independent regen events -> almost-certainly different bytes.
        assert first != second
        # set_password called once per regeneration.
        assert mock_set.call_count == 2


# ---------------------------------------------------------------------------
# generate_dek
# ---------------------------------------------------------------------------


class TestGenerateDek:
    def test_returns_32_bytes(self):
        dek = generate_dek()
        assert isinstance(dek, bytes)
        assert len(dek) == 32

    def test_uniqueness_across_calls(self):
        # Two consecutive calls returning identical 32 bytes would imply
        # a broken PRNG. Statistical sanity.
        a = generate_dek()
        b = generate_dek()
        assert a != b


# ---------------------------------------------------------------------------
# wrap_dek / unwrap_dek
# ---------------------------------------------------------------------------


class TestWrapUnwrapDek:
    def test_roundtrip_returns_original(self):
        kek = b"\x01" * 32
        dek = generate_dek()
        wrapped, nonce = wrap_dek(dek, kek)
        # Sanity: ciphertext is non-empty and a different value than dek.
        assert len(nonce) == 12
        assert wrapped != dek
        assert unwrap_dek(wrapped, nonce, kek) == dek

    def test_corruption_raises_invalid_tag(self):
        kek = b"\x02" * 32
        dek = generate_dek()
        wrapped, nonce = wrap_dek(dek, kek)
        # Flip a byte in the ciphertext.
        tampered = bytearray(wrapped)
        tampered[0] ^= 0xFF
        with pytest.raises(InvalidTag):
            unwrap_dek(bytes(tampered), nonce, kek)

    def test_aad_mismatch_raises_invalid_tag(self):
        """A wrap produced with ``DEK_WRAP_AAD`` cannot be unwrapped
        when a different AAD is presented at decrypt time. Constructed
        inline because :func:`unwrap_dek`'s public signature does not
        accept an aad arg -- the AAD is locked to ``DEK_WRAP_AAD``."""
        kek = b"\x03" * 32
        dek = generate_dek()
        wrapped, nonce = wrap_dek(dek, kek)
        # Confirm the correct AAD works.
        assert AESGCM(kek).decrypt(nonce, wrapped, DEK_WRAP_AAD) == dek
        # Wrong AAD (e.g. cross-scheme attempt) -> InvalidTag.
        with pytest.raises(InvalidTag):
            AESGCM(kek).decrypt(nonce, wrapped, b"different-aad")

    def test_wrong_kek_length_raises_value_error(self):
        with pytest.raises(ValueError, match="kek must be 32 bytes"):
            wrap_dek(b"\x00" * 32, b"\x00" * 16)

    def test_wrong_dek_length_raises_value_error(self):
        with pytest.raises(ValueError, match="dek must be 32 bytes"):
            wrap_dek(b"\x00" * 16, b"\x00" * 32)


# ---------------------------------------------------------------------------
# encrypt_body / decrypt_body
# ---------------------------------------------------------------------------


class TestEncryptDecryptBody:
    @pytest.mark.parametrize("body", [
        b"",
        b"x",
        b"\x00" * 1024,
        b"\xff" * 100_000,
    ])
    def test_roundtrip(self, body: bytes):
        dek = generate_dek()
        aad = aad_bytes(
            recording_id=1,
            flow_id="flow-abc",
            event_type="network.request",
            ts_ns=1_000_000_000,
        )
        ciphertext, nonce = encrypt_body(dek, body, aad)
        assert len(nonce) == 12
        assert decrypt_body(ciphertext, nonce, dek, aad) == body

    def test_corruption_raises_invalid_tag(self):
        dek = generate_dek()
        aad = aad_bytes(
            recording_id=1,
            flow_id="flow-abc",
            event_type="network.request",
            ts_ns=1_000_000_000,
        )
        ciphertext, nonce = encrypt_body(dek, b"hello world", aad)
        tampered = bytearray(ciphertext)
        tampered[0] ^= 0xFF
        with pytest.raises(InvalidTag):
            decrypt_body(bytes(tampered), nonce, dek, aad)

    def test_aad_mismatch_raises_invalid_tag(self):
        """Cross-flow-id binding check: ciphertext encrypted under
        ``aad_a`` cannot be decrypted with ``aad_b``."""
        dek = generate_dek()
        aad_a = aad_bytes(
            recording_id=1,
            flow_id="flow-a",
            event_type="network.request",
            ts_ns=1_000_000_000,
        )
        aad_b = aad_bytes(
            recording_id=1,
            flow_id="flow-b",  # different flow id
            event_type="network.request",
            ts_ns=1_000_000_000,
        )
        ciphertext, nonce = encrypt_body(dek, b"hello", aad_a)
        with pytest.raises(InvalidTag):
            decrypt_body(ciphertext, nonce, dek, aad_b)

    def test_empty_body_encrypts_to_short_ciphertext(self):
        """Empty body encrypts cleanly to just the GCM tag (~16 bytes)."""
        dek = generate_dek()
        aad = aad_bytes(
            recording_id=1,
            flow_id="f",
            event_type="network.request",
            ts_ns=1,
        )
        ciphertext, nonce = encrypt_body(dek, b"", aad)
        assert len(ciphertext) == 16  # GCM tag size, no plaintext
        assert decrypt_body(ciphertext, nonce, dek, aad) == b""


# ---------------------------------------------------------------------------
# aad_bytes (LOCKED FORMAT)
# ---------------------------------------------------------------------------


class TestAadBytesLockedFormat:
    def test_aad_bytes_locked_format(self):
        """Hand-encoded fixture guarding the AAD byte format. Any V1.5
        recording made under the prior format becomes undecryptable if
        :func:`aad_bytes` drifts -- DO NOT change this formula without
        a coordinated migration."""
        got = aad_bytes(
            recording_id=12345,
            flow_id="abc-flow",
            event_type="network.request",
            ts_ns=1714262400000000000,
        )
        expected = b'{"f":"abc-flow","r":12345,"t":"network.request","ts":1714262400000000000}'
        assert got == expected, (
            f"AAD format drift detected!\n"
            f"  expected: {expected!r}\n"
            f"  got:      {got!r}\n"
            f"Any V1.5 recording made under the prior format is now "
            f"undecryptable. DO NOT change aad_bytes() formula without "
            f"a coordinated migration."
        )

    def test_keys_appear_in_alphabetical_order(self):
        """``sort_keys=True`` is honored: keys appear as ``f, r, t, ts``."""
        out = aad_bytes(
            recording_id=42,
            flow_id="zzz",
            event_type="network.response",
            ts_ns=1,
        ).decode("ascii")
        idx_f = out.index('"f":')
        idx_r = out.index('"r":')
        idx_t = out.index('"t":')
        idx_ts = out.index('"ts":')
        assert idx_f < idx_r < idx_t < idx_ts

    def test_output_is_ascii_only(self):
        """``ensure_ascii=True`` -> the output is decodable as ASCII."""
        out = aad_bytes(
            recording_id=1,
            flow_id="ascii-id",
            event_type="network.request",
            ts_ns=1,
        )
        # Will raise if any byte is >= 0x80.
        out.decode("ascii")


class TestAadBytesTypeInvariants:
    def test_non_int_recording_id_raises(self):
        with pytest.raises(ValueError, match="recording_id must be int"):
            aad_bytes(
                recording_id="123",  # type: ignore[arg-type]
                flow_id="f",
                event_type="network.request",
                ts_ns=1,
            )

    def test_bool_recording_id_raises(self):
        """``isinstance(True, int)`` is True in Python; the bool guard
        is mandatory or ``aad_bytes(recording_id=True, ...)`` would
        silently produce ``"r":true`` instead of an integer."""
        with pytest.raises(ValueError, match="recording_id must be int"):
            aad_bytes(
                recording_id=True,  # type: ignore[arg-type]
                flow_id="f",
                event_type="network.request",
                ts_ns=1,
            )

    def test_bool_ts_ns_raises(self):
        with pytest.raises(ValueError, match="ts_ns must be int"):
            aad_bytes(
                recording_id=1,
                flow_id="f",
                event_type="network.request",
                ts_ns=False,  # type: ignore[arg-type]
            )

    def test_non_str_flow_id_raises(self):
        with pytest.raises(ValueError, match="flow_id must be str"):
            aad_bytes(
                recording_id=1,
                flow_id=123,  # type: ignore[arg-type]
                event_type="network.request",
                ts_ns=1,
            )

    def test_non_ascii_flow_id_raises(self):
        with pytest.raises(ValueError, match="flow_id must be ASCII"):
            aad_bytes(
                recording_id=1,
                flow_id="flow-é",  # latin-1 'é'
                event_type="network.request",
                ts_ns=1,
            )

    def test_non_ascii_event_type_raises(self):
        with pytest.raises(ValueError, match="event_type must be ASCII"):
            aad_bytes(
                recording_id=1,
                flow_id="f",
                event_type="network.réquest",
                ts_ns=1,
            )

    def test_non_str_event_type_raises(self):
        with pytest.raises(ValueError, match="event_type must be str"):
            aad_bytes(
                recording_id=1,
                flow_id="f",
                event_type=42,  # type: ignore[arg-type]
                ts_ns=1,
            )
