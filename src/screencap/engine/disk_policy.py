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
import os
import shutil
from pathlib import Path
from typing import Protocol

_logger = logging.getLogger(__name__)

_DISK_CHECK_INTERVAL = 30  # seconds between normal-band disk checks

# SCR-258 U4 (KTD-13): the daemon sets this on the engine subprocess when the
# encrypted container is active. The capture dir then lives inside the mounted
# volume, whose ``shutil.disk_usage`` reports the DECLARED (virtual, sparse) size
# — so a genuinely full host would never trip the guard. When set (and it names an
# existing path) the free-space checks below evaluate the HOST volume backing the
# bundle instead. Unset on a plaintext install → today's capture-dir behavior.
_DISK_HOST_PATH_ENV = "SCREENCAP_DISK_HOST_PATH"


def _free_space_path(capture_dir: Path | None) -> Path | None:
    """The path whose volume free space bounds recording (KTD-13).

    The host-volume override when the daemon set it (container active), else the
    capture dir (or its parent when the capture dir does not exist yet). Read at
    check time so an env override is honored without reconstructing the policy.
    """
    host = os.environ.get(_DISK_HOST_PATH_ENV)
    if host:
        host_path = Path(host)
        if host_path.exists():
            return host_path
        # A configured-but-missing host path is a fail-closed signal, not a reason
        # to silently fall back to the (virtual-size) mounted volume.
        return host_path
    if capture_dir is None:
        return None
    return capture_dir if capture_dir.exists() else capture_dir.parent


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

        # KTD-13: watch the HOST volume backing the bundle when the container is
        # active (the daemon sets the env), else the capture dir / its parent.
        check_path = _free_space_path(self._capture_dir)
        if check_path is None:
            return
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

        # KTD-13: same host-volume override as preflight — a mounted encrypted
        # volume's virtual free space must not mask a full host.
        poll_path = _free_space_path(self._capture_dir)
        if poll_path is None:
            return
        try:
            free = shutil.disk_usage(poll_path).free
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
