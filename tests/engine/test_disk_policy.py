"""Tests for the ``DiskPolicy`` seam (SCR-42, slice 5 of SCR-31).

Promotes the disk-space preflight and adaptive periodic poll from inline
code in ``_run_screen_recorder`` to a pluggable policy.

* ``MonitorAndStop`` — seam default: checks free space at startup
  (``DiskTooLowAtStart`` on miss), then polls every 30 s (5 s when in the
  warning band) and raises ``DiskSpaceCritical`` when free space drops below
  ``stop_mb``. Requires ``bind(capture_dir)`` before ``preflight()``.
* ``Noop``            — session workers and tests: all hooks are safe no-ops.

These tests pin the seam-level behavioural contract. The wiring into
``_run_screen_recorder`` is exercised by ``test_screen_recorder_parity``.
"""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from unittest import mock

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])

_PLENTY = DiskUsage(total=500_000_000_000, used=100_000_000_000, free=400_000_000_000)
_LOW = DiskUsage(total=500_000_000_000, used=498_000_000_000, free=2_000_000_000)  # 2 GB
_CRITICAL = DiskUsage(total=500_000_000_000, used=499_700_000_000, free=300 * 1_048_576)  # 300 MiB


# ---------------------------------------------------------------------------
# Cycle 1 — smoke test
# ---------------------------------------------------------------------------


def test_disk_policy_module_exposes_protocol_and_impls():
    """Smoke test: the module exists with the three documented names.

    Both ``MonitorAndStop`` and ``Noop`` must be constructible with no args.
    ``DiskSpaceCritical`` is the sentinel exception ``poll()`` raises when
    free space drops below ``stop_mb``.
    """
    from screencap.engine.disk_policy import (  # noqa: F401
        DiskPolicy,
        DiskSpaceCritical,
        MonitorAndStop,
        Noop,
    )

    MonitorAndStop()
    Noop()


# ---------------------------------------------------------------------------
# Cycle 2 — Noop lifecycle is safe
# ---------------------------------------------------------------------------


def test_noop_bind_preflight_poll_are_safe_noops(tmp_path):
    """``Noop`` must not call ``shutil.disk_usage`` or read config."""
    from screencap.engine.disk_policy import Noop

    policy = Noop()
    with mock.patch("shutil.disk_usage") as usage:
        policy.bind(tmp_path)
        policy.preflight()
        policy.poll(0.0)

    usage.assert_not_called()


def test_noop_next_poll_at_never_triggers():
    """``Noop.next_poll_at`` returns infinity so the main loop never polls."""
    from screencap.engine.disk_policy import Noop

    assert Noop().next_poll_at == float("inf")


def test_noop_warning_is_empty_string():
    """``Noop.warning`` is always an empty string — no UI warning injected."""
    from screencap.engine.disk_policy import Noop

    assert Noop().warning == ""


# ---------------------------------------------------------------------------
# Cycle 3 — MonitorAndStop.preflight: low-disk raises DiskTooLowAtStart
# ---------------------------------------------------------------------------


def test_monitor_and_stop_preflight_raises_disk_too_low_at_start(tmp_path):
    """``MonitorAndStop.preflight`` raises ``DiskTooLowAtStart`` when free
    space is below the warn threshold.

    Matches the wrapper-era behaviour: recording is rejected before
    ``capture_dir.mkdir()`` so no partial directory is left behind.
    """
    import pytest

    from screencap.engine.disk_policy import MonitorAndStop
    from screencap.engine.screen_recorder import DiskTooLowAtStart

    # 300 MB free, warn threshold 2000 MB → too low
    policy = MonitorAndStop()
    policy.bind(tmp_path / "rec")

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_CRITICAL),
    ):
        with pytest.raises(DiskTooLowAtStart):
            policy.preflight()


def test_monitor_and_stop_preflight_passes_when_plenty_of_disk(tmp_path):
    """``MonitorAndStop.preflight`` returns normally when disk is ample."""
    from screencap.engine.disk_policy import MonitorAndStop

    policy = MonitorAndStop()
    policy.bind(tmp_path / "rec")

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY),
    ):
        policy.preflight()  # must not raise


