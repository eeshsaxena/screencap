"""``DiskPolicy`` seam (SCR-42, slice 5 of SCR-31).

Promotes the disk-space preflight and adaptive periodic poll from inline
code in ``_run_screen_recorder`` to a pluggable policy. ``MonitorAndStop``
is the seam default; ``Noop`` is what session workers and tests use to skip
disk-space monitoring.

The ``bind(capture_dir)`` method must be called after ``capture_dir`` is
known (inside ``_run_screen_recorder``) but before ``preflight()``. This
two-step pattern exists because ``RecordingPolicies`` is built before
``_run_screen_recorder`` computes ``capture_dir`` from the request + legacy
options.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

_DISK_CHECK_INTERVAL = 30  # seconds between normal-band disk checks


class DiskSpaceCritical(Exception):
    """Raised by ``DiskPolicy.poll()`` when free space drops below ``stop_mb``.

    The caller (``_run_screen_recorder``) catches this to set
    ``_stop_reason = "disk_full"``, record ``_disk_free_at_stop`` for the
    live-display message, and call ``recorder.stop()``. The full
    ``DiskFullError`` (with menubar handles) is still assembled and raised
    by ``_run_screen_recorder`` after the recording loop exits.
    """

    def __init__(self, free_mb: float) -> None:
        self.free_mb = free_mb


class DiskPolicy(Protocol):
    """Disk-space preflight + adaptive polling. ``MonitorAndStop`` | ``Noop``."""

    def bind(self, capture_dir: Path) -> None: ...

    def preflight(self) -> None: ...

    def poll(self, now: float) -> None: ...

    @property
    def next_poll_at(self) -> float: ...

    @property
    def warning(self) -> str: ...


class MonitorAndStop:
    """Seam-default disk policy: check free space at startup, poll adaptively.

    ``preflight`` raises ``DiskTooLowAtStart`` if free space is below
    ``warn_mb`` before the recording begins. ``poll`` checks every 30 s
    (dropping to 5 s when in the warning band) and raises
    ``DiskSpaceCritical`` when space drops below ``stop_mb``.

    Both thresholds are read from config (``SCREENCAP_DISK_WARN_MB`` /
    ``SCREENCAP_DISK_STOP_MB``) at ``preflight()`` time so env-var overrides
    are respected without requiring reconstruction.
    """

    def __init__(self) -> None:
        self._capture_dir: Path | None = None
        self._warn_mb = 0
        self._stop_mb = 0
        self._next_poll_at: float = 0.0
        self._disk_check_interval: float = _DISK_CHECK_INTERVAL
        self._warning: str = ""

    def bind(self, capture_dir: Path) -> None:
        self._capture_dir = capture_dir

    def preflight(self) -> None:
        from screencap.config import get_disk_stop_mb, get_disk_warn_mb
        from screencap.engine.screen_recorder import DiskTooLowAtStart

        self._warn_mb = get_disk_warn_mb()
        self._stop_mb = get_disk_stop_mb()

        if self._warn_mb > 0 and self._stop_mb > 0 and self._stop_mb >= self._warn_mb:
            raise DiskTooLowAtStart(
                f"disk_stop_mb ({self._stop_mb}) must be less than "
                f"disk_warn_mb ({self._warn_mb}). Adjust your config or env vars."
            )

        if self._capture_dir is None:
            return

        check_path = (
            self._capture_dir.parent
            if not self._capture_dir.exists()
            else self._capture_dir
        )
        try:
            free = shutil.disk_usage(check_path).free
            warn_bytes = self._warn_mb * 1_048_576
            if self._warn_mb > 0 and free < warn_bytes:
                raise DiskTooLowAtStart(
                    f"Only {free / 1e9:.1f} GB free on {check_path}. "
                    f"Need at least {warn_bytes / 1e9:.1f} GB to start recording.\n"
                    f"  Set SCREENCAP_DISK_WARN_MB to lower the threshold, or =0 to disable."
                )
        except DiskTooLowAtStart:
            raise
        except FileNotFoundError:
            raise DiskTooLowAtStart(f"Recording path not found: {check_path}")
        except OSError:
            pass

    def poll(self, now: float) -> None:
        if now < self._next_poll_at or self._capture_dir is None:
            return

        try:
            free = shutil.disk_usage(self._capture_dir).free
            free_mb = free / 1_048_576

            if self._stop_mb > 0 and free_mb < self._stop_mb:
                self._disk_check_interval = _DISK_CHECK_INTERVAL
                self._next_poll_at = now + self._disk_check_interval
                self._warning = f"Disk critically low: {free_mb:.0f} MB free. Stopping."
                raise DiskSpaceCritical(free_mb)
            elif self._warn_mb > 0 and free_mb < self._warn_mb:
                self._disk_check_interval = 5
                self._warning = f"Low disk: {free / 1e9:.1f} GB free"
            else:
                self._disk_check_interval = _DISK_CHECK_INTERVAL
                self._warning = ""
        except DiskSpaceCritical:
            raise
        except OSError:
            self._warning = ""

        self._next_poll_at = now + self._disk_check_interval

    @property
    def next_poll_at(self) -> float:
        return self._next_poll_at

    @property
    def warning(self) -> str:
        return self._warning


class Noop:
    """Session-worker / test disk policy: all hooks are safe no-ops.

    Tests and session workers set this policy so they never call
    ``shutil.disk_usage`` or read disk-threshold config during recording.
    """

    def bind(self, capture_dir: Path) -> None:
        pass

    def preflight(self) -> None:
        pass

    def poll(self, now: float) -> None:
        pass

    @property
    def next_poll_at(self) -> float:
        return float("inf")

    @property
    def warning(self) -> str:
        return ""
