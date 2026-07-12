"""Client-side E2E encryption for cloud uploads (SCR — E2EE slice, U1).

This module is the SINGLE SOURCE OF TRUTH for the crypto that makes a
cloud upload unreadable by the server. It is the sibling of
``network/crypto.py`` (which protects captured network bodies): both hold a
long-lived KEK in the Keychain and both use ``AESGCM``, but this one
protects *whole recording artifacts in transit and at rest in the object
store*, so the server can only ever hold ciphertext.

Two pieces:

* **Cloud KEK** (:func:`get_cloud_kek` / :func:`get_or_create_cloud_kek`) —
  a 256-bit device-held key under a service distinct from the network KEK and
  the SCR-236 at-rest container key (its lifecycle diverges: future escrow +
  team-key wrapping). Its home is the shared-access-group data-protection
  keychain (SCR-220 KTD-1, ``synchronizable=false`` until Stage 2 flips it),
  with the legacy ``keyring`` fallback for un-entitled binaries and a one-time
  write-verify-then-delete migration of pre-Stage-1 keyring KEKs (KTD-2) —
  mirroring ``corpus_crypto``'s dual-path pattern. A lost KEK means every
  cloud recording encrypted under it is permanently undecryptable —
  documented, not recovered. Creation is **foreground-only** (the one-time
  Keychain ACL prompt needs a foreground process identity); the engine
  subprocess reads a delivered key, never calling
  :func:`get_or_create_cloud_kek`.

* **Framed AEAD** (:func:`encrypt_stream` / :func:`decrypt_to` /
  :class:`EncryptingReader`) — a per-file streaming scheme that seals the
  artifact as a header plus a sequence of AES-256-GCM frames. Streaming both
  directions bounds memory for large video chunks, and the ciphertext length
  is a pure function of the plaintext size so the upload can advertise an
  explicit ``Content-Length``.

Object layout (KTD-2 / KTD-3)::

    HEADER  ||  frame_0  ||  frame_1  ||  ...  ||  frame_{n-1}
    HEADER  = MAGIC(6) version(1) frame_size(4) key_id(8) nonce_prefix(8)   # 27 bytes
    frame_i = AES-256-GCM(key, nonce_i, plaintext_i, aad_i)                 # plaintext_i + 16
    nonce_i = nonce_prefix(8) || counter(4, big-endian)                     # 12 bytes
    aad_i   = HEADER || struct(">IB", i, is_final)

Security properties:

* **No nonce reuse.** ``nonce_prefix`` is 8 fresh random bytes per file; the
  4-byte counter makes each frame's nonce unique within the file. Cross-file
  collision requires an 8-byte prefix collision (negligible). The counter caps
  a file at :data:`MAX_FRAMES` frames; a larger file is rejected.
* **Truncation / reordering defeated.** The final frame is flagged in its AAD.
  Decryption discovers "is this the last frame?" from stream EOF, not from a
  declared length, so a truncated stream decrypts its last received frame with
  ``is_final=1`` while it was sealed ``is_final=0`` → ``InvalidTag``. A swapped
  frame's counter mismatches its AAD/nonce → ``InvalidTag``.
* **Header authenticated.** The full header (version, frame_size, key_id,
  nonce_prefix) is bound as AAD on every frame, so flipping any header field
  fails the tag rather than silently misparsing.

Memory hygiene mirrors ``network/crypto.py``: the key lives in Python ``bytes``
for the process lifetime; we rely on process death, not ``mlock``. Nonce
randomness relies on ``secrets`` being independently seeded per process — the
recorder already runs under ``multiprocessing`` spawn mode.

The key is never logged and never leaves the Keychain / delivered-file
channel; only its :func:`cloud_key_id` (a truncated hash) is written anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import math
import os
import secrets
import struct
import sys
from typing import BinaryIO, Callable, Iterable, Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keychain constants — distinct from network/crypto.py and the SCR-236 key
# ---------------------------------------------------------------------------

CLOUD_SERVICE = "com.screencap.e2ee"
"""Keychain ``service`` for the long-lived cloud-upload KEK. Stable; changing
it orphans every cloud recording encrypted under the old key."""

CLOUD_KEK_ACCOUNT = "kek"
"""Keychain ``account`` for the cloud KEK."""

# Shared Keychain access group (SCR-241) — same value + env override as
# ``screencap.auth.KEYCHAIN_ACCESS_GROUP``, re-declared here to avoid importing
# the heavier ``auth`` module (which pulls ``requests``).
KEYCHAIN_ACCESS_GROUP = os.environ.get(
    "SCREENCAP_KEYCHAIN_ACCESS_GROUP", "2A8S6MV8DZ.com.screencap.shared"
)

CLOUD_KEK_SYNCHRONIZABLE = True
"""SCR-253 (Stage 2, KTD-5): the cloud KEK is written with
``kSecAttrSynchronizable=true`` so it syncs to the user's other Macs via iCloud
Keychain (end-to-end encrypted; neither we nor Apple can read it). Every KEK
*read* uses :data:`keychain_group.SYNCHRONIZABLE_ANY` so a pre-Stage-2
device-local (``synchronizable=false``) item is still found and never mistaken
for absent — which would mint a forking second key. The only permitted delete of
a group KEK item targets the ``synchronizable=false`` flavor (during the one-time
rewrite); deleting a synced item would propagate to ALL the user's Macs."""