def test_monitor_and_stop_preflight_raises_on_invalid_thresholds(tmp_path):
    """``MonitorAndStop.preflight`` raises ``DiskTooLowAtStart`` when
    ``stop_mb >= warn_mb`` — misconfigured thresholds would make the stop
    trigger fire before the warning is ever shown."""
    import pytest

    from screencap.engine.disk_policy import MonitorAndStop
    from screencap.engine.screen_recorder import DiskTooLowAtStart

    policy = MonitorAndStop()
    policy.bind(tmp_path / "rec")

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=500),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY),
    ):
        with pytest.raises(DiskTooLowAtStart):
            policy.preflight()


def test_monitor_and_stop_preflight_checks_parent_dir_when_capture_dir_absent(tmp_path):
    """``MonitorAndStop.preflight`` checks ``capture_dir.parent`` when
    ``capture_dir`` does not yet exist.

    The preflight runs before ``capture_dir.mkdir()``, so the dir itself
    may not exist — the check must fall back to the parent to get the
    correct filesystem usage.
    """
    from screencap.engine.disk_policy import MonitorAndStop

    capture_dir = tmp_path / "new-rec"  # does not exist yet
    policy = MonitorAndStop()
    policy.bind(capture_dir)

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY) as usage,
    ):
        policy.preflight()

    # Must have checked the parent directory (tmp_path), not the missing dir
    usage.assert_called_once_with(tmp_path)


# ---------------------------------------------------------------------------
# Cycle 4 — MonitorAndStop.poll: disk full raises DiskSpaceCritical
# ---------------------------------------------------------------------------


def test_monitor_and_stop_poll_raises_disk_space_critical_when_below_stop_mb(tmp_path):
    """``MonitorAndStop.poll`` raises ``DiskSpaceCritical`` when free space
    drops below ``stop_mb``.

    The caller (``_run_screen_recorder``) catches this to set
    ``_stop_reason = "disk_full"``, record ``_disk_free_at_stop``,
    and call ``recorder.stop()``.
    """
    import pytest

    from screencap.engine.disk_policy import DiskSpaceCritical, MonitorAndStop

    policy = MonitorAndStop()
    policy.bind(tmp_path)

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY),
    ):
        policy.preflight()

    # Now simulate disk dropping below stop_mb during recording
    with (
        mock.patch("shutil.disk_usage", return_value=_CRITICAL),
    ):
        with pytest.raises(DiskSpaceCritical) as excinfo:
            policy.poll(0.0)

    # free_mb carried for the live-display message
    assert excinfo.value.free_mb == pytest.approx(300.0, abs=1.0)


def test_monitor_and_stop_poll_does_not_raise_when_disk_is_fine(tmp_path):
    """``MonitorAndStop.poll`` returns normally when disk is ample."""
    from screencap.engine.disk_policy import MonitorAndStop

    policy = MonitorAndStop()
    policy.bind(tmp_path)

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY),
    ):
        policy.preflight()
        policy.poll(0.0)  # must not raise


# ---------------------------------------------------------------------------
# Cycle 5 — MonitorAndStop.poll: adaptive cadence + warning property
# ---------------------------------------------------------------------------


def test_monitor_and_stop_poll_warning_set_in_warn_band_cleared_when_fine(tmp_path):
    """``MonitorAndStop.warning`` reflects disk state: non-empty in the
    warn band, cleared when space returns to normal.

    The live-display loop reads ``disk_policy.warning`` to populate the
    ``_build_live_display`` warning line.
    """
    from screencap.engine.disk_policy import MonitorAndStop

    policy = MonitorAndStop()
    policy.bind(tmp_path)

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY),
    ):
        policy.preflight()

    # In the warn band (low, but above stop_mb)
    with mock.patch("shutil.disk_usage", return_value=_LOW):
        policy.poll(0.0)
    assert policy.warning != ""

    # Back to plenty → warning cleared
    with mock.patch("shutil.disk_usage", return_value=_PLENTY):
        policy.poll(35.0)  # after interval
    assert policy.warning == ""


def test_monitor_and_stop_poll_interval_drops_to_5s_in_warn_band(tmp_path):
    """``MonitorAndStop`` polls every 5 s (instead of 30 s) while disk
    is in the warn band, to catch a fast-filling disk sooner.
    """
    from screencap.engine.disk_policy import MonitorAndStop

    policy = MonitorAndStop()
    policy.bind(tmp_path)

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY),
    ):
        policy.preflight()

    # First poll (t=0): disk in warn band → interval drops to 5 s
    with mock.patch("shutil.disk_usage", return_value=_LOW):
        policy.poll(0.0)

    assert policy.next_poll_at == 5.0

    # After warn band clears, interval resets to 30 s
    with mock.patch("shutil.disk_usage", return_value=_PLENTY):
        policy.poll(5.0)  # triggered by the 5 s interval

    assert policy.next_poll_at == 35.0


