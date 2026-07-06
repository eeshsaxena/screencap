"""``DiskPolicy`` seam.

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

import logging
import shutil
from pathlib import Path
from typing import Protocol

_logger = logging.getLogger(__name__)

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
        self._container_active: bool | None = None

    def bind(self, capture_dir: Path) -> None:
        self._capture_dir = capture_dir
        self._container_active = None  # re-resolve for the new capture dir

    def _free_space_check_path(self) -> Path | None:
        """Path whose *host* volume free space governs the guard (KTD-13).

        With the encrypted container active, ``capture_dir`` sits inside a
        mounted volume that reports free space against its *declared* size
        (host capacity), so a naive ``disk_usage(capture_dir)`` would never
        fire until the host is already full — the 2018 sparse-bundle
        silent-write failure class. The host volume backing the bundle is the
        bundle's parent (``~/.screencap``), which sits above the mountpoint
        and always exists. With the container off, behavior is unchanged: the
        capture dir's own volume is the host (parent fallback when it does not
        exist yet).

        The ``container_active`` decision is resolved once per recording (the
        capture dir does not move mid-recording), so ``poll`` does not re-read
        ``config.toml`` every cadence, and a mid-recording flag flip cannot
        switch which volume the guard measures out from under an in-flight
        capture.
        """
        if self._capture_dir is None:
            return None
        if self._container_active is None:
            from screencap import config

            self._container_active = config.container_active()
        if self._container_active:
            from screencap import container

            return container.default_bundle_path().parent
        return self._capture_dir if self._capture_dir.exists() else self._capture_dir.parent

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

        check_path = self._free_space_check_path()
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
        except OSError as exc:
            # An unreadable disk is the same threat as a full one: we can't
            # prove the recording will fit. Fail-closed.
            _logger.warning("disk preflight failed: %r", exc, exc_info=True)
            raise DiskTooLowAtStart(
                f"Cannot read disk free space on {check_path}: {exc!r}",
            )

    def poll(self, now: float) -> None:
        if now < self._next_poll_at or self._capture_dir is None:
            return

        try:
            free = shutil.disk_usage(self._free_space_check_path()).free
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
        except OSError as exc:
            _logger.warning("disk poll failed: %r", exc, exc_info=True)
            self._warning = "disk check unavailable"

        self._next_poll_at = now + self._disk_check_interval

    @property
    def next_poll_at(self) -> float:
        return self._next_poll_at

    @property
    def warning(self) -> str:
        return self._warning


class Noop:
    """Disk policy that skips all space monitoring.

    Used by session workers (to avoid redundant ``shutil.disk_usage`` polls
    on top of what the controller already runs) and by tests that need to
    bypass disk preflight entirely.
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
