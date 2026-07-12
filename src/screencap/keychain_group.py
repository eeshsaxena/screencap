"""Shared-access-group Keychain storage for the cloud refresh token (SCR-241).

`keyring`'s public API can only reach the legacy login keychain with a
**per-binary trusted-application ACL** — silently readable only by the exact code
identity that wrote it, so every *other* same-team ScreenCap binary that reads it
triggers the macOS "ScreenCap wants to use screencap-auth" authorization prompt.
This module stores the item on the **data-protection keychain** in a **shared
access group** (``kSecAttrAccessGroup``) instead, so every binary signed with the
same Team ID *and* carrying the ``keychain-access-groups`` entitlement reads it
without a prompt, and the grant survives re-signing / app updates.

Binaries that are **not** entitled for the group (the pip/pyenv terminal CLI, the
Debug ``python3 -m screencap.cli`` fallback) can't join it — macOS returns
``errSecMissingEntitlement`` — so :mod:`screencap.auth` falls back to the legacy
``keyring`` path for them (SCR-241 Fork 1A). :func:`load_legacy_noninteractive`
supports the one-time migration: it reads a pre-existing legacy item **without
ever prompting** (Fork 2A).

Design notes (see docs/plans/2026-07-08-001-…-plan.md and its U6 spike):

* **`ctypes` → Security.framework, no new dependency (KTD-1).** We mirror
  ``keyring.backends.macOS.api``'s proven loading strategy — ``find_library`` +
  ``c_void_p.in_dll`` for the ``kSec*`` constants — rather than depend on that
  private, compat-unguaranteed submodule. The spike confirmed all constants
  resolve via ``in_dll`` in source; the frozen-binary reconfirmation rides the
  signed-build smoke check.
* **Data-protection keychain, device-local, daemon-readable (KTD-2).**
  ``kSecUseDataProtectionKeychain=true`` + ``kSecAttrAccessGroup`` +
  ``kSecAttrAccessibleAfterFirstUnlock`` (an all-day daemon must read after login
  even while the screen is locked) + ``kSecAttrSynchronizable`` defaulting to
  false (never sync a refresh token to iCloud Keychain). The flag is a
  parameter because the SCR-220 cloud KEK flips it on in Stage 2; it must ride
  the add attributes AND every query — macOS queries match only
  non-synchronizable items by default, so the synced and non-synced items are
  effectively distinct and each operation must name its flavor.
* **The un-entitled fallback trigger is spike-verified (KTD-4):** an un-entitled
  binary returns exactly ``errSecMissingEntitlement`` (-34018) from *both* store
  and load, so :class:`MissingEntitlement` keys off that. Any *other* unexpected
  status is a real :class:`KeychainError`, never silently masked as un-entitled.
* **Legacy prompt suppression is spike-verified (KTD-5):**
  ``SecKeychainSetUserInteractionAllowed(false)`` fails cleanly
  (``errSecAuthFailed``) instead of prompting on the legacy ACL; we use it as the
  primary suppressant, belt-and-suspenders with ``kSecUseAuthenticationUIFail``.

The `ctypes` marshaling lives in private ``_sec_item_*`` primitives; the public
functions hold only the error-routing policy, so the policy is unit-testable by
stubbing the primitives while the real marshaling is covered by the
macOS-gated round-trip test and the signed-build smoke.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from ctypes import byref, c_char_p, c_int32, c_long, c_ubyte, c_void_p
from functools import lru_cache

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# OSStatus values we branch on (Security/SecBase.h + MacErrors.h).
# --------------------------------------------------------------------------
errSecSuccess = 0
errSecUserCanceled = -128  # userCanceledErr — returned by the UIFail path
errSecAuthFailed = -25293  # returned by SecKeychainSetUserInteractionAllowed(false)
errSecDuplicateItem = -25299
errSecItemNotFound = -25300
errSecInteractionNotAllowed = -25308
errSecMissingEntitlement = -34018  # the spike-verified un-entitled trigger

# Statuses that mean "a legacy item may exist but this binary can't read it
# silently" (or there is none) — the migration treats all of them as
# "not migratable → sign in again" rather than raising or prompting.
_LEGACY_NOT_READABLE = frozenset(
    {errSecItemNotFound, errSecAuthFailed, errSecInteractionNotAllowed, errSecUserCanceled}
)

_kCFStringEncodingUTF8 = 0x08000100


class KeychainError(RuntimeError):
    """A Security.framework call returned an unexpected non-success ``OSStatus``.

    Carries the raw ``status`` so callers can branch (and so a caller that wants
    the fallback can distinguish :class:`MissingEntitlement`).
    """

    def __init__(self, status: int, operation: str) -> None:
        super().__init__(f"{operation} failed with OSStatus {status}")
        self.status = status
        self.operation = operation


class MissingEntitlement(KeychainError):
    """The calling binary is not entitled for the access group (``-34018``).

    The signal for :mod:`screencap.auth` to fall back to the legacy ``keyring``
    path for an un-entitled binary (SCR-241 Fork 1A).
    """


class _SynchronizableAny:
    """Sentinel type for :data:`SYNCHRONIZABLE_ANY` (kept private; use the value)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "SYNCHRONIZABLE_ANY"


