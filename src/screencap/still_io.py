"""Shared still-image I/O seam for the encrypted recall corpus (search U2/U3/U7).

One light module that every corpus still reader/writer shares, so the
plaintext-``.jpg`` vs encrypted-``.jpg.enc`` distinction and the AAD-from-path
convention live in exactly one place and cannot drift between the capture writer,
the index-time scrub, the migration, and the ``frame.read`` verb.

It depends only on :mod:`screencap.corpus_crypto` (itself a leaf that pulls no
NLP/ML surface), so the capture hot path (engine ``write_screen_event``),
:mod:`screencap.frame_resolve`, the destination-agnostic ``backfill`` package, and
the daemon can all import it without dragging in the heavy ``redaction`` engine.

**AAD convention (LOCKED).** A still lives at
``<recordings>/<recording>/screenshots/<ts>.jpg[.enc]``. Its AAD binds
``(recording-dir-name, "<ts>.jpg")`` — the ``.enc`` suffix is stripped so a live
write, a redact-and-re-encrypt, and a plaintext→encrypted migration of the same
frame all compute one identical AAD. Readers derive the same pair straight from the
on-disk path, so no side-channel is needed to decrypt.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from screencap import corpus_crypto

ENC_SUFFIX = ".enc"
"""Suffix appended to a plaintext still filename to mark it corpus-encrypted
(``<ts>.jpg`` → ``<ts>.jpg.enc``)."""


def is_encrypted_path(path: str | os.PathLike[str]) -> bool:
    """True if ``path`` names an encrypted still (``*.jpg.enc``)."""
    return str(path).endswith(ENC_SUFFIX)


def logical_still_name(path: str | os.PathLike[str]) -> str:
    """The still's logical (plaintext) basename — ``<ts>.jpg``, with any trailing
    ``.enc`` stripped. The stable identity used in the AAD and the DB ``image_path``
    across the plaintext/encrypted split."""
    name = Path(path).name
    return name[: -len(ENC_SUFFIX)] if name.endswith(ENC_SUFFIX) else name


def still_aad_for_path(path: str | os.PathLike[str]) -> bytes:
    """Build the corpus AAD for a still from its on-disk path.

    ``recording`` = the recording directory name (``…/<recording>/screenshots/<f>``);
    ``name`` = :func:`logical_still_name`. Both the writer (before the file exists,
    passing the intended ``.enc`` path) and every reader compute the same value."""
    p = Path(path)
    recording = p.parent.parent.name
    return corpus_crypto.corpus_aad(recording, logical_still_name(p))


def png_blob_aad(recording_start_ts: float, screenshot_ts: float) -> bytes:
    """AAD for an inline ``png_data`` blob in ``recording.db``.

    Blobs have no on-disk path, so their identity binds the recording's start
    timestamp and the screenshot timestamp — both available to the live writer and
    to the migration reading the same rows, preventing a blob being swapped to
    another recording/frame."""
    return corpus_crypto.corpus_aad(f"recdb:{recording_start_ts!r}", f"png:{screenshot_ts:.6f}")


def write_encrypted_still(dest_enc: str | os.PathLike[str], jpeg_bytes: bytes, key: bytes) -> None:
    """Encrypt ``jpeg_bytes`` and write it to ``dest_enc`` atomically at ``0600``.

    Plaintext never lands on disk: the ciphertext is written to a temp file in the
    same directory, fsynced, then ``os.replace``-d into place — a crash mid-write
    leaves at most a ``*.enc.part`` ciphertext temp (never a plaintext ``.jpg`` and
    never a half-written ``.jpg.enc`` a reader could glob)."""
    token = corpus_crypto.encrypt(jpeg_bytes, key, still_aad_for_path(dest_enc))
    _atomic_write_0600(os.fspath(dest_enc), token)


def open_still(path: str | os.PathLike[str], key: bytes | None = None) -> bytes:
    """Read a still's JPEG bytes, decrypting transparently when it is ``*.jpg.enc``.

    The one seam every reader (scrub, migration, frame.read, thumbnails) goes
    through during the plaintext↔encrypted migration window. A plaintext ``.jpg`` is
    returned as-is (``key`` unused); an encrypted ``.jpg.enc`` requires ``key`` and
    is verified against its path-derived AAD. Raises
    :class:`screencap.corpus_crypto.CorpusDecryptError` for a missing key on an
    encrypted still or a decrypt failure."""
    p = Path(path)
    raw = p.read_bytes()
    if is_encrypted_path(p):
        if key is None:
            raise corpus_crypto.CorpusDecryptError(
                f"still {p.name} is encrypted but no corpus key was provided"
            )
        return corpus_crypto.decrypt(raw, key, still_aad_for_path(p))
    return raw


def _atomic_write_0600(dest: str, data: bytes) -> None:
    directory = os.path.dirname(dest) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=ENC_SUFFIX + ".part")
    closed = False
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = [
    "ENC_SUFFIX",
    "is_encrypted_path",
    "logical_still_name",
    "still_aad_for_path",
    "png_blob_aad",
    "write_encrypted_still",
    "open_still",
]
