"""Tier-3 integration test for SCR-44: chunk_processor + scrub_worker
flush serialization via the shared ``RecordingCollaborators._flush_lock``.

Acceptance criterion from the ticket:

    chunk_processor and scrub_worker still serialize correctly
    (no flush-counter race)

The shared lock is the load-bearing primitive that prevents the two
consumers from racing on the engine's ``flush_ack_counter``: without it,
a concurrent reset by one consumer can zero out the other's in-flight
ack count mid-poll, causing the second flush to time out with ``acked==0``
and the SELECT to read stale rows.

This test drives both consumers concurrently through the same code path
they use in production (``wait_for_writer_flush`` with the helper's
``_flush_lock``) against a single fake "engine writer" thread, and pins
the invariant that **each** consumer's flush completes with a positive
ack count.
"""

from __future__ import annotations

import logging
import multiprocessing as _mp
import threading
import time

from screencap._flush import wait_for_writer_flush


def _fake_engine_writer(
    *,
    flush_requested: _mp.Event,
    flush_ack_counter: _mp.Value,
    stop_event: threading.Event,
) -> None:
    """Increment the ack counter on every poll while ``flush_requested`` is set.

    Mirrors a fast engine writer: each tick where the event is set, it
    has "committed and acked" — production writers do this against
    different in-flight rows on each ack, but for the lock-serialization
    test the only invariant we care about is that *both* consumers'
    ``wait_for_writer_flush`` calls observe a positive counter. With the
    shared lock, each consumer's handshake gets exclusive access to the
    counter for its full poll window; without it, one consumer's reset
    can clobber the other's already-observed acks mid-poll.
    """
    while not stop_event.is_set():
        if flush_requested.is_set():
            with flush_ack_counter.get_lock():
                flush_ack_counter.value += 1
        time.sleep(0.001)


def test_chunk_and_scrub_flush_calls_serialize_via_shared_lock(tmp_path):
    """Two concurrent ``wait_for_writer_flush`` calls both ack with the
    shared lock — neither one's counter reset eats the other's progress.

    Without ``flush_lock``, the second caller's reset to 0 races against
    the first caller's polling and can yield a zero ack count even
    though the writer responded to both flush requests.
    """
    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )

    helper = RecordingCollaborators(
        request=RecordingRequest(name="t", config=RecordingConfig()),
        legacy=LegacyOptions(),
        channels=IpcChannels.create(),
    )
    flush_lock = helper._flush_lock
    flush_requested = _mp.Event()
    flush_ack_counter = _mp.Value("i", 0)
    stop_event = threading.Event()
    writer_done = threading.Event()

    writer = threading.Thread(
        target=_fake_engine_writer,
        kwargs={
            "flush_requested": flush_requested,
            "flush_ack_counter": flush_ack_counter,
            "stop_event": writer_done,
        },
        daemon=True,
        name="fake_engine_writer",
    )
    writer.start()

    results: dict[str, int] = {}

    def _flush_call(caller: str) -> None:
        results[caller] = wait_for_writer_flush(
            flush_requested=flush_requested,
            flush_ack_counter=flush_ack_counter,
            flush_lock=flush_lock,
            stop_event=stop_event,
            logger=logging.getLogger("test"),
            caller=caller,
        )

    t_chunk = threading.Thread(target=_flush_call, args=("chunk_processor",))
    t_scrub = threading.Thread(target=_flush_call, args=("scrub_worker",))
    t_chunk.start()
    t_scrub.start()
    t_chunk.join(timeout=15)
    t_scrub.join(timeout=15)

    writer_done.set()
    writer.join(timeout=2)

    assert not t_chunk.is_alive() and not t_scrub.is_alive(), (
        "flush threads must complete; the lock should be released between calls."
    )
    assert results["chunk_processor"] >= 1, (
        f"chunk_processor flush returned {results.get('chunk_processor')!r}; "
        "shared lock should ensure the writer ack count is preserved."
    )
    assert results["scrub_worker"] >= 1, (
        f"scrub_worker flush returned {results.get('scrub_worker')!r}; "
        "shared lock should ensure the writer ack count is preserved."
    )