# ---------------------------------------------------------------------------
# Framing constants
# ---------------------------------------------------------------------------

MAGIC = b"SCRE2E"
"""Object magic. First byte ``0x53`` ('S'); chosen not to collide with the
leading bytes of any uploaded artifact type (mp4 ``\\x00..ftyp``, PNG
``\\x89PNG``, flac ``fLaC``, json/jsonl ``{``). It is the sole signal that
distinguishes an encrypted object from a legacy plaintext one on download."""

VERSION = 1
"""Header format version — bumped for a future algorithm/key-rotation scheme."""

_KEK_LEN = 32
_NONCE_PREFIX_LEN = 8
_COUNTER_LEN = 4
_NONCE_LEN = _NONCE_PREFIX_LEN + _COUNTER_LEN  # 12, AES-GCM standard
_TAG_LEN = 16
_KEY_ID_LEN = 8

_HEADER_STRUCT = struct.Struct(">BI")  # version (u8), frame_size (u32)
HEADER_LEN = len(MAGIC) + _HEADER_STRUCT.size + _KEY_ID_LEN + _NONCE_PREFIX_LEN  # 27
_FRAME_META_STRUCT = struct.Struct(">IB")  # counter (u32), is_final (u8)

DEFAULT_FRAME_SIZE = 64 * 1024
"""Plaintext bytes per frame. 64 KiB keeps per-frame tag overhead at ~0.02%
while bounding streaming memory."""

MAX_FRAMES = 2**32 - 1
"""Counter is a 32-bit big-endian integer; a file may not exceed this many
frames. At the default frame size this is far past any real recording."""


class CloudCryptoError(Exception):
    """Base class for cloud-crypto failures."""


class CloudKeyMismatch(CloudCryptoError):
    """The object's header key id does not match the available cloud key.

    Distinct from a generic ``InvalidTag`` so callers can surface the
    documented "key lost — this recording is unrecoverable" message rather
    than a corruption-looking failure. Raised before any decryption is
    attempted.
    """


class CloudKeyUnavailable(CloudCryptoError):
    """An object is encrypted but no cloud key is available in this context.

    Distinct from :class:`CloudKeyMismatch` (a *wrong* key) and from a generic
    error so a keyless Mac (SCR-253 U8 — signed in on a second Mac before the
    KEK has synced) gets an actionable "sign in · turn on iCloud Keychain"
    guidance signal, not a corruption-looking failure. Raised before any temp
    file is created, so no partial plaintext is ever written.
    """


# ---------------------------------------------------------------------------
# Key lifecycle
# ---------------------------------------------------------------------------


def cloud_key_id(key: bytes) -> bytes:
    """Return an 8-byte stable identifier for ``key`` (truncated SHA-256).

    Written into the object header so download can detect a key change
    (restore-from-backup mints a fresh key) and raise :class:`CloudKeyMismatch`
    instead of an opaque tag failure. It is a hash prefix, not the key.
    """
    return hashlib.sha256(key).digest()[:_KEY_ID_LEN]


def _encode_kek(kek: bytes) -> str:
    return base64.b64encode(kek).decode("ascii")