SYNCHRONIZABLE_ANY = _SynchronizableAny()
"""Query-only ``synchronizable`` value (``kSecAttrSynchronizableAny``): match
**both** synced and non-synced items in a read.

Illegal in an *add*/*update*/*delete* — a stored item commits to one flavor, and
matching "any" for a delete could destroy a synced item (fleet-wide propagation).
So it is threaded only into :func:`load`. The SCR-253 (Stage 2) cloud KEK reads
with it so a pre-Stage-2 ``synchronizable=false`` item is still found and never
mistaken for absent — which would mint a second key and fork the ciphertext."""


# --------------------------------------------------------------------------
# ctypes binding (lazy, cached) — mirrors keyring/backends/macOS/api.py.
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _frameworks() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    """Load Security.framework + CoreFoundation and pin the prototypes once."""
    sec = ctypes.CDLL(ctypes.util.find_library("Security"))
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_int32]
    cf.CFDataCreate.restype = c_void_p
    cf.CFDataCreate.argtypes = [c_void_p, c_char_p, c_long]
    cf.CFDataGetLength.restype = c_long
    cf.CFDataGetLength.argtypes = [c_void_p]
    cf.CFDataGetBytePtr.restype = c_void_p
    cf.CFDataGetBytePtr.argtypes = [c_void_p]
    cf.CFDictionaryCreate.restype = c_void_p
    cf.CFDictionaryCreate.argtypes = [c_void_p, c_void_p, c_void_p, c_long, c_void_p, c_void_p]
    cf.CFRelease.argtypes = [c_void_p]

    sec.SecItemAdd.restype = c_int32
    sec.SecItemAdd.argtypes = [c_void_p, c_void_p]
    sec.SecItemCopyMatching.restype = c_int32
    sec.SecItemCopyMatching.argtypes = [c_void_p, c_void_p]
    sec.SecItemUpdate.restype = c_int32
    sec.SecItemUpdate.argtypes = [c_void_p, c_void_p]
    sec.SecItemDelete.restype = c_int32
    sec.SecItemDelete.argtypes = [c_void_p]
    sec.SecKeychainSetUserInteractionAllowed.restype = c_int32
    sec.SecKeychainSetUserInteractionAllowed.argtypes = [c_ubyte]
    return sec, cf


def _const(name: str, lib: ctypes.CDLL) -> c_void_p:
    return c_void_p.in_dll(lib, name)


def _cfstr(cf: ctypes.CDLL, s: str) -> c_void_p:
    return cf.CFStringCreateWithCString(None, s.encode("utf-8"), _kCFStringEncodingUTF8)


def _cfdata(cf: ctypes.CDLL, b: bytes) -> c_void_p:
    return cf.CFDataCreate(None, b, len(b))


