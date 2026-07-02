"""Identity-scoped TCC decoy/orphan cleanup at install (U4 / R5, R6, R8).

After the daemon helper bundle migration (SCR-196) the Privacy panes can show
look-alike decoy rows next to the daemon's real rows:

- ``screencap`` — the orphaned **bare** pre-SCR-196 identity whose binary no
  longer ships (dead rows in ``TCC.db``).
- ``com.screencap.macos`` — the app's own stray **Accessibility** row, left over
  from older dev builds.

A non-technical user faced with near-identical entries can toggle the wrong one
(the app's, or the orphan's) and leave the daemon ungranted — a silent recording
degradation. This module removes those decoy identities so the correct row
remains per pane (R5) and the revoke path is unambiguous (R8).

**Screen Recording is the exception (SCR-201).** On the nested-LoginItem helper
layout, macOS attributes the daemon's SR request *and* its capture to the
responsible host app (``com.screencap.macos``) — the row renders under the app's
name in the SR pane and is the daemon's *real* SR identity, not a decoy. So we
must NOT reset the app's ``ScreenCapture`` row: doing so deleted the very row the
proactive registration had just created, which is why no SR row auto-appeared.
Accessibility is unaffected — ``AXIsProcessTrustedWithOptions`` attributes to the
daemon's own identity (``com.screencap.daemon``, "ScreencapDaemon"), so the app's
Accessibility row genuinely is a stray decoy and is still cleared.

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
- We never reset any service for the app *except* ``Accessibility`` (its one
  genuine stray row), and always per-bundle-id — its Microphone grant is
  untouched, and its Screen Recording row (the daemon's real SR identity) is
  left alone (SCR-201).
- We never emit a bare ``tccutil reset <service>`` with no bundle id (that wipes
  every app).
- We never target ``com.screencap.daemon`` — resetting it would wipe the
  daemon's *real* grants, the opposite of the goal.

App SR-row provenance (SCR-201, resolved on-device): the shipped **app** process
never creates its own rows — ``CGPreflightScreenCaptureAccess()`` and
``AXIsProcessTrustedWithOptions({prompt: false})`` are read-only. But the
**daemon's** ``CGRequestScreenCaptureAccess()`` registers under the host app's
identity via the responsible-app rollup, so the app's SR row *is* (re)created by
registration — which is exactly why clearing it here broke onboarding. We keep
it. The app's Accessibility row has no such recreation path, so clearing it stays
a harmless one-time hygiene step.

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
# Only ``Accessibility`` may be reset for the app. ``ScreenCapture`` is
# deliberately excluded (SCR-201): on the nested-LoginItem helper layout,
# macOS attributes the daemon's Screen Recording request/capture to the
# responsible host app (``com.screencap.macos``), so the app's SR row IS the
# daemon's real SR identity — not a decoy. Resetting it deletes the very row
# the proactive registration just created. Accessibility is unaffected because
# ``AXIsProcessTrustedWithOptions`` attributes to the daemon's own identity, so
# the app's Accessibility row genuinely is a stray decoy.
ALLOWED_SERVICES_FOR_APP: frozenset[str] = frozenset({ACCESSIBILITY_SERVICE})

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

    Permitted only for the legacy app identity and only for its one genuine
    stray service (Accessibility). Refuses the daemon's own identity outright,
    and refuses any service outside the app allowlist (so Microphone, a bare
    ``All``, or the app's Screen Recording row — the daemon's real SR identity
    via the SCR-201 rollup — can never slip through).
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
    one genuine stray row (Accessibility). Every argv is validated against the
    allowlists in the builders, so an over-broad reset raises
    :class:`_CleanupGuardError` here instead of executing.

    The app's **Screen Recording** row is intentionally left alone (SCR-201): it
    is the daemon's real SR identity via the host-app attribution rollup, so
    resetting it would delete the row the proactive registration just created.
    """
    return [
        _all_reset_command(ORPHAN_BARE_IDENTITY),
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
