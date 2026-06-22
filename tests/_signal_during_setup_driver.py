"""Subprocess driver for ``test_signal_during_setup.py`` (SCR-39).

Runs ``ScreenRecorder.run()`` against a ``SlowFakeRecorder`` whose
``__enter__`` writes a ready marker and then sleeps for a few seconds —
opening the exact "handlers installed but recorder=None" window the
parent test races a SIGINT into.

The driver mocks the same external boundaries the parity / integration
tests do (TCC permission probe, orphan probe, disk usage, real
``mss``/``mp`` machinery), so nothing here actually touches the
hardware.
"""

from __future__ import annotations

import argparse
import time
from collections import namedtuple
from pathlib import Path
from unittest import mock


_DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
_PLENTY_OF_DISK = _DiskUsage(total=500e9, used=100e9, free=400e9)


def _build_slow_recorder(ready_marker: Path, hold_seconds: float):
    """Build a ``FakeRecorder`` subclass that pauses inside ``__enter__``.

    Defined inline (not imported from ``tests.conftest``) so the driver
    is self-contained and can be invoked as a plain script from a
    subprocess without pytest's fixture machinery on its path.
    """
    from screencap.engine.db import create_db, crud

    class SlowFakeRecorder:
        def __init__(self, capture_dir_str, **_kwargs):
            self.capture_dir = str(Path(capture_dir_str).resolve())
            self.is_recording = False
            self.health_warning = ""
            self.child_crashes = []
            self._chunk_process_q = None
            self._audio_ack_q = None

        def __enter__(self):
            ready_marker.write_text("ready")
            time.sleep(hold_seconds)
            self._create_db()
            return self

        def __exit__(self, *args):
            return False

        def wait_for_ready(self, timeout=30):
            return True

        def stop(self):
            self.is_recording = False

        def finalize_pipeline(self):
            return None

        def _create_db(self):
            db_path = Path(self.capture_dir) / "recording.db"
            engine, Session = create_db(str(db_path))
            session = Session()
            t = 1000.0
            crud.insert_recording(session, {
                "timestamp": t,
                "platform": "darwin",
                "monitor_width": 1920,
                "monitor_height": 1080,
                "pixel_ratio": 2.0,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5.0,
            })
            session.close()
            engine.dispose()

    return SlowFakeRecorder


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ready-marker", required=True, type=Path)
    parser.add_argument("--setup-hold", required=True, type=float)
    args = parser.parse_args()

    SlowFakeRecorder = _build_slow_recorder(args.ready_marker, args.setup_hold)

    from screencap.engine.lock_policy import InheritLock
    from screencap.recorder import start_recording

    with (
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.engine.recorder.Recorder", SlowFakeRecorder),
    ):
        start_recording("sigint-setup-test", output_dir=args.output_dir, _lock_policy=InheritLock())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
