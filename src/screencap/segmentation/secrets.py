"""Per-vendor BYO API-key storage + cheap validation (SCR BYO cloud, U2).

The bring-your-own-account cloud providers (OpenAI / Anthropic / Google Gemini,
API-key mechanism) need a pasted API key stored as a **secret**, never in
plaintext ``config.toml`` and never uploaded (R3). This module is the storage +
validation seam:

* **Storage** mirrors :mod:`screencap.auth`'s refresh-token helpers exactly —
  the shared-access-group ``keychain_group`` primitives with the legacy
  ``keyring`` fallback for un-entitled dev builds (SCR-241 Fork 1A). Each vendor
  gets its own generic-password item under a per-vendor **service name**
  (``screencap-openai`` / ``screencap-anthropic`` / ``screencap-gemini``) in the
  *same* already-entitled access group — the access group is what carries the
  entitlement, not the service name, so no provisioning-profile change is needed
  (plan Assumption, verified here). The key maps to the **vendor**, not the
  mechanism: the ``*-cli`` delegation ids hold no key (KTD1 — the CLI owns its
  own auth), so they have no entry here.

* **Validation** is a single cheap authenticated GET/POST per vendor, pinned to
  the vendor's **hardcoded HTTPS host** with TLS verification on and no
  env/config host override — so a plaintext-readable key can only ever reach the
  fixed vendor host. ``2xx`` → valid, ``401``/``403`` → invalid, any network
  error → unknown/unavailable. The HTTP client is imported lazily so importing
  this module stays cloud-free.

Keys are redacted from every log line here (only presence / vendor id / status
class is ever logged). The read-back surface (``--json``) exposes a per-vendor
*presence boolean* only, never the value.

Lazy-decrypt discipline (docs/solutions/…/keychain-auth-prompt-eager-decrypt):
keys are read only when a BYO cloud surface is actually engaged (set / validate /
a live provider call), never eagerly at launch.
"""

from __future__ import annotations

import logging
import sys

from screencap.auth import KEYCHAIN_ACCESS_GROUP

logger = logging.getLogger(__name__)

# The Keychain account under every per-vendor service item. A single account per
# service (one key per vendor); mirrors ``auth.KEYCHAIN_ACCOUNT``.
KEYCHAIN_ACCOUNT = "default"

#: The BYO **API-key** vendors and their Keychain service names. Only the
#: key-mechanism ids appear here; the ``*-cli`` delegation ids own no secret
#: (KTD1) and are deliberately absent. Keyed by the vendor id used as the config
#: ``cloud_provider`` value (``gemini`` is the reconciled BYO-key Gemini, R10).
_VENDOR_SERVICE = {
    "openai": "screencap-openai",
    "anthropic": "screencap-anthropic",
    "gemini": "screencap-gemini",
}

#: The vendor ids that hold an API-key secret (the keys of :data:`_VENDOR_SERVICE`).
KEY_VENDORS = tuple(_VENDOR_SERVICE)


class UnknownVendor(ValueError):
    """The vendor id has no API-key storage (unknown, or a ``*-cli`` id)."""


def _service_for(vendor: str) -> str:
    """Return the Keychain service name for ``vendor`` or raise :class:`UnknownVendor`.

    A ``*-cli`` delegation id (or any unknown id) has no key store — the CLI owns
    its own auth (KTD1) — so this fails loudly rather than inventing a service
    name a caller could then write a secret into.
    """
    try:
        return _VENDOR_SERVICE[vendor]
    except KeyError:
        raise UnknownVendor(
            f"{vendor!r} has no API-key storage (known key vendors: "
            f"{', '.join(KEY_VENDORS)})"
        ) from None


# --------------------------------------------------------------------------
# Keychain wrappers — mirror auth._store/_load/_delete_refresh_token exactly.
# --------------------------------------------------------------------------


