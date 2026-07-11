"""Corpus at-rest crypto: the corpus key + AES-256-GCM file/blob helpers (search U1).

The **recall corpus** is the searchable local materialization of a recording:
the flat ``screenshots/*.jpg`` stills, the FTS index ``content_index.db``, and the
inline ``png_data`` screenshot blobs in ``recording.db``. R3 of the search-by-default
guardrails plan requires that corpus encrypted at rest. This module owns the single
corpus key and the symmetric primitives every corpus reader/writer shares (engine
capture U2, index-time scrub U3, SQLCipher index U4, retention U5, Swift read-side
U6, migration U7, ``frame.read`` U8), so the crypto discipline lives in exactly one
place — mirroring :mod:`screencap.network.crypto`, the network-body single source.

Key storage mirrors the SCR-241 refresh-token model (:mod:`screencap.auth` +
:mod:`screencap.keychain_group`): the key lives on the data-protection keychain in
the shared access group ``2A8S6MV8DZ.com.screencap.shared`` so the entitled app
daemon and bundled CLI both read it without a prompt. Un-entitled binaries (the
pip/pyenv terminal CLI, Debug ``python3 -m screencap.cli`` builds) get
``errSecMissingEntitlement`` and fall back to the legacy ``keyring`` path — the same
fork ``auth`` uses, so a pip-CLI install (the Problem Frame's live plaintext
exposure) still reaches a ready gate. The daemon-spawned engine subprocess has a
different Keychain ACL identity (exactly like the ID-token channel), so it reads the
key from a ``0600`` file whose *path* is passed via :data:`CORPUS_KEY_FILE_ENV`; the
key bytes never ride ``_worker_args``/argv (base64'd into the command line and
``ps``-visible — ``auth`` forbids that for secrets).

Unlike the auth refresh token (a re-login recovers it), the corpus key is like the
network KEK: **losing it makes the corpus unreadable** — the video is untouched and
remains the source of truth (see SECURITY.md / plan Risks). So :func:`load_corpus_key`
is read-only and NEVER silently regenerates; only :func:`get_or_create_corpus_key`
mints a first key, under a cross-process lock, and it fails closed with
:class:`CorpusKeyUnavailable` when it cannot *persist* one — returning an
un-persisted key would let the next process generate a different one and orphan
every ciphertext written under the first.

AAD binds each ciphertext to its (recording, name) identity so a still or blob can
never be silently swapped across recordings. Callers build the AAD via
:func:`corpus_aad`; the on-disk still convention is ``name = <basename without the
'.enc' suffix>`` (so live-write, redact-and-re-encrypt, and plaintext→encrypted
migration all agree on one AAD for a given frame).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

CORPUS_KEYCHAIN_SERVICE = "screencap-corpus"
"""Keychain ``service`` for the corpus key. Stable across versions — changing it
orphans every prior recording's encrypted corpus (KEK-like, not re-derivable)."""

CORPUS_KEYCHAIN_ACCOUNT = "key"
"""Keychain ``account`` for the corpus key."""

CORPUS_KEY_FILE_ENV = "SCREENCAP_CORPUS_KEY_FILE"
"""Env var naming a ``0600`` file that holds the base64 corpus key. The daemon
writes it and passes the PATH (never the bytes) to the engine subprocess, mirroring
:data:`screencap.auth.ENGINE_TOKEN_FILE_ENV`. Also the headless/test key channel.
When set, it is the SOLE key source (the engine never touches the Keychain)."""

# Shared Keychain access group (SCR-241) — same value + env override as
# :data:`screencap.auth.KEYCHAIN_ACCESS_GROUP`, re-declared here to avoid importing
# the heavier ``auth`` module (which pulls ``requests``) into the capture hot path.
KEYCHAIN_ACCESS_GROUP = os.environ.get(
    "SCREENCAP_KEYCHAIN_ACCESS_GROUP", "2A8S6MV8DZ.com.screencap.shared"
)

_MAGIC = b"SCE1"
"""Corpus-ciphertext file/blob magic (ScreenCap Encrypted, format v1). Prefixes the
nonce so a corpus ``.jpg.enc`` / encrypted ``png_data`` blob is self-framing and
trivially distinguishable from a plaintext JPEG (which starts ``\\xff\\xd8``). A future
scheme emits a new magic; this scheme's :func:`decrypt` then rejects it cleanly."""

_KEY_LEN = 32  # AES-256
_NONCE_LEN = 12  # AES-GCM standard 96-bit nonce
_HEADER_LEN = len(_MAGIC) + _NONCE_LEN


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class CorpusKeyUnavailable(RuntimeError):
    """No corpus key exists and none can be created/persisted on any channel.

    The readiness gate (U8) treats this as "protection not ready" and forces stills
    + indexing OFF (R8 fail-closed). Distinct from a decrypt failure — this is the
    key-lifecycle failure, raised only by :func:`get_or_create_corpus_key`."""


