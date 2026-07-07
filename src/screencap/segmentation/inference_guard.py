"""Inference single-flight + RAM-headroom guard for the local model (U8, KTD11).

A ~2 GB downloaded-model worker must not run concurrently with another, nor on a
machine already short on memory, or it thrashes an 8 GB Mac during capture and
breaks "run all day." Two guards, both **skip-not-queue** (fail open to the
heuristic rather than deferring the memory spike):

- :func:`inference_slot` — a **process-wide** single-flight (non-blocking). Two
  overlapping terminal stages (independent daemon worker threads) never both
  hold it; the loser skips segmentation. (Same-process is the primary case; a
  cross-process flock is a future refinement.)
- :func:`has_ram_headroom` — refuses the spawn when available memory is below a
  model-size-plus-margin floor. Uses the already-sampled ``psutil`` when present;
  when it is absent the check is a no-op (never blocks on a missing optional dep).
"""

from __future__ import annotations

import contextlib
import logging
import threading

log = logging.getLogger(__name__)

# Process-wide: at most one local-model worker in flight across the daemon.
_INFERENCE_LOCK = threading.Lock()

# Fallback available-RAM floor when the model size is unknown (~2 GB model needs
# headroom for its working set + leave room for capture).
_DEFAULT_RAM_FLOOR_BYTES = 3 * 1024**3

# Extra headroom over the model's on-disk size (working set + capture room).
_RAM_MARGIN_BYTES = 1 * 1024**3


@contextlib.contextmanager
def inference_slot():
    """Yield ``True`` if this caller acquired the single-flight slot, else ``False``.

    Non-blocking: a second concurrent caller gets ``False`` and should skip
    segmentation fail-open (never queue — that just defers the memory spike).
    """
    acquired = _INFERENCE_LOCK.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _INFERENCE_LOCK.release()


def has_ram_headroom(model_size_bytes: int | None = None) -> bool:
    """True when available memory clears the model-size-plus-margin floor.

    Fail-open on a missing ``psutil`` (returns ``True``) — the guard is a
    best-effort safety margin, not a correctness gate.
    """
    try:
        import psutil
    except ImportError:
        return True
    try:
        available = psutil.virtual_memory().available
    except Exception:  # pragma: no cover - defensive
        return True
    floor = (
        model_size_bytes + _RAM_MARGIN_BYTES
        if model_size_bytes
        else _DEFAULT_RAM_FLOOR_BYTES
    )
    if available < floor:
        log.info(
            "Skipping local-model inference: %d MB available < %d MB floor",
            available // (1024**2), floor // (1024**2),
        )
        return False
    return True