def _log_keyring_fallback(vendor: str, operation: str) -> None:
    """Note a per-vendor key op used the legacy ``keyring`` instead of the group.

    A *frozen* (bundled) binary falling back is a silent-degradation signal — it
    is supposed to carry the ``keychain-access-groups`` entitlement, so a
    fallback means a wrong-team / missing-entitlement build (SCR-241 KTD-3) →
    WARN. An un-frozen context (pip/pyenv CLI, Debug fallback) is the expected
    path and logs at debug. Never logs the key.
    """
    if getattr(sys, "frozen", False):
        logger.warning(
            "byo-key keychain: entitled build fell back to legacy keyring for %s "
            "(%s) — access group unavailable (check keychain-access-groups "
            "entitlement + team signing)",
            operation,
            vendor,
        )
    else:
        logger.debug(
            "byo-key keychain: %s (%s) via legacy keyring (un-entitled context)",
            operation,
            vendor,
        )


def store_key(vendor: str, key: str) -> None:
    """Store the API ``key`` for ``vendor`` in the shared access group.

    Mirrors :func:`screencap.auth._store_refresh_token`: the access-group
    primitive add-or-update, falling back to legacy ``keyring`` only on
    :class:`~screencap.keychain_group.MissingEntitlement`. Any other Keychain
    failure raises (the caller reports it) — never masked as un-entitled, so a
    locked/broken Keychain never silently drops the secret onto keyring or
    reports a false success. Never logs the key.
    """
    service = _service_for(vendor)
    if sys.platform == "darwin":
        from screencap import keychain_group

        try:
            keychain_group.store(service, KEYCHAIN_ACCOUNT, key, KEYCHAIN_ACCESS_GROUP)
            logger.debug("byo-key keychain: stored %s key via access group", vendor)
            return
        except keychain_group.MissingEntitlement:
            _log_keyring_fallback(vendor, "store")
        except keychain_group.KeychainError as exc:
            raise RuntimeError(
                f"Couldn't save the {vendor} API key to the Keychain "
                f"(status {exc.status})."
            ) from exc
    import keyring

    keyring.set_password(service, KEYCHAIN_ACCOUNT, key)


def load_key(vendor: str) -> str | None:
    """Return the stored API key for ``vendor``, or ``None`` when absent.

    Mirrors :func:`screencap.auth._load_refresh_token`. Read only when a BYO
    cloud surface is engaged (validate / a live provider call), never eagerly at
    launch (lazy-decrypt). Never logs the key.
    """
    service = _service_for(vendor)
    if sys.platform == "darwin":
        from screencap import keychain_group

        try:
            key = keychain_group.load(service, KEYCHAIN_ACCOUNT, KEYCHAIN_ACCESS_GROUP)
        except keychain_group.MissingEntitlement:
            _log_keyring_fallback(vendor, "load")
        except keychain_group.KeychainError as exc:
            raise RuntimeError(
                f"Couldn't read the {vendor} API key from the Keychain "
                f"(status {exc.status})."
            ) from exc
        else:
            return key or None  # normalize empty → None
    import keyring

    return keyring.get_password(service, KEYCHAIN_ACCOUNT) or None