def _decode_kek(stored: str) -> bytes:
    return base64.b64decode(stored.encode("ascii"))


def _load_legacy_kek() -> bytes | None:
    """Legacy ``keyring`` home — primary store for un-entitled binaries (the
    pip/pyenv CLI, Debug builds), which own the ACL they wrote and read it
    silently."""
    import keyring  # lazy: Darwin backend can block; keep ``--help`` fast

    stored = keyring.get_password(CLOUD_SERVICE, CLOUD_KEK_ACCOUNT)
    if stored is None:
        return None
    return _decode_kek(stored)


def _group_store_prefer_existing(encoded: str) -> str | None:
    """Write ``encoded`` to the shared group unless an item already exists.

    ``keychain_group.store()`` is add-then-update-on-duplicate, so calling it
    unconditionally would overwrite a concurrently created KEK — forking the
    user's ciphertext across two keys (KTD-2). So: re-read first (matching
    *either* sync flavor, so a device-local pre-Stage-2 item or a synced copy
    from another Mac both win) and keep whatever is there; otherwise write the
    new key **synchronizable** (U7) and return the verified readback (``None``
    when the write cannot be confirmed, so the migration never deletes the
    legacy item on an unverified copy).

    Raises :class:`~screencap.keychain_group.MissingEntitlement` /
    :class:`~screencap.keychain_group.KeychainError` as ``keychain_group``.
    """
    from screencap import keychain_group

    existing = keychain_group.load(
        CLOUD_SERVICE,
        CLOUD_KEK_ACCOUNT,
        KEYCHAIN_ACCESS_GROUP,
        synchronizable=keychain_group.SYNCHRONIZABLE_ANY,
    )
    if existing is not None:
        return existing
    keychain_group.store(
        CLOUD_SERVICE,
        CLOUD_KEK_ACCOUNT,
        encoded,
        KEYCHAIN_ACCESS_GROUP,
        synchronizable=CLOUD_KEK_SYNCHRONIZABLE,
    )
    return keychain_group.load(
        CLOUD_SERVICE,
        CLOUD_KEK_ACCOUNT,
        KEYCHAIN_ACCESS_GROUP,
        synchronizable=keychain_group.SYNCHRONIZABLE_ANY,
    )


def _migrate_legacy_kek_if_needed() -> bytes | None:
    """One-time silent migration of a pre-Stage-1 legacy-``keyring`` KEK into
    the shared access group. Runs only on the entitled path when the group is
    empty (KTD-2, on the ``auth._migrate_legacy_token_if_needed`` template).

    Strictly non-interactive (read *and* delete) and fail-open, with two
    hardenings the re-mintable auth token didn't need: the legacy item is
    deleted only after a **verified readback** of the new group item, and an
    existing group item always wins over the legacy one (never overwritten) —
    a lost or forked KEK is permanently undecryptable ciphertext.
    """
    from screencap import keychain_group

    try:
        stored = keychain_group.load_legacy_noninteractive(CLOUD_SERVICE, CLOUD_KEK_ACCOUNT)
    except Exception:  # noqa: BLE001 — migration must never raise
        return None
    if stored is None:
        return None  # no legacy key / not silently readable → genuinely absent
    try:
        winner = _group_store_prefer_existing(stored)
    except Exception:  # noqa: BLE001 — fail-open: keep decrypting on the legacy key
        logger.debug(
            "cloud kek: legacy→group migration write failed; using legacy key", exc_info=True
        )
        return _decode_kek(stored)
    if winner is None:
        # Write not confirmed by readback — abort: the legacy item stays the
        # key's home until a later attempt verifiably lands.
        logger.warning("cloud kek: group write not confirmed by readback; keeping legacy key")
        return _decode_kek(stored)
    if winner != stored:
        # A concurrent writer landed a group KEK first — it wins (KTD-2). The
        # legacy item stays; its ciphertext may still need it.
        logger.warning("cloud kek: group already holds a different key; legacy key left in place")
        return _decode_kek(winner)
    # Verified readback matches → the group is now the home; drop the legacy copy.
    if keychain_group.delete_legacy_noninteractive(CLOUD_SERVICE, CLOUD_KEK_ACCOUNT):
        logger.info("cloud kek: migrated legacy keyring key into the access group")
    else:
        logger.warning(
            "cloud kek: migrated key to access group but the legacy item persists; "
            "key duplicated across homes until removed"
        )
    return _decode_kek(stored)


