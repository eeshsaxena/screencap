"""SIGTERM-during-startup elapsed recovery (SCR-71).

When SIGTERM arrives during engine startup, ``recorder.stop()`` sets
``_terminate_processing`` *before* the live status loop is entered, so
``is_recording`` is already ``False`` on the first check and the loop body
— the only place ``elapsed`` is assigned — never runs. ``elapsed`` keeps
its initial ``0.0`` even though the engine captured tens of seconds of
data, and that bogus value flows into ``.recording_ready.elapsed``.

The fix surfaces the engine's own recording duration (carried on the
``record.stopped`` status message, exposed as ``Recorder.recording_duration``)
and uses it as the ``elapsed`` fallback whenever the live loop was skipped.

This file pins both halves of the fix:

1. ``_run_screen_recorder`` returns a non-zero ``elapsed`` (≈ the engine's
   recording duration) when the live loop never runs — driven through the
   ``FakeRecorder`` seam, whose ``is_recording=False`` reproduces exactly
   the post-SIGTERM-before-ready state.
2. ``Recorder.wait_for_ready`` returns promptly once a stop has been
   requested, instead of blocking the full timeout on ``_ready_event``.
"""

from __future__ import annotations

import time
from collections import namedtuple
from unittest import mock

from tests.conftest import FakeRecorder

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
_PLENTY_OF_DISK = DiskUsage(total=500e9, used=100e9, free=400e9)

_ENGINE_DURATION = 12.5


class _LoopSkippedRecorder(FakeRecorder):
    """``FakeRecorder`` that reports a real engine recording duration.

    ``is_recording`` stays ``False`` (inherited), so the live loop is
    skipped and ``elapsed`` is left at ``0.0`` — the SIGTERM-before-ready
    state. ``recording_duration`` stands in for the value the real engine
    carries on its ``record.stopped`` message.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recording_duration = _ENGINE_DURATION


def _mocks(recorder_cls):
    return [
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.engine.recorder.Recorder", recorder_cls),
    ]


def test_elapsed_recovered_when_live_loop_skipped(tmp_path):
    """A recording whose live loop never ran reports the engine duration.

    Mirrors SIGTERM-arriving-before-``record.started``: the live loop's
    guard is false on the first check, so ``elapsed`` is never assigned by
    the loop and must fall back to the engine's recording duration.
    """
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.screen_recorder import NoopSignalPolicy
    from screencap.recorder import start_recording

    out_dir = tmp_path / "rec"

    mocks = _mocks(_LoopSkippedRecorder)
    for m in mocks:
        m.start()
    try:
        _capture_dir, elapsed, _mb_proc, _mb_state = start_recording(
            "scr71", output_dir=out_dir,
            _menubar_policy=MenubarNoop(),
            _signal_policy=NoopSignalPolicy(),
        )
    finally:
        for m in mocks:
            m.stop()

    assert elapsed == _ENGINE_DURATION, (
        f"live loop was skipped; elapsed should fall back to the engine "
        f"duration {_ENGINE_DURATION}, got {elapsed}"
    )


def test_elapsed_stays_zero_without_engine_duration(tmp_path):
    """No engine duration available → elapsed stays 0.0 (no fabrication).

    Guards the fallback against inventing a duration when the engine never
    reported one (``recording_duration`` is ``None``); the plain
    ``FakeRecorder`` has no value to recover.
    """
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.screen_recorder import NoopSignalPolicy
    from screencap.recorder import start_recording

    out_dir = tmp_path / "rec"

    mocks = _mocks(FakeRecorder)
    for m in mocks:
        m.start()
    try:
        _capture_dir, elapsed, _mb_proc, _mb_state = start_recording(
            "scr71-none", output_dir=out_dir,
            _menubar_policy=MenubarNoop(),
            _signal_policy=NoopSignalPolicy(),
        )
    finally:
        for m in mocks:
            m.stop()

    assert elapsed == 0.0


def test_wait_for_ready_returns_when_stop_requested(tmp_path):
    """``wait_for_ready`` must not block the full timeout once stopping.

    SIGTERM during startup sets ``_terminate_processing`` via
    ``recorder.stop()``; the main thread is parked in
    ``wait_for_ready(timeout=30)``. Before the fix that call ignored the
    stop signal and blocked on ``_ready_event`` until ``record.started``
    (or the 30 s timeout). It must now return promptly.
    """
    from screencap.engine.recorder import Recorder

    rec = Recorder(str(tmp_path / "cap"))
    # Simulate ``recorder.stop()`` firing during startup, before the engine
    # ever emits ``record.started`` (so ``_ready_event`` stays unset).
    rec._terminate_processing.set()

    started = time.monotonic()
    ready = rec.wait_for_ready(timeout=30)
    elapsed = time.monotonic() - started

    assert ready is False
    assert elapsed < 2.0, f"wait_for_ready blocked {elapsed:.1f}s despite stop signal"