def delete_key(vendor: str) -> None:
    """Delete the stored API key for ``vendor``; a missing item is a no-op.

    Mirrors :func:`screencap.auth._delete_refresh_token`: try the access-group
    delete, and always also clear any legacy ``keyring`` item so a pre-fallback
    key can't resurrect. Never logs the key.
    """
    service = _service_for(vendor)
    if sys.platform == "darwin":
        from screencap import keychain_group

        try:
            keychain_group.delete(service, KEYCHAIN_ACCOUNT, KEYCHAIN_ACCESS_GROUP)
        except keychain_group.MissingEntitlement:
            _log_keyring_fallback(vendor, "delete")
        except keychain_group.KeychainError as exc:
            logger.warning(
                "byo-key keychain: access-group delete failed for %s (status %s); "
                "clearing legacy item anyway",
                vendor,
                exc.status,
            )
    import keyring

    try:
        keyring.delete_password(service, KEYCHAIN_ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass  # nothing stored — already cleared


def has_key(vendor: str) -> bool:
    """Return whether a key is stored for ``vendor`` — presence only, no value.

    The read-back surface (``settings intelligence --json``) reports this
    boolean per vendor; the key value never appears there. Fails **closed**: any
    Keychain error is reported as "no key" rather than propagating, so a locked
    Keychain degrades the presence flag instead of crashing the read-back.
    """
    try:
        return load_key(vendor) is not None
    except Exception:  # noqa: BLE001 — presence read is best-effort, never fatal
        logger.debug("byo-key keychain: presence read for %s failed", vendor, exc_info=True)
        return False


# --------------------------------------------------------------------------
# Cheap per-vendor validation — hardcoded HTTPS host, no override, TLS verified.
# --------------------------------------------------------------------------

# The validation status a caller maps to UI copy. ``valid`` / ``invalid`` are
# authoritative (the vendor answered); ``unknown`` means we could not reach the
# vendor to decide (network error / timeout) and the key is neither confirmed
# good nor bad.
VALID = "valid"
INVALID = "invalid"
UNKNOWN = "unknown"

# Pinned validation endpoints. HTTPS + hardcoded host, TLS verification ON, and
# NO env/config override — a plaintext-readable key can only ever reach the fixed
# vendor host. Anthropic's count_tokens is a POST with a minimal body; the others
# are cheap authenticated GETs.
_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_ANTHROPIC_COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
_GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# A minimal Anthropic count_tokens body — one user turn, cheapest possible call.
_ANTHROPIC_COUNT_TOKENS_BODY = {
    "model": "claude-3-5-haiku-latest",
    "messages": [{"role": "user", "content": "hi"}],
}
_ANTHROPIC_VERSION = "2023-06-01"

_VALIDATE_TIMEOUT = 10  # seconds — a validity probe must not hang the settings UI


def _status_to_result(status_code: int) -> str:
    """Map an HTTP status to a validation result: 2xx→valid, 401/403→invalid."""
    if 200 <= status_code < 300:
        return VALID
    if status_code in (401, 403):
        return INVALID
    # Any other status (429 rate-limit, 5xx) is inconclusive — the key may be
    # fine; we just couldn't confirm it right now.
    return UNKNOWN


def validate_key(vendor: str, key: str) -> str:
    """Cheaply check ``key`` against ``vendor``'s API. Returns :data:`VALID` /
    :data:`INVALID` / :data:`UNKNOWN`.

    Pinned to the vendor's hardcoded HTTPS host with TLS verification on and no
    host override. ``requests`` is imported lazily. Any network/timeout error →
    :data:`UNKNOWN` (never raises, never logs the key).
    """
    if vendor not in _VENDOR_SERVICE:
        raise UnknownVendor(
            f"{vendor!r} has no API-key validation (known key vendors: "
            f"{', '.join(KEY_VENDORS)})"
        )
    import requests

    try:
        if vendor == "openai":
            resp = requests.get(
                _OPENAI_MODELS_URL,
                headers={"Authorization": f"Bearer {key}"},
                timeout=_VALIDATE_TIMEOUT,
            )
        elif vendor == "anthropic":
            resp = requests.post(
                _ANTHROPIC_COUNT_TOKENS_URL,
                headers={
                    "x-api-key": key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json=_ANTHROPIC_COUNT_TOKENS_BODY,
                timeout=_VALIDATE_TIMEOUT,
            )
        else:  # gemini
            resp = requests.get(
                _GEMINI_MODELS_URL,
                headers={"x-goog-api-key": key},
                timeout=_VALIDATE_TIMEOUT,
            )
    except Exception:  # noqa: BLE001 — any transport error is inconclusive, not fatal
        logger.debug("byo-key validation for %s could not reach the vendor", vendor, exc_info=True)
        return UNKNOWN

    result = _status_to_result(resp.status_code)
    logger.debug("byo-key validation for %s → %s (HTTP %s)", vendor, result, resp.status_code)
    return result