def _cfdict(cf: ctypes.CDLL, pairs: list[tuple[c_void_p, c_void_p]]) -> c_void_p:
    # `CFDictionaryCreate` retains its keys/values, so the CFString/CFData
    # temporaries built for a query are held by the returned dict and freed when
    # the caller releases it. The `+1` on each Create'd temporary is intentionally
    # not individually balanced: these calls are rare (startup / login / refresh)
    # and the temporaries are a few bytes each, so per-process leakage is trivial
    # and bounded — not worth the double-free risk of hand-tracking every object.
    keys = (c_void_p * len(pairs))(*[k for k, _ in pairs])
    vals = (c_void_p * len(pairs))(*[v for _, v in pairs])
    key_cb = _const("kCFTypeDictionaryKeyCallBacks", cf)
    val_cb = _const("kCFTypeDictionaryValueCallBacks", cf)
    return cf.CFDictionaryCreate(None, keys, vals, len(pairs), key_cb, val_cb)


def _cfdata_to_bytes(cf: ctypes.CDLL, data: c_void_p) -> bytes:
    length = cf.CFDataGetLength(data)
    ptr = cf.CFDataGetBytePtr(data)
    return ctypes.string_at(ptr, length)


def _base_pairs(cf: ctypes.CDLL, sec: ctypes.CDLL, service: str, account: str) -> list:
    """The class + service + account triple shared by every query."""
    return [
        (_const("kSecClass", sec), _const("kSecClassGenericPassword", sec)),
        (_const("kSecAttrService", sec), _cfstr(cf, service)),
        (_const("kSecAttrAccount", sec), _cfstr(cf, account)),
    ]


def _group_pairs(cf: ctypes.CDLL, sec: ctypes.CDLL, access_group: str) -> list:
    """Access-group + data-protection-keychain selectors."""
    return [
        (_const("kSecAttrAccessGroup", sec), _cfstr(cf, access_group)),
        (_const("kSecUseDataProtectionKeychain", sec), _const("kCFBooleanTrue", cf)),
    ]


def _sync_pair(
    cf: ctypes.CDLL, sec: ctypes.CDLL, synchronizable: "bool | _SynchronizableAny"
) -> list:
    """The ``kSecAttrSynchronizable`` selector. In add *attributes* it sets the
    item's iCloud-sync behavior; in update/copy/delete *queries* it is what lets
    a synced item match at all (queries default to non-synchronizable-only), so
    every primitive carries it. :data:`SYNCHRONIZABLE_ANY` (query-only) selects
    ``kSecAttrSynchronizableAny`` to match either flavor."""
    key = _const("kSecAttrSynchronizable", sec)
    if synchronizable is SYNCHRONIZABLE_ANY:
        # A Security-framework CFString constant, not a CFBoolean (query-only).
        return [(key, _const("kSecAttrSynchronizableAny", sec))]
    value = "kCFBooleanTrue" if synchronizable else "kCFBooleanFalse"
    return [(key, _const(value, cf))]


# --- primitives (real ctypes; stubbed in unit tests) ----------------------


def _sec_item_add(
    service: str, account: str, secret: bytes, access_group: str, *, synchronizable: bool = False
) -> int:
    sec, cf = _frameworks()
    attrs = (
        _base_pairs(cf, sec, service, account)
        + _group_pairs(cf, sec, access_group)
        + _sync_pair(cf, sec, synchronizable)
        + [
            (
                _const("kSecAttrAccessible", sec),
                _const("kSecAttrAccessibleAfterFirstUnlock", sec),
            ),
            (_const("kSecValueData", sec), _cfdata(cf, secret)),
        ]
    )
    d = _cfdict(cf, attrs)
    try:
        return int(sec.SecItemAdd(d, None))
    finally:
        cf.CFRelease(d)


def _sec_item_update(
    service: str, account: str, secret: bytes, access_group: str, *, synchronizable: bool = False
) -> int:
    sec, cf = _frameworks()
    query = _cfdict(
        cf,
        _base_pairs(cf, sec, service, account)
        + _group_pairs(cf, sec, access_group)
        + _sync_pair(cf, sec, synchronizable),
    )
    attrs = _cfdict(cf, [(_const("kSecValueData", sec), _cfdata(cf, secret))])
    try:
        return int(sec.SecItemUpdate(query, attrs))
    finally:
        cf.CFRelease(query)
        cf.CFRelease(attrs)