# ---------------------------------------------------------------------------
# Cycle 9 — Wiring: ScreenRecorder.run() calls disk_policy.bind + preflight
# ---------------------------------------------------------------------------


def test_screen_recorder_calls_disk_policy_bind_and_preflight(tmp_path):
    """``ScreenRecorder.run()`` must call ``disk_policy.bind(capture_dir)``
    then ``disk_policy.preflight()`` during setup.

    Pins the wiring contract: bind delivers the resolved capture_dir so
    the policy can choose the right filesystem to check; preflight replaces
    the inline disk-space thresholds + ``shutil.disk_usage`` calls.
    """
    from unittest import mock

    from screencap.engine.config import RecordingConfig
    from screencap.engine.lock_policy import InheritLock
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.network_policy import Null as NetworkNull
    from screencap.engine.permission_policy import Noop as PermNoop
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingPolicies,
        RecordingRequest,
        ScreenRecorder,
    )
    from tests.conftest import FakeRecorder

    class SpyDiskPolicy:
        def __init__(self):
            self.bind_calls: list = []
            self.preflight_calls = 0

        def bind(self, capture_dir):
            self.bind_calls.append(capture_dir)

        def preflight(self):
            self.preflight_calls += 1

        def poll(self, now):
            pass

        @property
        def next_poll_at(self):
            return float("inf")

        @property
        def warning(self):
            return ""

    spy = SpyDiskPolicy()
    capture_dir = tmp_path / "disk-spy"
    request = RecordingRequest(name="disk-spy", config=RecordingConfig())
    channels = IpcChannels.create()
    policies = RecordingPolicies(
        signal=mock.MagicMock(spec=["install", "uninstall"]),
        lock=InheritLock(),
        menubar=MenubarNoop(),
        permission=PermNoop(),
        disk=spy,
        network=NetworkNull(),
    )
    legacy = LegacyOptions(output_dir=capture_dir)

    with (
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("screencap.engine.recorder.Recorder", FakeRecorder),
    ):
        ScreenRecorder(
            request=request, channels=channels, policies=policies, legacy=legacy,
        ).run()

    assert len(spy.bind_calls) == 1, "bind must be called exactly once"
    assert spy.bind_calls[0] == capture_dir, "bind must receive the resolved capture_dir"
    assert spy.preflight_calls == 1, "preflight must be called exactly once after bind"


def test_monitor_and_stop_poll_skips_before_interval(tmp_path):
    """``MonitorAndStop.poll`` does not call ``shutil.disk_usage`` before the
    polling interval has elapsed."""
    from screencap.engine.disk_policy import MonitorAndStop

    policy = MonitorAndStop()
    policy.bind(tmp_path)

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY),
    ):
        policy.preflight()
        policy.poll(0.0)  # first poll: triggers check

    # t=10 is within the 30 s window — must skip
    with mock.patch("shutil.disk_usage") as usage:
        policy.poll(10.0)

    usage.assert_not_called()


def test_preflight_oserror_fails_closed(tmp_path):
    """An ``OSError`` from ``shutil.disk_usage`` raises ``DiskTooLowAtStart``.

    A disk we can't measure is the same threat as a full one — fail-closed.
    """
    import pytest

    from screencap.engine.disk_policy import MonitorAndStop
    from screencap.engine.screen_recorder import DiskTooLowAtStart

    policy = MonitorAndStop()
    policy.bind(tmp_path)

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", side_effect=PermissionError("EACCES")),
        pytest.raises(DiskTooLowAtStart),
    ):
        policy.preflight()


def test_poll_oserror_surfaces_warning(tmp_path):
    """An ``OSError`` from ``shutil.disk_usage`` mid-recording sets ``warning``.

    Used to silently set ``_warning = ""`` (fail-open + invisible).
    """
    from screencap.engine.disk_policy import MonitorAndStop

    policy = MonitorAndStop()
    policy.bind(tmp_path)

    with (
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY),
    ):
        policy.preflight()

    with mock.patch("shutil.disk_usage", side_effect=OSError("EIO")):
        policy.poll(100.0)

    assert policy.warning, "warning must surface to the live display"
