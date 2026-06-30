"""Identity-scoped TCC decoy/orphan cleanup at install (U4 / R5, R6, R8).

After the daemon helper bundle migration (SCR-196) the Privacy panes can show
look-alike decoy rows next to the daemon's real "ScreenCap" row:

- ``screencap`` — the orphaned **bare** pre-SCR-196 identity whose binary no
  longer ships (dead rows in ``TCC.db``).
- ``com.screencap.macos`` — the app's own stray Screen Recording / Accessibility
  rows, left over from older dev builds.

A non-technical user faced with three near-identical entries can toggle the
wrong one (the app's, or the orphan's) and leave the daemon ungranted — a
silent recording degradation. This module removes those two decoy identities so
exactly one correct "ScreenCap" row remains per pane (R5), the app never appears
in these panes (R6), and the revoke path is therefore unambiguous (R8).

**This is a destructive surface.** ``tccutil reset`` with the wrong scope wipes
unrelated apps' grants — an untargeted ``tccutil reset ScreenCapture`` clears
*every* app's Screen Recording grant; ``tccutil reset All com.screencap.macos``
would strip the app's own **Microphone** grant. So every ``tccutil`` argv this
module emits is built through a guarded builder that validates the target
against a named allowlist and raises :class:`_CleanupGuardError` before the
command can run. The test suite asserts the exact argv against those allowlists,
because the danger is an over-broad reset, not a missed one.

What we deliberately do NOT do:

- We never ``reset All`` anything but the orphan bare ``screencap`` identity —
  ``All`` is only safe for an identity that no longer ships, so clearing every
  service cannot strip a grant a live process relies on.
- We never reset any service for the app *except* ``ScreenCapture`` and
  ``Accessibility`` (the only two stray rows), and always per-bundle-id — its
  Microphone grant is untouched.
- We never emit a bare ``tccutil reset <service>`` with no bundle id (that wipes
  every app).
- We never target ``com.screencap.daemon`` — resetting it would wipe the
  daemon's *real* grants, the opposite of the goal.

App-row recreation (resolved on-device): the **shipped** app only ever *reads*
its permission state — ``CGPreflightScreenCaptureAccess()`` and
``AXIsProcessTrustedWithOptions({prompt: false})`` are read-only and do not
create a TCC row; the request APIs are gone from the app path (see
``PermissionController.checkScreenRecording``/``checkAccessibility`` and the
comment at ``requestDaemonPermission``). So clearing the app's rows is hygiene
that is a no-op on shipped builds — there is no app code path that recreates
them, hence nothing to gate for R6.

The cleanup is best-effort and fail-soft: a non-zero ``tccutil`` exit (e.g. "No
such bundle identifier" when a decoy row never existed) is tolerated and never
fails an otherwise-successful install. It must be invoked behind the same
first-install/once-per-version gate as the proactive registration (U3), so the
resets do not repeatedly clear a row the user is mid-interaction with.
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

TCCUTIL = "tccutil"

# The orphaned bare pre-SCR-196 identity. ``tccutil reset All <id>`` is only ever
# safe for an identity whose binary no longer ships — clearing *all* its services
# cannot strip a grant a live process depends on. NEVER add a live bundle id
# (``com.screencap.macos`` would lose Microphone; ``com.screencap.daemon`` would
# lose the daemon's real grants).
ORPHAN_BARE_IDENTITY = "screencap"
ALLOWED_ALL_RESET_IDENTITIES: frozenset[str] = frozenset({ORPHAN_BARE_IDENTITY})

# The legacy app identity whose stray Screen Recording / Accessibility rows we
# clear so only the daemon's row remains (R6).
APP_IDENTITY = "com.screencap.macos"

# The daemon's own identity — its real grants must NEVER be reset by this path.
DAEMON_IDENTITY = "com.screencap.daemon"

# tccutil service constants (NOT "ScreenRecording" — the service is
# ``ScreenCapture``; Accessibility is ``Accessibility``). Microphone
# (``Microphone``) is intentionally absent: the app keeps that grant.
SCREEN_CAPTURE_SERVICE = "ScreenCapture"
ACCESSIBILITY_SERVICE = "Accessibility"
ALLOWED_SERVICES_FOR_APP: frozenset[str] = frozenset(
    {SCREEN_CAPTURE_SERVICE, ACCESSIBILITY_SERVICE}
)

_TCCUTIL_TIMEOUT_SECONDS = 10.0


class _CleanupGuardError(RuntimeError):
    """A built ``tccutil`` argv violated a safety allowlist.

    This is a programming error (a hardcoded identity/service drifted out of an
    allowlist), never a user-facing path: :func:`build_cleanup_commands` only
    ever passes allowlisted values. The guard exists so the test suite — and a
    fail-closed runtime — catch an over-broad reset *before* it executes rather
    than after it has wiped a grant.
    """


def _all_reset_command(identity: str) -> list[str]:
    """``tccutil reset All <identity>`` — permitted ONLY for the orphan identity."""
    if identity not in ALLOWED_ALL_RESET_IDENTITIES:
        raise _CleanupGuardError(
            "`tccutil reset All` is only permitted for "
            f"{sorted(ALLOWED_ALL_RESET_IDENTITIES)}, never {identity!r} "
            "(a live identity would lose every grant, incl. Microphone)"
        )
    return [TCCUTIL, "reset", "All", identity]


def _service_reset_command(service: str, identity: str) -> list[str]:
    """``tccutil reset <service> <identity>`` — per-service, per-bundle-id.

    Permitted only for the legacy app identity and only for the two stray
    services. Refuses the daemon's own identity outright, and refuses any
    service outside the app allowlist (so Microphone / a bare ``All`` can never
    slip through).
    """
    if identity == DAEMON_IDENTITY:
        raise _CleanupGuardError(
            f"refusing to reset {DAEMON_IDENTITY!r} — that would wipe the "
            "daemon's real grants"
        )
    if identity != APP_IDENTITY:
        raise _CleanupGuardError(
            f"per-service reset is only permitted for {APP_IDENTITY!r}, "
            f"not {identity!r}"
        )
    if service not in ALLOWED_SERVICES_FOR_APP:
        raise _CleanupGuardError(
            f"service {service!r} is not in the app reset allowlist "
            f"{sorted(ALLOWED_SERVICES_FOR_APP)}"
        )
    return [TCCUTIL, "reset", service, identity]


def build_cleanup_commands() -> list[list[str]]:
    """Build the exact, allowlist-checked ``tccutil`` argv list run at install.

    Order: clear the orphan bare identity wholesale, then strip the legacy app's
    two stray rows per-service. Every argv is validated against the allowlists in
    the builders, so an over-broad reset raises :class:`_CleanupGuardError` here
    instead of executing.
    """
    return [
        _all_reset_command(ORPHAN_BARE_IDENTITY),
        _service_reset_command(SCREEN_CAPTURE_SERVICE, APP_IDENTITY),
        _service_reset_command(ACCESSIBILITY_SERVICE, APP_IDENTITY),
    ]


def run_decoy_cleanup() -> None:
    """Run the identity-scoped decoy/orphan cleanup once, best-effort.

    No-op on non-darwin hosts. Each ``tccutil`` invocation is fail-soft: a
    non-zero exit (e.g. "No such bundle identifier" when a decoy row never
    existed) or a spawn failure is logged at debug and tolerated — the cleanup
    must never fail an otherwise-successful install. Idempotent: re-running
    simply re-clears already-absent rows.

    Callers MUST gate this behind a first-install/once-per-version one-shot so it
    does not re-fire on every daemon reconnect.
    """
    if sys.platform != "darwin":
        return
    for argv in build_cleanup_commands():
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_TCCUTIL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("tcc_cleanup: %s did not run", argv, exc_info=True)
            continue
        if result.returncode != 0:
            logger.debug(
                "tcc_cleanup: %s exited %d: %s",
                argv,
                result.returncode,
                (result.stderr or "").strip(),
            )


__all__ = ["build_cleanup_commands", "run_decoy_cleanup"]
