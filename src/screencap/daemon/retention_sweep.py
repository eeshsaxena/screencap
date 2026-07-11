"""Periodic screenshot-retention sweep (search guardrails U5 / R2).

``retention.evict_recording`` only fires during a recording and at convergence, so
nothing revisits a finished recording 30 days later to apply the screenshot bound —
without a periodic sweep the bound is dead code. This module is that sweep: a
Supervisor-style background task (mirroring ``daemon/backfill_job.py``) that walks
``~/.screencap/recordings/`` on an interval and applies the age/size bound to every
recording's ``screenshots/`` dir.

It is deliberately **not auth-gated** — retention must run for signed-out,
local-only users — and imports no auth surface. It is strictly fail-open: a bad
recording dir is logged and skipped, never aborting the sweep or the daemon. When
no bound is configured (the pre-U8-flip default), a tick is a cheap no-op.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Sweep cadence. A 30-day bound tolerates a coarse interval, and the first tick is
# delayed a full interval so a short-lived auto-spawned daemon (headless
# ``screencap status``) never pays a disk scan + deletes just to answer a status
# call — only the persistent LaunchAgent daemon lives long enough to sweep.
_DEFAULT_INTERVAL_SECS = 6 * 3600.0


@dataclass
class SweepReport:
    """Outcome of one :func:`sweep_once` pass."""

    recordings_scanned: int = 0
    stills_evicted: int = 0
    bytes_freed: int = 0
    index_rows_purged: int = 0
    errors: list[str] = field(default_factory=list)


def sweep_once(recordings_dir: Path | str | None = None, *, now: float | None = None) -> SweepReport:
    """Apply the screenshot retention bound to every recording under
    ``recordings_dir`` (defaults to the configured recordings dir).

    Synchronous + self-contained so it is unit-testable without the daemon. Reads
    the bound once; an unbounded config short-circuits to a no-op. Fail-open per
    recording."""
    from screencap import config, retention

    report = SweepReport()
    days = config.get_screenshot_retention_days()
    size_cap_mb = config.get_screenshot_size_cap_mb()
    if days <= 0 and size_cap_mb <= 0:
        return report  # unbounded → nothing to sweep (today's behavior)

    root = Path(recordings_dir) if recordings_dir is not None else config.get_recordings_dir()
    if not root.is_dir():
        return report
    try:
        rec_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return report

    # Open ONE read-side content index for the whole sweep (the "don't race the
    # indexer" high-water lookups) instead of one open per recording.
    from contextlib import nullcontext

    from screencap.content_index import ContentIndex, default_index_path

    idx_path = default_index_path()
    store_ctx = ContentIndex(idx_path) if idx_path.exists() else nullcontext(None)
    with store_ctx as index_store:
        for rec_dir in rec_dirs:
            try:
                sub = retention.evict_screenshots(
                    rec_dir,
                    now=now,
                    days=days,
                    size_cap_mb=size_cap_mb,
                    last_indexed_ts=retention._last_indexed_ts(
                        rec_dir.name, store=index_store
                    ),
                )
                report.recordings_scanned += 1
                report.stills_evicted += len(sub.evicted)
                report.bytes_freed += sub.bytes_freed
                report.index_rows_purged += sub.index_rows_purged
            except Exception as exc:  # noqa: BLE001 — one bad dir must not abort the sweep
                logger.warning(
                    "retention sweep: %s failed (%s)", rec_dir.name, type(exc).__name__
                )
                report.errors.append(rec_dir.name)
    if report.stills_evicted:
        logger.info(
            "retention sweep: evicted %d stills (%d bytes) across %d recordings",
            report.stills_evicted,
            report.bytes_freed,
            report.recordings_scanned,
        )
    return report


class RetentionSweep:
    """Background periodic driver for :func:`sweep_once`, owned by the daemon lifespan."""

    def __init__(self, *, interval_secs: float = _DEFAULT_INTERVAL_SECS) -> None:
        self._interval = interval_secs
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the periodic loop (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        from screencap import config

        while True:
            # Delay-first: never sweep on the startup tick (see interval note).
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            try:
                await asyncio.to_thread(sweep_once, config.get_recordings_dir())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a tick failure must not kill the loop
                logger.warning("retention sweep tick failed", exc_info=True)

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Cancel the loop and await it, bounded — never hang daemon teardown."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            logger.debug("awaiting retention sweep during shutdown raised", exc_info=True)
