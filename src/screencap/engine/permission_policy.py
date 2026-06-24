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

import logging
import sys
from typing import Callable, Protocol

from screencap.engine.screen_recorder import Monitor

_logger = logging.getLogger(__name__)


class PermissionPolicy(Monitor, Protocol):
    """TCC permission preflight + revocation polling. ``MacOSTCC`` | ``Noop`` | ``FreshScreenWatch``."""


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
    """Permission policy that skips all TCC interaction.

    Used by session workers (to avoid redundant per-process TCC subprocesses)
    and by tests that need to bypass macOS permission checks entirely.
    """

    @property
    def next_poll_at(self) -> float:
        return float("inf")

    def preflight(self) -> None:
        pass

    def poll(self, now: float) -> None:
        pass


class FreshScreenWatch:
    """Shell/daemon-path policy: cache-immune mid-recording Screen-Recording watch (SCR-106).

    ``Noop`` left the daemon worker with NO robust mid-recording Screen-Recording
    revocation teardown — the standalone ``MacOSTCC`` poll is skipped to avoid
    per-process TCC subprocesses, and the startup preflight (``session.py``) is a
    one-shot check at ``elapsed=0``. The only remaining fallback was the
    capture-health supervisor's screen edge, which is doubly unreliable here:

    * its in-process ``CGPreflightScreenCaptureAccess`` labeller reads the
      per-process TCC cache, pinned to "granted" from the worker's granted start
      (see the module docstring + the linked solution doc); and
    * its attempt-vs-output detector can be defeated by a denial that still
      yields a readable desktop/wallpaper frame (``screen.output`` keeps
      advancing, so the gap never opens) — a documented SCR-76 gap.

    This policy adds the one robust signal: a FRESH-process Screen-Recording read
    on a slow cadence. A spawned child does a fresh TCC lookup (no inherited
    cache) and the read is content-independent (it never inspects a frame), so it
    closes both gaps. On a debounced *explicit* denial it raises
    ``PermissionRevoked("screen_recording")`` so the existing
    ``_run_screen_recorder`` loop emits ``permission_lost`` and stops — the same
    teardown the standalone path uses.

    Fail-open: a probe that returns ``None`` (spawn/timeout/PyObjC error, or a
    non-darwin host) is "couldn't determine" — it resets the streak and never
    tears down a healthy recording.
    """

    def __init__(
        self,
        *,
        interval: float | None = None,
        debounce: int | None = None,
        probe: Callable[[], bool | None] | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._enabled = (sys.platform == "darwin") if enabled is None else enabled
        # Resolve each default from config independently so the type checker can
        # narrow ``interval``/``debounce`` to non-None before the assignments
        # below. Config is still read only when an arg is None (lazy import).
        if interval is None:
            from screencap.engine.config import config as _config

            interval = _config.SCREEN_PERM_WATCH_INTERVAL_SECS
            # Floor the config-sourced interval so a 0/negative env misconfig
            # (SCREEN_PERM_WATCH_INTERVAL_SECS<=0) can't make poll() spawn a fresh
            # probe process every supervisor tick. Mirrors the debounce clamp
            # below; only the env/default path is clamped — an explicit non-None
            # constructor arg (tests pass interval=0.0 with a fake probe) is
            # honoured as-is.
            if interval <= 0:
                interval = 20.0
        if debounce is None:
            from screencap.engine.config import config as _config

            debounce = _config.SCREEN_PERM_WATCH_DEBOUNCE
        self._interval: float = interval
        self._debounce = max(1, int(debounce))
        self._probe = probe
        self._next_poll_at: float = 0.0
        self._denied_streak = 0
        # Counts consecutive inconclusive (``None``) probes so a persistently
        # blind watch surfaces once. Re-armed (set False) on the first non-None
        # probe so a recovered watch can warn again on a later blind spell.
        self._none_streak = 0
        self._none_warned = False

    @property
    def next_poll_at(self) -> float:
        return float("inf") if not self._enabled else self._next_poll_at

    def preflight(self) -> None:
        # Startup TCC is the worker's own fail-fast preflight (session.py); this
        # policy governs only the mid-recording window, so preflight is a no-op
        # here — re-running it would risk a duplicate prompt.
        pass

    def poll(self, now: float) -> None:
        if not self._enabled or now < self._next_poll_at:
            return
        self._next_poll_at = now + self._interval

        granted = self._run_probe()
        if granted is False:
            self._denied_streak += 1
            self._none_streak = 0
            if self._denied_streak >= self._debounce:
                from screencap.engine.screen_recorder import PermissionRevoked

                raise PermissionRevoked("screen_recording")
        elif granted is None:
            # Couldn't determine → fail-open: reset the denied streak so a blind
            # watch never tears down. But a *persistently* inconclusive watch is
            # silently blind (indistinguishable from "granted"), so once it has
            # been blind for ``_debounce`` consecutive probes, surface a one-time
            # advisory. We never raise here — staying fail-open is the contract.
            self._denied_streak = 0
            self._none_streak += 1
            if self._none_streak >= self._debounce and not self._none_warned:
                self._none_warned = True
                _logger.warning(
                    "Screen-Recording permission watch inconclusive for %d "
                    "consecutive probes; mid-recording revocation detection is "
                    "degraded (fail-open, recording continues).",
                    self._none_streak,
                )
        else:
            # True (granted) → fail-open reset of both streaks and re-arm the
            # inconclusive advisory so a later blind spell can warn again.
            self._denied_streak = 0
            self._none_streak = 0
            self._none_warned = False

    def _run_probe(self) -> bool | None:
        if self._probe is not None:
            try:
                return self._probe()
            except Exception:
                return None
        from screencap.engine._screen_perm_probe import probe_screen_recording_granted

        return probe_screen_recording_granted()
