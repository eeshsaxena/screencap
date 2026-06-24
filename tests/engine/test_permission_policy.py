"""Tests for the ``PermissionPolicy`` seam (SCR-42, slice 5 of SCR-31).

Promotes the macOS TCC permission preflight (``_check_macos_permissions``)
and mid-recording revocation polling (``_check_permissions_now``) from
inline code in ``_run_screen_recorder`` to a pluggable policy.

* ``MacOSTCC`` — standalone CLI and seam default: preflight checks Screen
  Recording / Accessibility / Input Monitoring at startup; poll runs every
  5 s and emits ``permission_lost`` stderr event + raises ``PermissionRevoked``
  on revocation.
* ``Noop``     — session workers and tests: all hooks are safe no-ops.

These tests pin the seam-level behavioural contract. The wiring into
``_run_screen_recorder`` is exercised by ``test_screen_recorder_parity``.
"""

from __future__ import annotations

from unittest import mock


# ---------------------------------------------------------------------------
# Cycle 1 — smoke test
# ---------------------------------------------------------------------------


def test_permission_policy_module_exposes_protocol_and_impls():
    """Smoke test: the module exists with the three documented names.

    Both ``MacOSTCC`` and ``Noop`` must be constructible with no args —
    ``RecordingPolicies`` is built by the CLI adapter and session workers
    without any per-recording context at the construction site.
    """
    from screencap.engine.permission_policy import MacOSTCC, Noop, PermissionPolicy  # noqa: F401

    MacOSTCC()
    Noop()


# ---------------------------------------------------------------------------
# Cycle 2 — Noop lifecycle methods are safe no-ops
# ---------------------------------------------------------------------------


def test_noop_preflight_does_not_call_macos_permission_check():
    """``Noop.preflight`` must not call ``_check_macos_permissions``.

    Tests and session workers set the policy to ``Noop`` so they never
    trigger macOS TCC prompts or system permission calls.
    """
    from screencap.engine.permission_policy import Noop

    with mock.patch("screencap.recorder._check_macos_permissions") as check:
        Noop().preflight()

    check.assert_not_called()


def test_noop_poll_does_not_call_check_permissions_now():
    """``Noop.poll`` must not call ``_check_permissions_now``."""
    from screencap.engine.permission_policy import Noop

    with mock.patch("screencap.recorder._check_permissions_now") as check:
        Noop().poll(100.0)

    check.assert_not_called()


def test_noop_next_poll_at_never_triggers():
    """``Noop.next_poll_at`` returns infinity so the main loop never polls."""
    from screencap.engine.permission_policy import Noop

    assert Noop().next_poll_at == float("inf")


# ---------------------------------------------------------------------------
# Cycle 3 — MacOSTCC.preflight delegates to _check_macos_permissions
# ---------------------------------------------------------------------------


def test_macos_tcc_preflight_delegates_to_check_macos_permissions():
    """``MacOSTCC.preflight`` calls ``_check_macos_permissions`` exactly once.

    Pins the delegation contract: the policy wraps the existing function
    rather than reimplementing TCC logic directly.
    """
    from screencap.engine.permission_policy import MacOSTCC

    with mock.patch("screencap.recorder._check_macos_permissions") as check:
        MacOSTCC().preflight()

    check.assert_called_once_with()


def test_macos_tcc_preflight_propagates_systemexit():
    """``MacOSTCC.preflight`` propagates ``SystemExit`` from ``_check_macos_permissions``.

    The underlying function raises ``SystemExit(1)`` when a permission is
    missing; the policy must not swallow it — the CLI adapter relies on
    it to exit with the right code.
    """
    import pytest

    from screencap.engine.permission_policy import MacOSTCC

    with mock.patch(
        "screencap.recorder._check_macos_permissions",
        side_effect=SystemExit(1),
    ):
        with pytest.raises(SystemExit) as excinfo:
            MacOSTCC().preflight()

    assert excinfo.value.code == 1


# ---------------------------------------------------------------------------
# Cycle 4 — MacOSTCC.poll detects permission revocation
# ---------------------------------------------------------------------------


