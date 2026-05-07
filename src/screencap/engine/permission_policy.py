"""``PermissionPolicy`` seam (SCR-42, slice 5 of SCR-31).

Promotes the macOS TCC permission preflight (``_check_macos_permissions``)
and mid-recording revocation poll (``_check_permissions_now``) from inline
code in ``_run_screen_recorder`` to a pluggable policy. ``MacOSTCC`` is the
standalone-CLI and seam-default implementation; ``Noop`` is what session
workers and tests use to bypass TCC interaction entirely.

The 5 s polling interval bounds the fresh-TCC subprocess cost (50–100 ms
per call). Combined with SwiftUI's own 5 s poll, the detection SLA stays
under 10 s even when both miss the same transition tick.

See ``docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md``
for why TCC checks use fresh subprocesses rather than in-process PyObjC
calls — macOS caches TCC state per process lifetime, so an in-process call
returns the stale granted-at-startup value even after the user revokes.
"""

from __future__ import annotations

from typing import Protocol

from screencap.engine.screen_recorder import Monitor


class PermissionPolicy(Monitor, Protocol):
    """TCC permission preflight + revocation polling. ``MacOSTCC`` | ``Noop``."""


_INTERVAL = 5.0


class MacOSTCC:
    """Standalone-CLI permission policy: check macOS TCC at startup and poll every 5 s.

    ``preflight`` delegates to ``_check_macos_permissions()`` from
    ``screencap.recorder``, which guides the user through granting missing
    permissions and raises ``SystemExit(1)`` on failure. This will be
    replaced by a typed ``PermissionsMissing`` exception in SCR-45.

    ``poll`` calls ``_check_permissions_now()`` (fresh subprocess per call —
    see module docstring) and raises ``PermissionRevoked(missing)`` if any
    TCC permission was revoked mid-recording.
    """

    def __init__(self) -> None:
        self._next_poll_at: float = 0.0

    def preflight(self) -> None:
        from screencap.recorder import _check_macos_permissions

        _check_macos_permissions()

    def poll(self, now: float) -> None:
        if now < self._next_poll_at:
            return
        self._next_poll_at = now + _INTERVAL

        from screencap.recorder import _check_permissions_now

        ok, missing = _check_permissions_now()
        if not ok:
            from screencap.engine.screen_recorder import PermissionRevoked

            raise PermissionRevoked(missing)

    @property
    def next_poll_at(self) -> float:
        return self._next_poll_at


class Noop:
    """Session-worker / test permission policy: all hooks are safe no-ops.

    Tests and session workers set this policy so they never trigger macOS
    TCC prompts or fresh-subprocess permission checks during recording.
    """

    @property
    def next_poll_at(self) -> float:
        return float("inf")

    def preflight(self) -> None:
        pass

    def poll(self, now: float) -> None:
        pass
