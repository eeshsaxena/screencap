"""KEK / DEK / AAD / body-crypto primitives for V1.5 network body capture.

This module is the SINGLE SOURCE OF TRUTH for all crypto operations on
network capture bodies. Direct callers:

* Unit 5 (pre-flight): :func:`get_or_create_kek`, :func:`generate_dek`,
  :func:`wrap_dek` -- DEK is generated and wrapped before the proxy
  spawns so the wrapped form can be persisted to ``network_event_meta``.
* Unit 4 (capture addon): :func:`encrypt_body`, :func:`aad_bytes` --
  per-body encryption inside the mitmproxy hot path.
* Unit 6 (NetworkScrubPipeline): :func:`get_or_create_kek`,
  :func:`unwrap_dek`, :func:`decrypt_body`, :func:`aad_bytes` --
  export-time decryption + scrub.
* Unit 8 (``--remove-kek`` CLI flag): direct ``keyring.delete_password``
  call against :data:`SERVICE` / :data:`KEK_ACCOUNT`.

Locked decisions (see ``docs/tickets/medium-2026-04-27-feat-network-logging-v1.5-bodies.md``
"Pre-V1.5 decisions" block, ratified 2026-04-29):

* **Keychain ACL = default.** No ``kSecAccessControlUserPresence``, no
  code-signing-pinned ACL. Plain ``keyring.set_password`` -- standard
  "Always Allow" trusted-binary extension. V2 may revisit once the
  SwiftUI v1 phase-5 signing pipeline lands.
* **Service / account names are stable across versions.**
  ``service="com.screencap.network"``, ``account="kek"`` -- no version
  suffix. V2 KEK rotation rewraps in place.
* **AAD format is locked.** Canonical JSON form via :func:`aad_bytes`.
  Any change to the formula breaks every prior V1.5 recording. The
  hand-encoded fixture test in ``tests/network/test_crypto.py`` is a
  permanent guard. Keys: ``f`` (flow_id), ``r`` (recording_id), ``t``
  (event_type), ``ts`` (timestamp_ns) -- alphabetical via
  ``sort_keys=True``, ASCII-only via ``ensure_ascii=True``.

Plan reference: ``docs/plans/2026-04-25-003-feat-network-proxy-logging-plan.md``
Unit 2 (V1.5 portion).

Memory hygiene: V1.5 does NOT mlock or zero DEK plaintext on shutdown.
DEK plaintext lives in Python ``bytes`` for the lifetime of the recorder
process and the export pipeline; we rely on process death for cleanup.
Documented decision -- mlock would require ``ctypes`` + Darwin-specific
syscalls and the threat model already concedes any code running in the
recorder process can read DEK material.
"""

from __future__ import annotations

import base64
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


SERVICE = "com.screencap.network"
"""Keychain ``service`` identifier for the long-lived KEK. Stable across
V1.5 / V2 / V3 -- changing it orphans every prior recording's wrapped
DEK. V2 KEK rotation rewraps in place under this same identifier."""

KEK_ACCOUNT = "kek"
"""Keychain ``account`` identifier. Plain "kek", no version suffix --
locked decision per the V1.5 ticket. Rotation will rewrap in place."""

DEK_WRAP_AAD = b"screencap-network-dek-v1"
"""AAD bytes used by :func:`wrap_dek` / :func:`unwrap_dek` to bind the
wrapped-DEK ciphertext to the "this is a screencap network DEK" context.
Different scheme/version => use a different constant; NEVER reuse this
exact value for a different purpose. The ``-v1`` suffix is a future-proof
marker -- if we ever need to migrate to a new wrap scheme, the new code
emits ``-v2`` and the old code's :func:`unwrap_dek` raises ``InvalidTag``
on the new ciphertext (which is the correct cross-scheme failure mode).
"""

_KEK_LEN = 32
"""Length of the KEK in bytes. AES-256-GCM."""

_DEK_LEN = 32
"""Length of the DEK in bytes. AES-256-GCM."""