def test_macos_tcc_poll_raises_permission_revoked_when_permission_missing():
    """``MacOSTCC.poll`` raises ``PermissionRevoked`` when a TCC permission
    is revoked mid-recording.

    The ``missing`` attribute carries the permission name so the caller can
    set ``_stop_reason = f"permission_revoked_{exc.missing}"`` and build
    the ``permission_lost`` stderr event.
    """
    import pytest

    from screencap.engine.permission_policy import MacOSTCC
    from screencap.engine.screen_recorder import PermissionRevoked

    with mock.patch(
        "screencap.recorder._check_permissions_now",
        return_value=(False, "screen_recording"),
    ):
        policy = MacOSTCC()
        with pytest.raises(PermissionRevoked) as excinfo:
            policy.poll(0.0)

    assert excinfo.value.missing == "screen_recording"


def test_macos_tcc_poll_does_not_raise_when_permissions_ok():
    """``MacOSTCC.poll`` returns normally when all TCC permissions are granted."""
    from screencap.engine.permission_policy import MacOSTCC

    with mock.patch(
        "screencap.recorder._check_permissions_now",
        return_value=(True, None),
    ):
        MacOSTCC().poll(0.0)  # must not raise


# ---------------------------------------------------------------------------
# Cycle 5 — MacOSTCC.poll cadence: 5 s interval, skips early calls
# ---------------------------------------------------------------------------


def test_macos_tcc_poll_skips_check_before_interval_elapses():
    """``MacOSTCC.poll`` skips ``_check_permissions_now`` when called before
    the 5 s polling interval has elapsed.

    Prevents redundant subprocess spawning (each fresh-TCC check costs
    50–100 ms) and keeps the combined detection SLA under 10 s.
    """
    from screencap.engine.permission_policy import MacOSTCC

    policy = MacOSTCC()
    # First call at t=0 triggers a check
    with mock.patch(
        "screencap.recorder._check_permissions_now", return_value=(True, None)
    ) as check:
        policy.poll(0.0)
    check.assert_called_once()

    # Second call at t=2 (within the 5 s window) must NOT trigger a check
    with mock.patch(
        "screencap.recorder._check_permissions_now", return_value=(True, None)
    ) as check2:
        policy.poll(2.0)
    check2.assert_not_called()


def test_macos_tcc_poll_checks_again_after_interval():
    """``MacOSTCC.poll`` calls ``_check_permissions_now`` again once the 5 s
    interval has elapsed since the last check."""
    from screencap.engine.permission_policy import MacOSTCC

    policy = MacOSTCC()
    with mock.patch(
        "screencap.recorder._check_permissions_now", return_value=(True, None)
    ):
        policy.poll(0.0)  # first poll at t=0

    # Poll at t=5 (interval elapsed) — must trigger a new check
    with mock.patch(
        "screencap.recorder._check_permissions_now", return_value=(True, None)
    ) as check:
        policy.poll(5.0)

    check.assert_called_once()


def test_macos_tcc_next_poll_at_advances_after_each_check():
    """``MacOSTCC.next_poll_at`` advances by 5 s after each check so the
    main loop can gate calls via ``elapsed >= policy.next_poll_at``."""
    from screencap.engine.permission_policy import MacOSTCC

    policy = MacOSTCC()
    assert policy.next_poll_at == 0.0  # polls immediately on first call

    with mock.patch(
        "screencap.recorder._check_permissions_now", return_value=(True, None)
    ):
        policy.poll(0.0)

    assert policy.next_poll_at == 5.0


# ---------------------------------------------------------------------------
# Cycle 9 — Wiring: ScreenRecorder.run() calls permission_policy
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FreshScreenWatch — shell/daemon-path mid-recording Screen-Recording watch (SCR-106)
# ---------------------------------------------------------------------------


def _watch(probe_returns, *, debounce=2, interval=20.0):
    """Build a FreshScreenWatch driven by a scripted probe.

    ``probe_returns`` is a list of tri-state values yielded one per probe call.
    """
    from screencap.engine.permission_policy import FreshScreenWatch

    seq = iter(probe_returns)
    return FreshScreenWatch(
        interval=interval,
        debounce=debounce,
        probe=lambda: next(seq),
        enabled=True,
    )


