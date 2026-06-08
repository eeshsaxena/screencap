"""On-demand daemon-driven TCC registration (U8).

When the user clicks **Grant** in the onboarding walkthrough, the app calls the
``permission.request`` daemon verb, which runs *in the daemon's own process* so
the registration is attributed to the daemon's TCC identity
(``com.screencap.daemon`` — the bare ``screencap`` binary), not the app. This is
the load-bearing constraint from the U7 spike: TCC attributes a request to the
*responsible process*, so a request issued from the app (an already-authorized
context) would register the app, not the daemon. See R5 and
``docs/research/2026-06-05-daemon-tcc-registration-spike.md``.

The spike (Decision A, macOS 26.5.1) confirmed which mechanism registers a
toggleable Settings entry per permission, from the daemon's bare ad-hoc identity
under launchd:

- ``screen_recording`` → ``CGRequestScreenCaptureAccess()`` (the request API).
- ``accessibility``    → ``AXIsProcessTrustedWithOptions({prompt: true})``.
- ``input_monitoring`` → the request API alone did **not** register; a real
  listen-only event-tap *touch* is required (``DarwinPlatform``'s
  ``register_input_monitoring_access``). Input Monitoring is advisory (not
  capture-fatal), so even if the touch fails to register, the app's "open
  Settings" fallback lets the user enable it manually.

Registration is best-effort. Each mechanism is wrapped so an unexpected raise
returns ``False`` (couldn't confirm) rather than propagating — the app re-probes
``daemon.info`` (a fresh subprocess) for the authoritative post-grant state and
opens the matching pane regardless, so a registration hiccup never strands the
user. ``register_permission`` assumes its ``permission`` argument was already
validated against the canonical allowlist by the route handler.
"""

from __future__ import annotations

import logging
import sys

from screencap.daemon.permission_probe import (
    PERMISSION_ACCESSIBILITY,
    PERMISSION_INPUT_MONITORING,
    PERMISSION_KEYS,
    PERMISSION_SCREEN_RECORDING,
)

logger = logging.getLogger(__name__)


def register_permission(permission: str) -> bool:
    """Run the registration mechanism for ``permission`` in this process.

    Returns the request mechanism's advisory grant bool (``True`` == already
    granted at call time). Fails soft to ``False`` on any error or on a
    non-darwin host. ``permission`` must be one of
    :data:`screencap.daemon.permission_probe.PERMISSION_KEYS`; an unknown value
    raises ``ValueError`` (the route validates before calling, so this is a
    defensive guard, not a user-facing path).
    """
    if permission not in PERMISSION_KEYS:
        raise ValueError(f"unknown permission: {permission!r}")

    if sys.platform != "darwin":
        # Non-darwin host (CI / Linux): nothing to register. Report not-granted
        # so callers never read a false positive.
        return False

    try:
        from screencap.engine.platform.darwin import DarwinPlatform
    except Exception:
        logger.debug("permission_register: DarwinPlatform import failed", exc_info=True)
        return False

    mechanisms = {
        PERMISSION_SCREEN_RECORDING: DarwinPlatform.request_screen_recording_access,
        PERMISSION_ACCESSIBILITY: DarwinPlatform.request_accessibility_access,
        PERMISSION_INPUT_MONITORING: DarwinPlatform.register_input_monitoring_access,
    }
    mechanism = mechanisms[permission]
    try:
        return bool(mechanism())
    except Exception:
        logger.debug(
            "permission_register: mechanism for %s raised", permission, exc_info=True
        )
        return False


__all__ = ["register_permission"]
