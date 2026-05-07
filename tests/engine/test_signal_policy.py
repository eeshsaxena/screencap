"""Tests for the ``SignalPolicy`` seam (SCR-39, slice 3 of SCR-31).

Promotes the inline three-tap SIGINT escalation + SIGTERM handler in
``_run_screen_recorder`` to a pluggable policy. Two implementations:

* ``ThreeTapSigint``  — installs SIGINT (3-tap escalation) and SIGTERM
  handlers; CLI standalone path uses this.
* ``NoopSignalPolicy`` — does nothing; session workers use this because
  the ``SessionController`` parent owns Ctrl+C.

These are *unit* tests for install/uninstall behaviour. The Tier-3
integration test that proves SIGINT-during-setup wins lives in
``tests/test_signal_during_setup.py``.
"""

from __future__ import annotations

import signal
from contextlib import contextmanager


@contextmanager
def _saved_handlers():
    """Snapshot SIGINT + SIGTERM handlers and restore on exit.

    Tests in this module mutate process-global signal handlers; the
    pytest runner (and any later test in the file) must see the
    pre-test state. ``signal.signal`` returns the previous handler.
    """
    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)


def test_three_tap_sigint_install_registers_provided_handlers():
    """``ThreeTapSigint.install`` registers the SIGINT/SIGTERM handlers it
    is given. The policy is install/uninstall plumbing — handler bodies
    stay in ``_run_screen_recorder`` (they close over nonlocal state)."""
    from screencap.engine.screen_recorder import ThreeTapSigint

    def sigint_handler(sig, frame):  # noqa: ARG001
        pass

    def sigterm_handler(sig, frame):  # noqa: ARG001
        pass

    policy = ThreeTapSigint()
    with _saved_handlers():
        policy.install(sigint_handler=sigint_handler, sigterm_handler=sigterm_handler)
        assert signal.getsignal(signal.SIGINT) is sigint_handler
        assert signal.getsignal(signal.SIGTERM) is sigterm_handler


def test_three_tap_sigint_uninstall_restores_defaults():
    """``ThreeTapSigint.uninstall`` restores SIGINT to ``default_int_handler``
    and SIGTERM to ``SIG_DFL`` — the same restoration ``_run_screen_recorder``
    performs in its ``finally`` block today."""
    from screencap.engine.screen_recorder import ThreeTapSigint

    policy = ThreeTapSigint()
    with _saved_handlers():
        policy.install(
            sigint_handler=lambda sig, frame: None,
            sigterm_handler=lambda sig, frame: None,
        )
        policy.uninstall()
        assert signal.getsignal(signal.SIGINT) == signal.default_int_handler
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


def test_noop_signal_policy_install_does_not_touch_handlers():
    """``NoopSignalPolicy.install`` leaves SIGINT/SIGTERM unchanged.

    Session workers set the policy to ``Noop`` because the parent
    ``SessionController`` owns Ctrl+C. The previous shape was a
    ``_skip_sigint_handler=True`` private kwarg on ``start_recording``;
    SCR-39 retires that flag in favour of this policy.
    """
    from screencap.engine.screen_recorder import NoopSignalPolicy

    sentinel_int = signal.getsignal(signal.SIGINT)
    sentinel_term = signal.getsignal(signal.SIGTERM)

    policy = NoopSignalPolicy()
    policy.install(
        sigint_handler=lambda sig, frame: None,
        sigterm_handler=lambda sig, frame: None,
    )
    try:
        assert signal.getsignal(signal.SIGINT) is sentinel_int
        assert signal.getsignal(signal.SIGTERM) is sentinel_term
    finally:
        policy.uninstall()  # also a no-op — must leave state untouched


def test_noop_signal_policy_uninstall_does_not_touch_handlers():
    """``NoopSignalPolicy.uninstall`` is the symmetric no-op of ``install``."""
    from screencap.engine.screen_recorder import NoopSignalPolicy

    sentinel_int = signal.getsignal(signal.SIGINT)
    sentinel_term = signal.getsignal(signal.SIGTERM)

    NoopSignalPolicy().uninstall()

    assert signal.getsignal(signal.SIGINT) is sentinel_int
    assert signal.getsignal(signal.SIGTERM) is sentinel_term


# ---------------------------------------------------------------------------
# Wiring: ScreenRecorder.run() calls signal_policy.install / uninstall.
# ---------------------------------------------------------------------------


def test_screen_recorder_invokes_signal_policy_install_then_uninstall(tmp_path):
    """``ScreenRecorder.run()`` must invoke ``policies.signal.install(...)``
    with the inner SIGINT + SIGTERM handlers, and ``uninstall()`` in
    ``finally``. This is the wiring contract that retires the
    ``_skip_sigint_handler`` private kwarg: the policy decides whether
    to register; the body always calls ``install`` / ``uninstall``."""
    from collections import namedtuple
    from unittest import mock

    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        NoopSignalPolicy,
        RecordingPolicies,
        RecordingRequest,
        ScreenRecorder,
    )
    from tests.conftest import FakeRecorder

    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    plenty = DiskUsage(total=500e9, used=100e9, free=400e9)

    class SpyPolicy:
        def __init__(self):
            self.install_calls: list[tuple] = []
            self.uninstall_calls: int = 0

        def install(self, *, sigint_handler, sigterm_handler):
            self.install_calls.append((sigint_handler, sigterm_handler))

        def uninstall(self):
            self.uninstall_calls += 1

    spy = SpyPolicy()
    request = RecordingRequest(name="signal-spy", config=RecordingConfig())
    channels = IpcChannels.create()
    policies = RecordingPolicies(
        signal=spy,
        lock=NoopSignalPolicy(),  # placeholder; lock policy not yet pluggable
        menubar=object(),
        permission=object(),
        disk=object(),
        network=object(),
    )
    legacy = LegacyOptions(output_dir=tmp_path / "spy")
    rec = ScreenRecorder(
        request=request, channels=channels, policies=policies, legacy=legacy,
    )

    with (
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("screencap.recorder.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.recorder.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=plenty),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("screencap.pidfile.write_pidfile"),
        mock.patch("screencap.pidfile.delete_pidfile"),
        mock.patch("screencap.engine.recorder.Recorder", FakeRecorder),
    ):
        rec.run()

    assert len(spy.install_calls) == 1, (
        f"expected exactly 1 install call, got {len(spy.install_calls)}"
    )
    sigint_handler, sigterm_handler = spy.install_calls[0]
    assert callable(sigint_handler), "install was passed a non-callable sigint_handler"
    assert callable(sigterm_handler), "install was passed a non-callable sigterm_handler"
    assert spy.uninstall_calls == 1, (
        f"uninstall must run exactly once in finally, got {spy.uninstall_calls}"
    )