def _ensure_group_kek_synchronizable(encoded: str) -> None:
    """One-time rewrite of a pre-Stage-2 device-local (``synchronizable=false``)
    group KEK to ``synchronizable=true`` so it syncs to the user's other Macs via
    iCloud Keychain (SCR-253 U7, KTD-5).

    Entitled path only. Same add-verify-then-delete discipline as U1's migration:
    write the synced flavor, verify a readback, and only then delete the OLD
    device-local item — a delete that **never propagates** (that item never
    synced), unlike deleting a synced item, which would wipe the key from every
    Mac. Fail-open at every step: on any failure the device-local key still
    decrypts locally, and the synced copy is retried on a later call.

    Idempotent and safe against a fork: a no-op once a synced item exists
    (already migrated, or a copy synced in from another Mac), and it only ever
    writes the SAME bytes it read, so it can never overwrite a different key.
    """
    from screencap import keychain_group

    try:
        synced = keychain_group.load(
            CLOUD_SERVICE, CLOUD_KEK_ACCOUNT, KEYCHAIN_ACCESS_GROUP, synchronizable=True
        )
    except keychain_group.MissingEntitlement:
        return  # un-entitled legacy home never syncs
    except keychain_group.KeychainError:
        logger.debug("cloud kek: could not read sync flavor; leaving key as-is", exc_info=True)
        return
    if synced is not None:
        return  # already syncable (migrated here, or synced from another Mac)
    # Only a device-local item exists → establish the synced flavor, verified.
    try:
        keychain_group.store(
            CLOUD_SERVICE,
            CLOUD_KEK_ACCOUNT,
            encoded,
            KEYCHAIN_ACCESS_GROUP,
            synchronizable=True,
        )
        verify = keychain_group.load(
            CLOUD_SERVICE, CLOUD_KEK_ACCOUNT, KEYCHAIN_ACCESS_GROUP, synchronizable=True
        )
    except keychain_group.KeychainError:
        logger.warning("cloud kek: sync-on rewrite failed; key stays device-local")
        return
    if verify != encoded:
        logger.warning(
            "cloud kek: sync-on rewrite not confirmed by readback; key stays device-local"
        )
        return
    # Verified: the synced item is the home. Drop the device-local item — a
    # non-synchronizable delete never reaches the user's other Macs (KTD-5).
    try:
        keychain_group.delete(
            CLOUD_SERVICE, CLOUD_KEK_ACCOUNT, KEYCHAIN_ACCESS_GROUP, synchronizable=False
        )
    except keychain_group.KeychainError:
        logger.warning(
            "cloud kek: synced copy created but the device-local item persists "
            "(harmless duplicate — same key)"
        )
    else:
        logger.info("cloud kek: rewrote device-local key to sync via iCloud Keychain")


def get_cloud_kek() -> bytes | None:
    """Read-only cloud-KEK lookup. Returns ``None`` if none is stored.

    Group-first (KTD-1): the shared access group is the KEK's home for entitled
    binaries; un-entitled ones (``MissingEntitlement``) fall back to the legacy
    ``keyring`` path. An entitled binary whose group is empty attempts the
    one-time legacy migration (KTD-2). The group read matches **either sync
    flavor** (SCR-253 U7: ``SYNCHRONIZABLE_ANY``), so a Stage-1 device-local key
    and a Stage-2 synced key are both found — a present key is never mistaken for
    absent (which would cascade into a forking re-mint). Use from every path that
    must not create or mutate a key: the engine/daemon read path, download, and
    ``e2ee status``/``disable``. Never silently re-creates and never rewrites the
    sync flavor — the one-time sync-on rewrite is foreground-only
    (:func:`get_or_create_cloud_kek`).

    Raises:
        keyring.errors.KeyringError: on legacy Keychain access failure.
        keychain_group.KeychainError: on a non-entitlement group failure —
            never masked as "absent", which could cascade into a re-mint.
    """
    if sys.platform != "darwin":
        return _load_legacy_kek()
    from screencap import keychain_group  # lazy, mirrors corpus_crypto

    try:
        stored = keychain_group.load(
            CLOUD_SERVICE,
            CLOUD_KEK_ACCOUNT,
            KEYCHAIN_ACCESS_GROUP,
            synchronizable=keychain_group.SYNCHRONIZABLE_ANY,
        )
    except keychain_group.MissingEntitlement:
        return _load_legacy_kek()
    if stored is not None:
        return _decode_kek(stored)
    return _migrate_legacy_kek_if_needed()