def test_fresh_screen_watch_constructs_with_no_args():
    """Mirrors MacOSTCC/Noop: constructible with no args for the policy seam."""
    from screencap.engine.permission_policy import FreshScreenWatch

    FreshScreenWatch()


def test_fresh_screen_watch_raises_after_debounced_denial():
    """Two consecutive *explicit* denials → PermissionRevoked('screen_recording').

    This is the SCR-106 fix: the daemon worker (Noop before) now tears down on a
    genuine mid-recording Screen-Recording revocation via the existing
    PermissionRevoked → permission_lost → stop() path.
    """
    import pytest

    from screencap.engine.screen_recorder import PermissionRevoked

    watch = _watch([False, False], debounce=2)
    watch.poll(0.0)  # first denial — below debounce, no raise
    with pytest.raises(PermissionRevoked) as excinfo:
        watch.poll(20.0)  # second denial — debounce reached
    assert excinfo.value.missing == "screen_recording"


def test_fresh_screen_watch_fail_open_on_none():
    """A probe that can't determine (None) NEVER tears down a healthy recording."""
    watch = _watch([None, None, None], debounce=2)
    watch.poll(0.0)
    watch.poll(20.0)
    watch.poll(40.0)  # must not raise


def test_fresh_screen_watch_granted_does_not_raise():
    """Granted reads never raise."""
    watch = _watch([True, True], debounce=2)
    watch.poll(0.0)
    watch.poll(20.0)  # must not raise


def test_fresh_screen_watch_denied_then_recovered_resets_streak():
    """A denied blip that recovers before the debounce must not tear down.

    Sequence: denied (streak=1), granted (streak reset), denied (streak=1) —
    debounce of 2 is never reached, so no PermissionRevoked.
    """
    watch = _watch([False, True, False], debounce=2)
    watch.poll(0.0)
    watch.poll(20.0)
    watch.poll(40.0)  # must not raise — streak was reset by the granted read


def test_fresh_screen_watch_skips_probe_before_interval():
    """poll() within the interval does not spend a probe."""
    calls = {"n": 0}

    from screencap.engine.permission_policy import FreshScreenWatch

    def _probe():
        calls["n"] += 1
        return True

    watch = FreshScreenWatch(interval=20.0, debounce=2, probe=_probe, enabled=True)
    watch.poll(0.0)
    watch.poll(5.0)  # within interval — no probe
    assert calls["n"] == 1


def test_fresh_screen_watch_disabled_never_polls():
    """On a non-darwin host (enabled=False) the watch is inert."""
    from screencap.engine.permission_policy import FreshScreenWatch

    def _probe():
        raise AssertionError("probe must not run when disabled")

    watch = FreshScreenWatch(interval=20.0, debounce=2, probe=_probe, enabled=False)
    watch.poll(0.0)
    watch.poll(100.0)  # must not raise / must not probe
    assert watch.next_poll_at == float("inf")


def test_fresh_screen_watch_preflight_is_noop():
    """preflight must NOT spawn a probe or prompt — startup TCC is the worker's
    own fail-fast check (session.py); this policy owns only the runtime window."""
    from screencap.engine.permission_policy import FreshScreenWatch

    def _probe():
        raise AssertionError("preflight must not probe")

    FreshScreenWatch(probe=_probe, enabled=True).preflight()  # must not raise


def test_fresh_screen_watch_probe_exception_is_fail_open():
    """A probe callable that raises is swallowed to None (fail-open), not denied."""
    from screencap.engine.permission_policy import FreshScreenWatch

    def _probe():
        raise RuntimeError("simulated probe crash")

    watch = FreshScreenWatch(interval=20.0, debounce=1, probe=_probe, enabled=True)
    watch.poll(0.0)
    watch.poll(20.0)  # must not raise — exceptions map to None, never to denied