def _sec_item_copy_matching(
    service: str,
    account: str,
    access_group: str,
    *,
    synchronizable: "bool | _SynchronizableAny" = False,
) -> tuple[int, bytes | None]:
    sec, cf = _frameworks()
    query = _cfdict(
        cf,
        _base_pairs(cf, sec, service, account)
        + _group_pairs(cf, sec, access_group)
        + _sync_pair(cf, sec, synchronizable)
        + [
            (_const("kSecReturnData", sec), _const("kCFBooleanTrue", cf)),
            (_const("kSecMatchLimit", sec), _const("kSecMatchLimitOne", sec)),
        ],
    )
    out = c_void_p(0)
    try:
        status = int(sec.SecItemCopyMatching(query, byref(out)))
        if status == errSecSuccess and out.value:
            data = _cfdata_to_bytes(cf, out)
            cf.CFRelease(out)
            return status, data
        return status, None
    finally:
        cf.CFRelease(query)


def _sec_item_delete(
    service: str, account: str, access_group: str, *, synchronizable: bool = False
) -> int:
    sec, cf = _frameworks()
    query = _cfdict(
        cf,
        _base_pairs(cf, sec, service, account)
        + _group_pairs(cf, sec, access_group)
        + _sync_pair(cf, sec, synchronizable),
    )
    try:
        return int(sec.SecItemDelete(query))
    finally:
        cf.CFRelease(query)


def _sec_item_copy_legacy_noninteractive(service: str, account: str) -> tuple[int, bytes | None]:
    """Read a *legacy* (file-keychain) item with interaction suppressed.

    No ``kSecUseDataProtectionKeychain`` (so it searches the legacy keychain),
    and both suppressants belt-and-suspenders:
    ``SecKeychainSetUserInteractionAllowed(false)`` (primary, legacy-native) plus
    ``kSecUseAuthenticationUI=kSecUseAuthenticationUIFail``. Returns
    ``(OSStatus, secret_bytes | None)``; the caller maps it. Never prompts
    (spike-verified).
    """
    sec, cf = _frameworks()
    query = _cfdict(
        cf,
        _base_pairs(cf, sec, service, account)
        + [
            (_const("kSecReturnData", sec), _const("kCFBooleanTrue", cf)),
            (_const("kSecMatchLimit", sec), _const("kSecMatchLimitOne", sec)),
            (_const("kSecUseAuthenticationUI", sec), _const("kSecUseAuthenticationUIFail", sec)),
        ],
    )
    out = c_void_p(0)
    sec.SecKeychainSetUserInteractionAllowed(0)
    try:
        status = int(sec.SecItemCopyMatching(query, byref(out)))
        if status == errSecSuccess and out.value:
            data = _cfdata_to_bytes(cf, out)
            cf.CFRelease(out)
            return status, data
        return status, None
    finally:
        cf.CFRelease(query)
        sec.SecKeychainSetUserInteractionAllowed(1)


def _sec_item_delete_legacy_noninteractive(service: str, account: str) -> int:
    """Delete a *legacy* (file-keychain) item with interaction suppressed.

    The migration only reaches here after a silent legacy *read* succeeded (so the
    binary is already ACL-trusted), but suppression keeps the "never prompt"
    guarantee even if delete authorization ever diverged from read.
    """
    sec, cf = _frameworks()
    query = _cfdict(cf, _base_pairs(cf, sec, service, account))
    sec.SecKeychainSetUserInteractionAllowed(0)
    try:
        return int(sec.SecItemDelete(query))
    finally:
        cf.CFRelease(query)
        sec.SecKeychainSetUserInteractionAllowed(1)


# --------------------------------------------------------------------------
# Public policy API.
# --------------------------------------------------------------------------


def _raise_for_status(status: int, operation: str) -> None:
    if status == errSecMissingEntitlement:
        raise MissingEntitlement(status, operation)
    raise KeychainError(status, operation)