def get_or_create_cloud_kek() -> bytes:
    """Return the cloud KEK, creating it on first call.

    **Foreground-only.** The first call prompts the macOS Keychain ACL dialog,
    which requires a foreground process identity — call this from interactive
    ``screencap login`` success or an explicit init command, never a daemon or
    engine tick. The engine reads a delivered key instead.

    This is also the single site of the SCR-253 U7 sync-on rewrite: when the key
    already exists as a Stage-1 device-local item, it is rewritten
    ``synchronizable=true`` here (entitled path, fail-open) so a re-``e2ee
    enable`` on the first Mac makes the key reach the user's other Macs. A fresh
    mint is written synchronizable from the start.

    Edge case mirrors ``network/crypto.py``: if the Keychain entry went missing
    (corruption, restore-from-backup, manual delete) a fresh key is minted and
    prior cloud recordings become undecryptable. Callers that care print a
    warning; this helper just returns bytes.

    Raises:
        keyring.errors.KeyringError: on legacy Keychain access failure.
        keychain_group.KeychainError: on a non-entitlement group failure.
    """
    existing = get_cloud_kek()
    if existing is not None:
        if sys.platform == "darwin":
            _ensure_group_kek_synchronizable(_encode_kek(existing))
        return existing
    kek = secrets.token_bytes(_KEK_LEN)
    encoded = _encode_kek(kek)
    if sys.platform == "darwin":
        from screencap import keychain_group

        try:
            winner = _group_store_prefer_existing(encoded)
        except keychain_group.MissingEntitlement:
            pass  # un-entitled binary → legacy keyring home below
        else:
            # A concurrent writer's key wins (KTD-2) — never fork ciphertext
            # across two keys by overwriting.
            return _decode_kek(winner) if winner is not None else kek
    import keyring

    keyring.set_password(CLOUD_SERVICE, CLOUD_KEK_ACCOUNT, encoded)
    return kek


# ---------------------------------------------------------------------------
# Engine key delivery (out-of-band, mirrors auth.ENGINE_TOKEN_FILE_ENV)
# ---------------------------------------------------------------------------

ENGINE_CLOUD_KEY_FILE_ENV = "SCREENCAP_ENGINE_CLOUD_KEY_FILE"
"""Env var pointing the engine subprocess at a daemon-written ``0600`` file
holding the base64 cloud key. Set ONLY on the engine (the subprocess can't read
the Keychain — wrong ACL identity), mirroring ``auth.ENGINE_TOKEN_FILE_ENV``."""


def read_delivered_cloud_key() -> bytes | None:
    """Return the daemon-delivered cloud key, or ``None`` if not in the engine.

    Reads the base64 ``0600`` file pointed at by :data:`ENGINE_CLOUD_KEY_FILE_ENV`.
    ``None`` when the env var is unset (the normal daemon/CLI/interactive context,
    which falls through to the Keychain), and ``None`` when the env var IS set but
    the file is missing / unreadable / empty / malformed — the caller then treats
    that as "no key" and fails closed (never uploads plaintext under the flag).
    Read fresh on every call.
    """
    path = os.environ.get(ENGINE_CLOUD_KEY_FILE_ENV)
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return base64.b64decode(raw)
    except (ValueError, TypeError):
        return None


def resolve_cloud_key() -> bytes | None:
    """The cloud key for the current process context; ``None`` if unavailable.

    Engine subprocess (:data:`ENGINE_CLOUD_KEY_FILE_ENV` set) → the delivered
    file, never the Keychain. Every other context (daemon terminal stage, CLI,
    interactive download) → the Keychain via :func:`get_cloud_kek`. Read-only in
    both cases: creation is foreground-only (:func:`get_or_create_cloud_kek`).
    """
    if os.environ.get(ENGINE_CLOUD_KEY_FILE_ENV):
        return read_delivered_cloud_key()
    return get_cloud_kek()


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------


