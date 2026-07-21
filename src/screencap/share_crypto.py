"""Per-recording share-key crypto for share-by-link (SCR-229, U1).

Share-by-link re-encrypts the **masked** cloud copy of one recording under a
fresh per-recording *share key* so an anonymous recipient can decrypt it in the
browser with the key carried in the URL fragment — while the master cloud KEK
never leaves the device.

The share key is unrelated to the cloud KEK. The single per-user KEK
(:mod:`screencap.cloud_crypto`) encrypts *every* recording, so exporting it
would expose the whole corpus; a per-share key decrypts exactly one recording's
share copy and nothing else. The share object reuses the framed AES-256-GCM
format of :mod:`screencap.cloud_crypto` verbatim (same header, frames, AAD), so
the browser decryptor is a straight port of ``decrypt_to`` keyed by the share
key.

Source of the plaintext is the caller's responsibility (the daemon share verb):
it must be the already-masked ``<name>-scrubbed`` cloud copy, never the raw
local screenshots (which are unmasked). This module takes a reader over that
source, detects whether it is KEK-ciphertext (E2EE was on) or server-readable
plaintext (E2EE off), and produces the share object.
"""

from __future__ import annotations

import base64
import secrets
import tempfile
from typing import BinaryIO

from screencap import cloud_crypto

SHARE_KEY_LEN = 32
"""Share-key width in bytes (256-bit, matching the cloud KEK)."""

_SPOOL_MAX = 8 * 1024 * 1024
"""Re-encryption spools the KEK-decrypted plaintext to memory up to this size,
then to a temp file — bounds memory for large video without holding the whole
recording in RAM."""


def mint_share_key() -> bytes:
    """Return a fresh 256-bit CSPRNG share key."""
    return secrets.token_bytes(SHARE_KEY_LEN)


def share_key_to_fragment(key: bytes) -> str:
    """Encode a share key for the URL ``#fragment`` (base64url, unpadded)."""
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


def fragment_to_share_key(fragment: str) -> bytes:
    """Decode a share key from the URL fragment.

    Raises:
        ValueError: the fragment is malformed or not a 32-byte key.
    """
    raw = fragment.encode("ascii")
    key = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    if len(key) != SHARE_KEY_LEN:
        raise ValueError(f"share key must be {SHARE_KEY_LEN} bytes, got {len(key)}")
    return key


def encrypt_for_share(src: BinaryIO, share_key: bytes, dst: BinaryIO) -> None:
    """Encrypt plaintext read from ``src`` under ``share_key`` into ``dst``.

    Reuses the cloud framed AES-256-GCM object format verbatim, so the browser
    decryptor is a port of ``cloud_crypto.decrypt_to`` keyed by the share key.
    """
    for blob in cloud_crypto.encrypt_stream(src, share_key):
        dst.write(blob)


def reencrypt_for_share(
    src: BinaryIO,
    source_key: bytes | None,
    share_key: bytes,
    dst: BinaryIO,
) -> None:
    """Re-encrypt the masked cloud copy read from ``src`` under ``share_key``.

    ``src`` is the ``<name>-scrubbed`` cloud copy. When ``cloud_e2ee_enabled``
    was on the copy is KEK-ciphertext and ``source_key`` is the cloud KEK — it is
    KEK-decrypted first. When E2EE was off the copy is server-readable plaintext
    and ``source_key`` is ``None`` — it is used as-is. Either way the master KEK
    never appears in the output.

    The source shape is detected from the object magic, not the caller's flag
    alone: a plaintext source is treated as plaintext even if a stray
    ``source_key`` is passed, and a ciphertext source with no key fails loudly
    rather than emitting a share of unreadable bytes.

    Raises:
        ValueError: the source is KEK-ciphertext but ``source_key`` is ``None``.
    """
    head = _read_exact(src, len(cloud_crypto.MAGIC))
    chained = _PrefixedReader(head, src)
    if cloud_crypto.is_encrypted_prefix(head):
        if source_key is None:
            raise ValueError("source copy is encrypted but no cloud key was provided")
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX) as tmp:
            cloud_crypto.decrypt_to(chained, tmp, source_key)
            tmp.seek(0)
            encrypt_for_share(tmp, share_key, dst)
    else:
        encrypt_for_share(chained, share_key, dst)


def _read_exact(src: BinaryIO, n: int) -> bytes:
    """Read up to ``n`` bytes, looping until ``n`` reached or EOF."""
    parts: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = src.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


class _PrefixedReader:
    """A ``.read``-surface that yields ``prefix`` first, then delegates to ``rest``.

    Lets :func:`reencrypt_for_share` peek the object magic and still hand the full
    stream (magic included) to the decrypt/encrypt path.
    """

    def __init__(self, prefix: bytes, rest: BinaryIO) -> None:
        self._prefix = prefix
        self._rest = rest

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out = self._prefix + self._rest.read()
            self._prefix = b""
            return out
        out = b""
        if self._prefix:
            out = self._prefix[:n]
            self._prefix = self._prefix[len(out) :]
            n -= len(out)
        if n > 0:
            out += self._rest.read(n)
        return out


__all__ = [
    "SHARE_KEY_LEN",
    "mint_share_key",
    "share_key_to_fragment",
    "fragment_to_share_key",
    "encrypt_for_share",
    "reencrypt_for_share",
]