def store(
    service: str, account: str, secret: str, access_group: str, *, synchronizable: bool = False
) -> None:
    """Write ``secret`` to the access group, add-or-update (idempotent).

    ``synchronizable`` picks the item flavor (default: device-local, never
    synced to iCloud Keychain); the synced and non-synced items are distinct,
    so :func:`load`/:func:`delete` must pass the same value.

    Raises :class:`MissingEntitlement` when the binary is not entitled for the
    group (caller falls back to ``keyring``), or :class:`KeychainError` on any
    other unexpected status.
    """
    data = secret.encode("utf-8")
    status = _sec_item_add(service, account, data, access_group, synchronizable=synchronizable)
    if status == errSecSuccess:
        return
    if status == errSecDuplicateItem:
        status = _sec_item_update(service, account, data, access_group, synchronizable=synchronizable)
        if status == errSecSuccess:
            return
    _raise_for_status(status, "SecItemAdd/Update")


def load(
    service: str,
    account: str,
    access_group: str,
    *,
    synchronizable: "bool | _SynchronizableAny" = False,
) -> str | None:
    """Read the secret from the access group, or ``None`` when absent.

    ``synchronizable`` picks which flavor to match; pass
    :data:`SYNCHRONIZABLE_ANY` to match either (used by the cloud KEK so a
    pre-Stage-2 device-local item is never mistaken for absent).

    Raises :class:`MissingEntitlement` / :class:`KeychainError` as :func:`store`.
    """
    status, data = _sec_item_copy_matching(service, account, access_group, synchronizable=synchronizable)
    if status == errSecSuccess:
        return data.decode("utf-8") if data else None  # normalize empty bytes → None
    if status == errSecItemNotFound:
        return None
    _raise_for_status(status, "SecItemCopyMatching")


def delete(service: str, account: str, access_group: str, *, synchronizable: bool = False) -> None:
    """Delete the access-group item; a missing item is a no-op.

    Raises :class:`MissingEntitlement` / :class:`KeychainError` as :func:`store`.
    """
    status = _sec_item_delete(service, account, access_group, synchronizable=synchronizable)
    if status in (errSecSuccess, errSecItemNotFound):
        return
    _raise_for_status(status, "SecItemDelete")


def load_legacy_noninteractive(service: str, account: str) -> str | None:
    """Read a legacy (login-keychain) item **without ever prompting**.

    Supports the one-time SCR-241 migration: succeeds silently only when the
    calling binary is already in the item's ACL (the single-identity release
    case); for any not-silently-readable / absent status
    (``errSecAuthFailed`` / ``errSecInteractionNotAllowed`` / ``errSecUserCanceled``
    / ``errSecItemNotFound``, all spike-verified as no-prompt outcomes) it
    returns ``None`` so the caller re-logs in. Never raises — migration is
    strictly fail-open.
    """
    try:
        status, data = _sec_item_copy_legacy_noninteractive(service, account)
    except Exception:  # noqa: BLE001 — migration must never raise (fail-open)
        logger.debug("legacy non-interactive read raised; treating as absent", exc_info=True)
        return None
    if status == errSecSuccess:
        return data.decode("utf-8") if data else None  # normalize empty bytes → None
    if status not in _LEGACY_NOT_READABLE:
        # An unexpected status still fails open (no prompt was shown), but log it
        # so a novel macOS behavior is visible rather than silently a re-login.
        logger.debug("legacy non-interactive read: unexpected OSStatus %s → treating as absent", status)
    return None


def delete_legacy_noninteractive(service: str, account: str) -> bool:
    """Delete a legacy (login-keychain) item **without ever prompting**.

    Returns ``True`` when the item is gone (deleted, or already absent), ``False``
    when it could not be removed (still present). Never prompts, never raises —
    the migration uses the boolean to decide whether to WARN about a duplicate.
    """
    try:
        status = _sec_item_delete_legacy_noninteractive(service, account)
    except Exception:  # noqa: BLE001 — migration cleanup must never raise
        logger.debug("legacy non-interactive delete raised; treating as not-removed", exc_info=True)
        return False
    if status in (errSecSuccess, errSecItemNotFound):
        return True
    logger.debug("legacy non-interactive delete: OSStatus %s (item may persist)", status)
    return False