class CorpusDecryptError(Exception):
    """A corpus ciphertext could not be decrypted: bad magic/length, a tampered or
    truncated ciphertext, a wrong key, or an AAD mismatch (e.g. a still swapped
    across recordings). AES-GCM guarantees no plaintext is produced on tag failure,
    so this always means "nothing usable was recovered" — never a partial read."""


# --------------------------------------------------------------------------
# AAD + symmetric primitives
# --------------------------------------------------------------------------


def corpus_aad(recording: str, name: str) -> bytes:
    """Construct the AAD binding a corpus ciphertext to its ``(recording, name)``.

    ``recording`` is the recording directory name; ``name`` is the logical artifact
    identity (for a still: its basename with any trailing ``.enc`` stripped, e.g.
    ``1720000000000.jpg``; for a ``png_data`` blob: a caller-chosen stable label).
    The pair is bound as AAD so a ciphertext moved to a different recording/frame
    fails :func:`decrypt` instead of silently decrypting under the shared key.

    Format (LOCKED): ``json.dumps({"n": name, "r": recording}, separators=(",",":"),
    sort_keys=True, ensure_ascii=True).encode("utf-8")``. Changing it makes every
    prior ciphertext undecryptable, so it is fixed here as the only AAD callsite.
    """
    if not isinstance(recording, str):
        raise ValueError(f"corpus_aad: recording must be str (got {type(recording).__name__})")
    if not isinstance(name, str):
        raise ValueError(f"corpus_aad: name must be str (got {type(name).__name__})")
    return json.dumps(
        {"n": name, "r": recording},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")


def encrypt(plaintext: bytes, key: bytes, aad: bytes) -> bytes:
    """AES-256-GCM-encrypt ``plaintext``, binding to ``aad``.

    Returns a self-framing token ``_MAGIC || nonce(12) || ciphertext+tag`` suitable
    for writing directly to a ``.jpg.enc`` file or a ``png_data`` BLOB column. The
    nonce is freshly random per call (a re-encrypt of the same frame yields new
    bytes — see the idempotence note in U3).
    """
    if len(key) != _KEY_LEN:
        raise ValueError(f"encrypt: key must be {_KEY_LEN} bytes (got {len(key)})")
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return _MAGIC + nonce + ciphertext


def decrypt(token: bytes, key: bytes, aad: bytes) -> bytes:
    """Inverse of :func:`encrypt`. Returns the original plaintext bytes.

    Raises :class:`CorpusDecryptError` on a bad magic/length header, or on a
    tampered/truncated ciphertext, wrong key, or AAD mismatch (all indistinguishable
    by design — every one means "not produced by this scheme for this identity").
    """
    if len(key) != _KEY_LEN:
        raise ValueError(f"decrypt: key must be {_KEY_LEN} bytes (got {len(key)})")
    if len(token) < _HEADER_LEN or token[: len(_MAGIC)] != _MAGIC:
        raise CorpusDecryptError("not a corpus ciphertext (bad magic or too short)")
    nonce = token[len(_MAGIC) : _HEADER_LEN]
    ciphertext = token[_HEADER_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise CorpusDecryptError(
            "corpus decryption failed (tampered ciphertext, wrong key, or AAD mismatch)"
        ) from exc


def is_encrypted(blob: bytes) -> bool:
    """True if ``blob`` carries the corpus-ciphertext magic header.

    Cheap structural check for migration/readers that must tell an already-encrypted
    ``png_data`` blob apart from a plaintext JPEG without attempting a decrypt."""
    return len(blob) >= len(_MAGIC) and blob[: len(_MAGIC)] == _MAGIC


# --------------------------------------------------------------------------
# Key encode/decode + file channel
# --------------------------------------------------------------------------


def _encode_key(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def _decode_key(stored: str) -> bytes | None:
    """Decode a base64 key string; return None (with a warning) if it is not a
    valid 32-byte key — treated as "absent" so the gate degrades to not-ready rather
    than crashing, and a corrupt key never silently masquerades as a good one."""
    try:
        raw = base64.b64decode(stored.encode("ascii"))
    except Exception:  # noqa: BLE001 — any decode failure is "not a usable key"
        logger.warning("corpus key: stored value is not valid base64; treating as absent")
        return None
    if len(raw) != _KEY_LEN:
        logger.warning(
            "corpus key: stored value decodes to %d bytes (expected %d); treating as absent",
            len(raw),
            _KEY_LEN,
        )
        return None
    return raw


def _read_key_file(path: str) -> bytes | None:
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    return _decode_key(text) if text else None


def _write_key_file(path: str, key: bytes) -> None:
    """Write the base64 key to ``path`` with ``0600`` perms (create-or-truncate)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, _encode_key(key).encode("ascii"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)  # tighten if the file pre-existed with looser perms
    except OSError:
        pass


# --------------------------------------------------------------------------
# Key lifecycle
# --------------------------------------------------------------------------


def load_corpus_key() -> bytes | None:
    """Read-only corpus-key lookup across channels; ``None`` when genuinely absent.

    Channel order: the :data:`CORPUS_KEY_FILE_ENV` file (sole source when the env var
    is set — the engine subprocess / headless / test context), else the shared
    Keychain group, else — for an un-entitled binary — the legacy ``keyring`` path.
    NEVER generates: a transiently missing Keychain must not silently orphan the
    corpus (see :func:`get_or_create_corpus_key`)."""
    env_path = os.environ.get(CORPUS_KEY_FILE_ENV)
    if env_path:
        return _read_key_file(env_path)
    return _load_from_keychain()


def _load_from_keychain() -> bytes | None:
    if sys.platform != "darwin":
        import keyring

        stored = keyring.get_password(CORPUS_KEYCHAIN_SERVICE, CORPUS_KEYCHAIN_ACCOUNT)
        return _decode_key(stored) if stored else None

    from screencap import keychain_group

    try:
        stored = keychain_group.load(
            CORPUS_KEYCHAIN_SERVICE, CORPUS_KEYCHAIN_ACCOUNT, KEYCHAIN_ACCESS_GROUP
        )
    except keychain_group.MissingEntitlement:
        # Un-entitled binary (pip/pyenv CLI, Debug): keyring is its primary store and
        # this binary owns the ACL it wrote, so it reads silently (no prompt).
        import keyring

        kr = keyring.get_password(CORPUS_KEYCHAIN_SERVICE, CORPUS_KEYCHAIN_ACCOUNT)
        return _decode_key(kr) if kr else None
    except keychain_group.KeychainError:
        # A non-entitlement Keychain failure (locked / backend error). Treat as
        # absent so the gate is not-ready; NOT a reason to regenerate.
        logger.warning("corpus key: shared-group read failed; treating as absent", exc_info=True)
        return None
    return _decode_key(stored) if stored is not None else None


@contextmanager
def _corpus_key_lock():
    """Serialize corpus-key generation across processes so two concurrent
    first-recordings cannot each mint a different key and orphan one another's
    writes. Best-effort (mirrors :func:`screencap.auth._refresh_lock`): a missing
    lock dir degrades to no cross-process guarantee rather than failing."""
    f = None
    try:
        try:
            f = open(_lock_path(), "w")
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX)
        except (OSError, ImportError):
            f = None
        yield
    finally:
        if f is not None:
            try:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass
            f.close()


def _lock_path() -> Path:
    from screencap.config import get_base_dir

    run_dir = get_base_dir() / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / "corpus-key.lock"


def get_or_create_corpus_key() -> bytes:
    """Return the corpus key, minting + persisting a first one if none exists.

    Called by the daemon/CLI/migration (never the engine subprocess, which only
    reads). Generation happens under :func:`_corpus_key_lock` with a re-check, so a
    generation race resolves to one key. Raises :class:`CorpusKeyUnavailable` when a
    freshly generated key cannot be persisted on any channel — an un-persisted key
    would differ next process and orphan its ciphertext, so we fail closed instead."""
    existing = load_corpus_key()
    if existing is not None:
        return existing
    with _corpus_key_lock():
        existing = load_corpus_key()  # another process may have just minted one
        if existing is not None:
            return existing
        key = secrets.token_bytes(_KEY_LEN)
        _persist_key(key)
        return key


def _persist_key(key: bytes) -> None:
    """Store ``key`` on the first available channel, or raise CorpusKeyUnavailable."""
    encoded = _encode_key(key)

    env_path = os.environ.get(CORPUS_KEY_FILE_ENV)
    if env_path:
        try:
            _write_key_file(env_path, key)
            return
        except OSError as exc:
            raise CorpusKeyUnavailable(
                f"could not write the corpus key file {env_path!r}: {exc}"
            ) from exc

    if sys.platform == "darwin":
        from screencap import keychain_group

        try:
            keychain_group.store(
                CORPUS_KEYCHAIN_SERVICE, CORPUS_KEYCHAIN_ACCOUNT, encoded, KEYCHAIN_ACCESS_GROUP
            )
            return
        except keychain_group.MissingEntitlement:
            pass  # un-entitled → keyring fallback below
        except keychain_group.KeychainError as exc:
            raise CorpusKeyUnavailable(
                f"could not store the corpus key in the shared Keychain group (status {exc.status})"
            ) from exc

    import keyring

    try:
        keyring.set_password(CORPUS_KEYCHAIN_SERVICE, CORPUS_KEYCHAIN_ACCOUNT, encoded)
    except Exception as exc:  # noqa: BLE001 — any keyring backend failure = can't persist
        raise CorpusKeyUnavailable(f"could not store the corpus key in keyring: {exc}") from exc


__all__ = [
    "CORPUS_KEYCHAIN_SERVICE",
    "CORPUS_KEYCHAIN_ACCOUNT",
    "CORPUS_KEY_FILE_ENV",
    "KEYCHAIN_ACCESS_GROUP",
    "CorpusKeyUnavailable",
    "CorpusDecryptError",
    "corpus_aad",
    "encrypt",
    "decrypt",
    "is_encrypted",
    "load_corpus_key",
    "get_or_create_corpus_key",
]
