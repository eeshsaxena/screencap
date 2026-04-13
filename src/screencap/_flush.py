"""Shared writer-flush handshake used by chunk_processor and scrub_worker.

Both consumers need to force the engine writer processes to commit their
buffered DB rows before reading from the live SQLite. They share a single
``flush_requested`` event and ``flush_ack_counter``; a ``threading.Lock``
serializes the two callers so one's counter reset doesn't zero out the
other's in-flight ack count.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

_FLUSH_DEADLINE_SECONDS = 5.0
_FLUSH_STABLE_SECONDS = 1.0
_POLL_INTERVAL_SECONDS = 0.1


def wait_for_writer_flush(
    *,
    flush_requested: Any,
    flush_ack_counter: Any,
    flush_lock: threading.Lock | None,
    stop_event: threading.Event,
    logger: logging.Logger,
    caller: str,
) -> int:
    """Trigger a writer-process flush and wait for ack count to stabilize.

    Returns the final ack count. Returns 0 immediately if the IPC
    primitives are not configured (e.g. engine started without them).

    ``flush_lock`` is held only across the handshake (counter reset →
    event set → poll → event clear). Callers MUST release whatever
    downstream lock they hold before invoking this so both consumers
    can fairly contend for the flush lock.
    """
    if flush_requested is None or flush_ack_counter is None:
        return 0

    if flush_lock is not None:
        flush_lock.acquire()
    try:
        with flush_ack_counter.get_lock():
            flush_ack_counter.value = 0

        flush_requested.set()

        deadline = time.time() + _FLUSH_DEADLINE_SECONDS
        prev_acked = 0
        stable_since = time.time()
        while time.time() < deadline and not stop_event.is_set():
            time.sleep(_POLL_INTERVAL_SECONDS)
            with flush_ack_counter.get_lock():
                acked = flush_ack_counter.value
            if acked > prev_acked:
                prev_acked = acked
                stable_since = time.time()
            elif acked > 0 and time.time() - stable_since > _FLUSH_STABLE_SECONDS:
                break

        flush_requested.clear()

        with flush_ack_counter.get_lock():
            acked = flush_ack_counter.value
        if acked == 0:
            logger.debug("%s: flush skipped (no writers acked)", caller)
        else:
            logger.info("%s: flushed %d writer(s)", caller, acked)
        return acked
    finally:
        if flush_lock is not None:
            flush_lock.release()