def frame_count(plaintext_size: int, frame_size: int = DEFAULT_FRAME_SIZE) -> int:
    """Number of frames for ``plaintext_size`` — always at least one.

    An empty artifact still emits one (empty, tagged, final) frame, so the
    object is self-describing and the length formula stays total-derivable.
    """
    if plaintext_size < 0:
        raise ValueError("plaintext_size must be non-negative")
    return max(1, math.ceil(plaintext_size / frame_size))


def ciphertext_length(
    plaintext_size: int, frame_size: int = DEFAULT_FRAME_SIZE
) -> int:
    """Exact encrypted-object length for a plaintext of ``plaintext_size``.

    ``HEADER_LEN + plaintext_size + n_frames * TAG_LEN`` — computable before
    the first byte is sent, so upload can advertise ``Content-Length``.
    """
    n = frame_count(plaintext_size, frame_size)
    return HEADER_LEN + plaintext_size + n * _TAG_LEN


def is_encrypted_prefix(prefix: bytes) -> bool:
    """True if ``prefix`` begins with the encrypted-object magic.

    Download uses this to tell an encrypted object from a legacy plaintext one
    (KTD-6). ``prefix`` need only be the object's first ``len(MAGIC)`` bytes.
    """
    return prefix[: len(MAGIC)] == MAGIC


def _build_header(frame_size: int, key: bytes) -> bytes:
    nonce_prefix = secrets.token_bytes(_NONCE_PREFIX_LEN)
    return (
        MAGIC
        + _HEADER_STRUCT.pack(VERSION, frame_size)
        + cloud_key_id(key)
        + nonce_prefix
    )


def _parse_header(header: bytes) -> tuple[int, bytes, bytes]:
    """Return ``(frame_size, key_id, nonce_prefix)`` from a 27-byte header.

    Raises CloudCryptoError on a bad magic or unsupported version — these
    are pre-decryption structural checks (an unauthenticated header read),
    but every field is also bound as AAD, so a flipped field that slips past
    here still fails the first frame's tag.
    """
    if len(header) != HEADER_LEN or header[: len(MAGIC)] != MAGIC:
        raise CloudCryptoError("not an encrypted cloud object (bad magic)")
    off = len(MAGIC)
    version, frame_size = _HEADER_STRUCT.unpack_from(header, off)
    if version != VERSION:
        raise CloudCryptoError(f"unsupported cloud-object version {version}")
    off += _HEADER_STRUCT.size
    key_id = header[off : off + _KEY_ID_LEN]
    nonce_prefix = header[off + _KEY_ID_LEN : HEADER_LEN]
    return frame_size, key_id, nonce_prefix


def _nonce(nonce_prefix: bytes, index: int) -> bytes:
    return nonce_prefix + struct.pack(">I", index)


def _read_upto(read: Callable[[int], bytes], n: int) -> bytes:
    """Read up to ``n`` bytes, looping until ``n`` reached or EOF."""
    parts: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


# ---------------------------------------------------------------------------
# Encrypt
# ---------------------------------------------------------------------------


def encrypt_stream(
    src: BinaryIO, key: bytes, frame_size: int = DEFAULT_FRAME_SIZE
) -> Iterator[bytes]:
    """Yield the encrypted object for the plaintext read from ``src``.

    Yields the header first, then one ciphertext blob per frame. The final
    frame is discovered by a one-frame read-ahead: a frame is final iff the
    next read is empty (so an empty ``src`` yields exactly one empty final
    frame).
    """
    if frame_size <= 0:
        raise ValueError("frame_size must be positive")
    header = _build_header(frame_size, key)
    yield header
    aead = AESGCM(key)
    read = src.read
    buf = _read_upto(read, frame_size)
    index = 0
    while True:
        nxt = _read_upto(read, frame_size)
        is_final = len(nxt) == 0
        if index >= MAX_FRAMES:
            raise CloudCryptoError("artifact exceeds maximum frame count")
        aad = header + _FRAME_META_STRUCT.pack(index, 1 if is_final else 0)
        yield aead.encrypt(_nonce(header[-_NONCE_PREFIX_LEN:], index), buf, aad)
        if is_final:
            return
        buf = nxt
        index += 1