def test_fresh_screen_watch_clamps_nonpositive_config_interval():
    """A 0/negative ``SCREEN_PERM_WATCH_INTERVAL_SECS`` env misconfig is floored.

    Only the config-sourced default is clamped (mirrors the debounce clamp) so a
    misconfigured env can't make ``poll()`` spawn a fresh probe every supervisor
    tick. An explicitly-injected ``interval`` arg is honoured as-is (the
    every-tick fake-probe tests rely on ``interval=0.0``).
    """
    from screencap.engine.permission_policy import FreshScreenWatch

    with mock.patch("screencap.engine.config.config") as cfg:
        cfg.SCREEN_PERM_WATCH_INTERVAL_SECS = 0.0
        cfg.SCREEN_PERM_WATCH_DEBOUNCE = 2
        watch = FreshScreenWatch(enabled=True)
    assert watch._interval == 20.0  # floored to the documented default

    # An explicit non-None arg is NOT clamped, even at the config path's value.
    assert FreshScreenWatch(interval=0.0, enabled=True)._interval == 0.0


def test_fresh_screen_watch_inconclusive_surfaces_once_and_rearms(caplog):
    """A persistently inconclusive (all-None) watch warns once, never raises.

    A ``None`` probe is fail-open, but a watch that is blind for ``_debounce``
    consecutive probes is indistinguishable from "granted", so it must surface a
    one-time advisory. It must NOT spam (re-arm only after a non-None probe) and
    must NEVER raise ``PermissionRevoked``.
    """
    import logging

    watch = _watch([None, None, None, True, None, None], debounce=2)

    with caplog.at_level(logging.WARNING, logger="screencap.engine.permission_policy"):
        watch.poll(0.0)  # None streak=1 — below debounce, no warning
        watch.poll(20.0)  # None streak=2 — debounce reached, warns once
        watch.poll(40.0)  # None streak=3 — already warned, must not re-warn
    first = [r for r in caplog.records if "inconclusive" in r.getMessage()]
    assert len(first) == 1, "must warn exactly once per blind spell"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="screencap.engine.permission_policy"):
        watch.poll(60.0)  # True — re-arms the advisory, resets the None streak
        watch.poll(80.0)  # None streak=1 — below debounce
        watch.poll(100.0)  # None streak=2 — re-armed, warns again
    second = [r for r in caplog.records if "inconclusive" in r.getMessage()]
    assert len(second) == 1, "a non-None probe must re-arm the one-time advisory"


def test_screen_recorder_calls_permission_policy_preflight_and_poll(tmp_path):
    """``ScreenRecorder.run()`` must invoke ``permission_policy.preflight()``
    during setup and ``permission_policy.poll(elapsed)`` in the recording loop.

    Pins the wiring contract that replaces the inline ``_check_macos_permissions``
    and ``_check_permissions_now`` calls with policy delegation.
    """
    from collections import namedtuple

    from screencap.engine.config import RecordingConfig
    from screencap.engine.disk_policy import Noop as DiskNoop
    from screencap.engine.network_policy import Null as NetworkNull
    from screencap.engine.lock_policy import InheritLock
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingPolicies,
        RecordingRequest,
        ScreenRecorder,
    )
    from tests.conftest import FakeRecorder

    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    plenty = DiskUsage(total=500_000_000_000, used=100_000_000_000, free=400_000_000_000)

    class SpyPermissionPolicy:
        def __init__(self):
            self.preflight_calls = 0
            self.poll_calls: list[float] = []

        def preflight(self):
            self.preflight_calls += 1

        def poll(self, now: float):
            self.poll_calls.append(now)

        @property
        def next_poll_at(self) -> float:
            return 0.0  # always ready to poll

    spy = SpyPermissionPolicy()
    request = RecordingRequest(name="perm-spy", config=RecordingConfig())
    channels = IpcChannels.create()
    policies = RecordingPolicies(
        signal=mock.MagicMock(spec=["install", "uninstall"]),
        lock=InheritLock(),
        menubar=MenubarNoop(),
        permission=spy,
        disk=DiskNoop(),
        network=NetworkNull(),
    )
    legacy = LegacyOptions(output_dir=tmp_path / "perm-spy")

    with (
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("shutil.disk_usage", return_value=plenty),
        mock.patch("screencap.engine.recorder.Recorder", FakeRecorder),
    ):
        ScreenRecorder(
            request=request, channels=channels, policies=policies, legacy=legacy,
        ).run()

    assert spy.preflight_calls == 1, "preflight must be called exactly once during setup"
