"""Search-by-default readiness gate for screenshot capture (search U8 / KTD5 / R6 / R8).

Resolves whether a recording captures stills — and whether those stills are
encrypted — at ``recording.start``, in ONE pure, unit-testable truth table so the
default never flips on a machine where the guardrails failed to initialize (a bare
constant flip would silently re-open the plaintext exposure).

The rules (KTD5):

- An explicit ``capture_images=false`` ALWAYS wins (user opt-out).
- An explicit ``capture_images=true`` is clamped OFF, with a structured reason, when
  protection is REQUIRED (the corpus is encrypted) but not ready (key ∧ scrub) —
  R8's fail-closed rule extends to explicit requests, preserving the
  never-write-plaintext invariant. On a pre-flip (plaintext) machine an explicit
  true keeps today's plaintext behavior (no key needed).
- An UNSET ``capture_images`` follows the DEFAULT-ON gate: ON iff the corpus is
  encrypted ∧ key present ∧ scrub importable ∧ this recording's scrub is enabled ∧
  a retention bound is set ∧ the disclosure was acknowledged ∧ consent was not
  declined. Any missing conjunct → OFF + a structured reason (recording continues
  video-only, as today).

Stills are encrypted (``capture_images_encrypted``) exactly when they are ON AND the
corpus is encrypted AND a key is present. The same readiness feeds
``content_index_enabled``'s effective default (see ``config.get_content_index_enabled``),
so the two orthogonal flags are resolved by one check.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureGateResult:
    """The resolved capture decision + a structured reason for the daemon log."""

    capture_images: bool
    capture_images_encrypted: bool
    reason: str


def resolve_capture_gate(
    *,
    explicit_capture_images: bool | None,
    scrub_enabled: bool,
    key_present: bool,
    scrub_importable: bool,
    retention_bound_set: bool,
    disclosure_acknowledged: bool,
    consent_declined: bool,
    corpus_encrypted: bool,
) -> CaptureGateResult:
    """Resolve the stills/encryption decision from the readiness inputs (pure)."""
    # Protection (key ∧ scrub) is REQUIRED only once the corpus is encrypted. Before
    # the flip, plaintext capture is today's behavior and needs no key/scrub.
    protection_ready = (not corpus_encrypted) or (key_present and scrub_importable)
    encrypted = corpus_encrypted and key_present

    if explicit_capture_images is False:
        return CaptureGateResult(False, False, "explicit_opt_out")

    if explicit_capture_images is True:
        if not protection_ready:
            return CaptureGateResult(
                False, False, "explicit_true_clamped_protection_not_ready"
            )
        return CaptureGateResult(True, encrypted, "explicit_true")

    # Unset → the default-on gate (R6 disclosure + R8 fail-closed).
    if consent_declined:
        return CaptureGateResult(False, False, "consent_declined")
    if not disclosure_acknowledged:
        return CaptureGateResult(False, False, "disclosure_not_acknowledged")
    ready = (
        corpus_encrypted
        and key_present
        and scrub_importable
        and scrub_enabled
        and retention_bound_set
    )
    if ready:
        return CaptureGateResult(True, encrypted, "default_on")
    return CaptureGateResult(False, False, "default_off_not_ready")


def _key_present() -> bool:
    from screencap import corpus_crypto

    try:
        return corpus_crypto.load_corpus_key() is not None
    except Exception:  # noqa: BLE001 — any key-read failure → "not present" (fail closed)
        return False


def _scrub_importable() -> bool:
    try:
        from screencap.redaction.local_scrub import create_local_scrub_pipeline  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def gather_and_resolve(
    explicit_capture_images: bool | None, scrub_enabled: bool
) -> CaptureGateResult:
    """Gather the live readiness inputs from config/keychain and resolve the gate."""
    from screencap import config

    retention_bound_set = (
        config.get_screenshot_retention_days() > 0
        or config.get_screenshot_size_cap_mb() > 0
    )
    return resolve_capture_gate(
        explicit_capture_images=explicit_capture_images,
        scrub_enabled=scrub_enabled,
        key_present=_key_present(),
        scrub_importable=_scrub_importable(),
        retention_bound_set=retention_bound_set,
        disclosure_acknowledged=config.get_search_disclosure_acknowledged(),
        consent_declined=config.get_content_index_consent_declined(),
        corpus_encrypted=config.get_corpus_encrypted(),
    )


__all__ = ["CaptureGateResult", "resolve_capture_gate", "gather_and_resolve"]