class EncryptingReader:
    """A ``requests``-compatible read adapter over :func:`encrypt_stream`.

    Exposes ``.read(n)`` (yielding header + frames incrementally) and
    ``__len__`` (the computed ciphertext length), the same surface
    ``_ProgressFile`` gives ``requests`` today — so ``requests.put`` sets an
    explicit ``Content-Length`` and streams rather than buffering the whole
    body or switching to chunked transfer-encoding.

    ``plaintext_size`` must be the exact byte size of ``src`` (e.g.
    ``os.fstat(src.fileno()).st_size`` or a known ``FileInfo.size``).
    """

    def __init__(
        self,
        src: BinaryIO,
        plaintext_size: int,
        key: bytes,
        frame_size: int = DEFAULT_FRAME_SIZE,
    ) -> None:
        self._gen = encrypt_stream(src, key, frame_size)
        self._buf = b""
        self._len = ciphertext_length(plaintext_size, frame_size)

    def __len__(self) -> int:
        return self._len

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunks = [self._buf]
            self._buf = b""
            chunks.extend(self._gen)
            return b"".join(chunks)
        while len(self._buf) < size:
            try:
                self._buf += next(self._gen)
            except StopIteration:
                break
        out, self._buf = self._buf[:size], self._buf[size:]
        return out


# ---------------------------------------------------------------------------
# Decrypt
# ---------------------------------------------------------------------------


def decrypt_to(
    src: BinaryIO,
    dst: BinaryIO,
    key: bytes,
) -> None:
    """Decrypt an encrypted object read from ``src`` into ``dst``.

    Streams frame-by-frame, writing each frame's plaintext only after its tag
    verifies. Raises :class:`CloudKeyMismatch` when the header key id does not
    match ``key`` (before any decryption), and ``InvalidTag`` on a tampered,
    reordered, or truncated stream.
    """
    read = src.read
    header = _read_upto(read, HEADER_LEN)
    frame_size, key_id, nonce_prefix = _parse_header(header)
    if key_id != cloud_key_id(key):
        raise CloudKeyMismatch(
            "cloud object was encrypted under a different key (key lost or rotated)"
        )
    aead = AESGCM(key)
    frame_ct = frame_size + _TAG_LEN
    buf = _read_upto(read, frame_ct)
    index = 0
    while True:
        nxt = _read_upto(read, frame_ct)
        is_final = len(nxt) == 0
        aad = header + _FRAME_META_STRUCT.pack(index, 1 if is_final else 0)
        plaintext = aead.decrypt(_nonce(nonce_prefix, index), buf, aad)
        dst.write(plaintext)
        if is_final:
            return
        buf = nxt
        index += 1


def decrypt_stream_from_chunks(
    chunks: Iterable[bytes], dst: BinaryIO, key: bytes
) -> None:
    """Decrypt a ciphertext delivered as an iterable of byte chunks.

    Convenience wrapper for the download path, which reads from
    ``resp.iter_content(...)``. Buffers the chunk iterator behind a ``.read``
    surface and delegates to :func:`decrypt_to`.
    """

    class _ChunkReader:
        def __init__(self, it: Iterator[bytes]) -> None:
            self._it = it
            self._buf = b""
            self._eof = False

        def read(self, n: int) -> bytes:
            while len(self._buf) < n and not self._eof:
                try:
                    self._buf += next(self._it)
                except StopIteration:
                    self._eof = True
            out, self._buf = self._buf[:n], self._buf[n:]
            return out

    decrypt_to(_ChunkReader(iter(chunks)), dst, key)  # type: ignore[arg-type]


__all__ = [
    "CLOUD_SERVICE",
    "CLOUD_KEK_ACCOUNT",
    "KEYCHAIN_ACCESS_GROUP",
    "MAGIC",
    "VERSION",
    "HEADER_LEN",
    "DEFAULT_FRAME_SIZE",
    "MAX_FRAMES",
    "CloudCryptoError",
    "CloudKeyMismatch",
    "CloudKeyUnavailable",
    "cloud_key_id",
    "get_cloud_kek",
    "get_or_create_cloud_kek",
    "frame_count",
    "ciphertext_length",
    "is_encrypted_prefix",
    "encrypt_stream",
    "EncryptingReader",
    "decrypt_to",
    "decrypt_stream_from_chunks",
]
