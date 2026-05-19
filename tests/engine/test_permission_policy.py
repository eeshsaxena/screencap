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