_NONCE_LEN = 12
"""AES-GCM nonce length. 96 bits is the AES-GCM standard."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _generate_kek() -> bytes:
    """Return 32 fresh random bytes for use as a KEK.

    Internal helper -- :func:`get_or_create_kek` is the only legitimate
    caller. Mirrors :func:`generate_dek` for symmetry.
    """
    return secrets.token_bytes(_KEK_LEN)


# ---------------------------------------------------------------------------
# KEK lifecycle
# ---------------------------------------------------------------------------


def get_or_create_kek() -> bytes:
    """Return the long-lived 256-bit Key Encryption Key.

    First call generates 32 fresh random bytes via :func:`_generate_kek`
    and stores them via :func:`keyring.set_password` (base64-encoded
    because keyring values are ``str``). On macOS this prompts the user
    via the Keychain dialog the first time the screencap binary asks
    for it; subsequent calls read silently after the user clicks
    "Always Allow".

    Subsequent calls read silently and decode the base64 form back to
    the original 32 bytes.

    Threat model: any same-user "Always Allow"-trusted binary can read
    the KEK silently. This is the V1.5 acceptance per the locked
    decision (see module docstring). V2 considers code-signing-pinned
    ACL once the signing infrastructure ships.

    Edge case: if a previous KEK was stored but the Keychain entry
    has gone missing (Keychain corruption, restore-from-backup, manual
    delete), this function generates a fresh KEK -- any prior recordings
    encrypted under the old KEK become permanently undecryptable. The
    pre-flight (Unit 5) is responsible for printing a user-facing
    warning when it observes a fresh-KEK situation; this helper just
    returns bytes.

    Raises:
        keyring.errors.KeyringError: on Keychain access failure (e.g.
            user cancelled the dialog, locked Keychain, no Keychain
            backend available).
    """
    # Lazy import: keyring's Darwin backend can synchronously dispatch
    # to the Security framework on first access. Module-import-time
    # imports could block ``screencap --help``.
    import keyring

    stored = keyring.get_password(SERVICE, KEK_ACCOUNT)
    if stored is not None:
        return base64.b64decode(stored.encode("ascii"))

    # First call -- generate, persist, return.
    kek = _generate_kek()
    keyring.set_password(SERVICE, KEK_ACCOUNT, base64.b64encode(kek).decode("ascii"))
    return kek


# ---------------------------------------------------------------------------
# DEK generation + wrap/unwrap
# ---------------------------------------------------------------------------


def generate_dek() -> bytes:
    """Return 32 fresh random bytes for use as a per-recording DEK."""
    return secrets.token_bytes(_DEK_LEN)


def wrap_dek(dek: bytes, kek: bytes) -> tuple[bytes, bytes]:
    """AES-GCM-wrap ``dek`` with ``kek``, binding the result to
    :data:`DEK_WRAP_AAD`.

    Returns a ``(ciphertext_with_tag, nonce)`` tuple. Nonce is freshly
    random per call (12 bytes from :mod:`secrets`). Caller persists
    both fields to ``network_event_meta`` (Unit 1).

    Args:
        dek: 32-byte DEK material from :func:`generate_dek`.
        kek: 32-byte KEK material from :func:`get_or_create_kek`.

    Raises:
        ValueError: if ``dek`` is not 32 bytes or ``kek`` is not 32
            bytes. These are programming errors -- fail loud at the
            wrap site rather than producing a wrap that mysteriously
            fails to unwrap downstream.
    """
    if len(kek) != _KEK_LEN:
        raise ValueError(f"wrap_dek: kek must be {_KEK_LEN} bytes (got {len(kek)})")
    if len(dek) != _DEK_LEN:
        raise ValueError(f"wrap_dek: dek must be {_DEK_LEN} bytes (got {len(dek)})")
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = AESGCM(kek).encrypt(nonce, dek, DEK_WRAP_AAD)
    return ciphertext, nonce


def unwrap_dek(wrapped: bytes, nonce: bytes, kek: bytes) -> bytes:
    """Inverse of :func:`wrap_dek`. Returns the original 32-byte DEK.

    Raises:
        cryptography.exceptions.InvalidTag: if the ciphertext was
            tampered with OR if the AAD does not match
            :data:`DEK_WRAP_AAD` (e.g., a wrap from a different scheme
            version). The two cases are indistinguishable by design --
            both are "this ciphertext was not produced by the current
            scheme" failures.
    """
    return AESGCM(kek).decrypt(nonce, wrapped, DEK_WRAP_AAD)


# ---------------------------------------------------------------------------
# AAD construction (LOCKED FORMAT)
# ---------------------------------------------------------------------------


def aad_bytes(
    recording_id: int,
    flow_id: str,
    event_type: str,
    ts_ns: int,
) -> bytes:
    """Construct the AAD for a single body-encryption operation.

    THE ONLY callsite for AAD construction in the codebase. Capture-time
    wrap (Unit 4 addon) and export-time unwrap (Unit 6 scrub pipeline)
    MUST go through this function -- otherwise a one-character drift in
    the formula silently makes every existing recording undecryptable
    (because :func:`decrypt_body` would raise ``InvalidTag`` and there
    is no remediation path short of "re-record everything").

    Format (LOCKED -- DO NOT CHANGE without a coordinated migration)::

        json.dumps(
            {"r": recording_id, "f": flow_id, "t": event_type, "ts": ts_ns},
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")

    Field-type invariants:

    * ``recording_id``: ``int`` (NOT ``float``; ``time.time_ns()`` is int)
    * ``flow_id``: ``str`` (mitmproxy uuid4 hex; ASCII-safe)
    * ``event_type``: ``str`` (e.g. ``"network.request"``; ASCII-safe)
    * ``ts_ns``: ``int`` (``time.time_ns()`` value)

    Non-ASCII inputs are rejected because ``json.dumps`` with
    ``ensure_ascii=True`` emits ``\\uXXXX`` escape sequences for
    non-ASCII characters, but the exact escape choice (lowercase vs
    uppercase hex, surrogate-pair handling) has shifted across
    Python minor versions historically. Refusing non-ASCII at the
    boundary keeps the byte form deterministic across Python upgrades.

    Bool guard: ``isinstance(True, int)`` is ``True`` in Python, so the
    ``int`` checks must explicitly reject ``bool`` -- otherwise
    ``aad_bytes(recording_id=True, ...)`` would silently produce the
    string ``"r":true`` instead of an integer.

    Raises:
        ValueError: with an actionable message on type or ASCII failure.
            Programming errors are surfaced at the AAD construction
            site, not delayed until decryption raises ``InvalidTag``.
    """
    # bool is a subclass of int -- guard explicitly.
    if not isinstance(recording_id, int) or isinstance(recording_id, bool):
        raise ValueError(
            f"aad_bytes: recording_id must be int (got {type(recording_id).__name__})"
        )
    if not isinstance(ts_ns, int) or isinstance(ts_ns, bool):
        raise ValueError(
            f"aad_bytes: ts_ns must be int (got {type(ts_ns).__name__})"
        )
    if not isinstance(flow_id, str):
        raise ValueError(
            f"aad_bytes: flow_id must be str (got {type(flow_id).__name__})"
        )
    if not isinstance(event_type, str):
        raise ValueError(
            f"aad_bytes: event_type must be str (got {type(event_type).__name__})"
        )
    if not flow_id.isascii():
        raise ValueError(
            f"aad_bytes: flow_id must be ASCII (got {flow_id!r})"
        )
    if not event_type.isascii():
        raise ValueError(
            f"aad_bytes: event_type must be ASCII (got {event_type!r})"
        )

    return json.dumps(
        {"r": recording_id, "f": flow_id, "t": event_type, "ts": ts_ns},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Body encrypt / decrypt
# ---------------------------------------------------------------------------


def encrypt_body(dek: bytes, body: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """AES-GCM-encrypt ``body`` with ``dek``, binding to ``aad``.

    Args:
        dek: 32-byte DEK from :func:`generate_dek` (or the unwrapped
            form returned by :func:`unwrap_dek`).
        body: arbitrary bytes -- empty (``b""``) is allowed and
            encrypts cleanly (just the GCM tag, ~16 bytes ciphertext).
            Bodies are capped at 100 KB by network config; this
            function does NOT enforce the cap (the addon does).
        aad: AAD from :func:`aad_bytes` -- pass through verbatim. The
            AAD is not stored inside the ciphertext; the caller MUST
            persist ``aad`` alongside ``ciphertext`` and ``nonce`` to
            ``NetworkEvent.body_aad`` so :func:`decrypt_body` can
            reconstruct it at export time.

    Returns:
        ``(ciphertext_with_tag, nonce)`` -- nonce is freshly random
        (12 bytes). Caller persists both to
        ``NetworkEvent.body_ciphertext`` and ``NetworkEvent.body_nonce``.

    Note: AES-GCM nonce safety relies on never reusing a nonce under
    the same key. We use a fresh 12-byte random nonce per call --
    collision probability for ~2^48 encryptions under the same DEK
    is ~2^-32, well within safety margins for the volume one
    recording produces. Cross-process nonce safety depends on
    ``multiprocessing.set_start_method('spawn')`` so the addon
    process has independently-seeded :mod:`secrets` PRNG state --
    the spawn-mode assertion lives in ``cli.py`` (Unit 4).
    """
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = AESGCM(dek).encrypt(nonce, body, aad)
    return ciphertext, nonce


def decrypt_body(ciphertext: bytes, nonce: bytes, dek: bytes, aad: bytes) -> bytes:
    """Inverse of :func:`encrypt_body`. Returns the original body bytes.

    Args:
        ciphertext: ``NetworkEvent.body_ciphertext`` from the DB.
        nonce: ``NetworkEvent.body_nonce`` from the DB.
        dek: unwrapped DEK from :func:`unwrap_dek`.
        aad: reconstructed via :func:`aad_bytes` from the same
            ``recording_id`` / ``flow_id`` / ``event_type`` / ``ts_ns``
            that were used at capture time. The AAD is NOT pulled from
            ``NetworkEvent.body_aad`` (which is informational / audit
            only); it is recomputed from the canonical fields. If the
            persisted ``body_aad`` and the recomputed AAD differ, the
            scrub pipeline should treat that as an integrity error
            distinct from tampered ciphertext.

    Caller MUST run a ``body_aad IS NULL AND body_ciphertext IS NOT
    NULL`` integrity check BEFORE calling this function. Once
    decryption fires, ``InvalidTag`` from genuine tampering and from a
    missing-AAD bug are indistinguishable. The pre-decrypt check makes
    the integrity error a distinct audit event that can be logged
    separately by Unit 6's pipeline.

    Raises:
        cryptography.exceptions.InvalidTag: on tampered ciphertext OR
            mismatched ``aad`` (e.g., AAD format drift between capture
            and export). The two cases are indistinguishable by design.
    """
    return AESGCM(dek).decrypt(nonce, ciphertext, aad)


__all__ = [
    "SERVICE",
    "KEK_ACCOUNT",
    "DEK_WRAP_AAD",
    "get_or_create_kek",
    "generate_dek",
    "wrap_dek",
    "unwrap_dek",
    "aad_bytes",
    "encrypt_body",
    "decrypt_body",
]
