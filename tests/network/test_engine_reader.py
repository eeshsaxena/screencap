"""Tests for ``_network_event_reader_loop`` in engine/recorder.py.

The reader thread drains the proxy's ``out_q`` into ``network_write_q``,
routes ``NetworkPinFailureEvent`` to console (control-only), and
synthesizes ``NetworkDropBurstEvent`` on backpressure. The drain phase
after ``terminate_event`` is set must process trailing events instead
of exiting immediately, otherwise events the addon already pushed but
the reader hasn't consumed are silently lost on every clean stop.
"""

from __future__ import annotations

import queue as _queue_mod
import threading
import time
from unittest.mock import MagicMock

from screencap.engine.events import NetworkRequestEvent
from screencap.engine.recorder import _network_event_reader_loop


def _make_request_event(host: str = "example.com") -> NetworkRequestEvent:
    return NetworkRequestEvent(
        timestamp=time.time(),
        timestamp_ns=time.time_ns(),
        flow_id="flow-1",
        method="GET",
        url=f"https://{host}/",
        host=host,
    )


class TestReaderDrainOnTerminate:
    """Regression for PR #156 review P2-1.

    The reader's main-phase loop exits when ``terminate_event`` is set,
    even if events are still in ``out_q``. Since the engine teardown
    sets ``terminate_processing`` BEFORE terminating the proxy
    mp.Process, the addon may push final events between those two
    steps that the reader would silently drop without a drain phase.
    """

    def test_drains_events_pushed_after_terminate(self):
        """Events put on out_q AFTER terminate_event is set must be
        forwarded to network_write_q, not silently dropped."""
        out_q: _queue_mod.Queue = _queue_mod.Queue()
        write_q = MagicMock()
        write_q.put = MagicMock()
        terminate_event = threading.Event()
        started_event = threading.Event()

        # Start the reader thread.
        thread = threading.Thread(
            target=_network_event_reader_loop,
            args=(out_q, write_q, terminate_event, started_event),
            daemon=True,
        )
        thread.start()
        assert started_event.wait(timeout=2.0)

        # Push event during main phase.
        evt_before = _make_request_event("during.example.com")
        out_q.put(evt_before)
        time.sleep(0.1)  # let main-phase loop pick it up

        # Set terminate, then push more events to simulate the proxy
        # flushing during shutdown (addon's done() emitting drop_burst).
        terminate_event.set()
        evt_after_1 = _make_request_event("trailing-1.example.com")
        evt_after_2 = _make_request_event("trailing-2.example.com")
        out_q.put(evt_after_1)
        out_q.put(evt_after_2)

        # Wait for drain phase to exit (5 empty polls × 50ms ≈ 250ms).
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "reader thread did not exit"

        # All three events should have been forwarded — the trailing
        # ones via the drain phase. The bug was that they got dropped.
        forwarded = [call.args[0] for call in write_q.put.call_args_list]
        forwarded_hosts = [getattr(e, "host", None) for e in forwarded]
        assert "during.example.com" in forwarded_hosts
        assert "trailing-1.example.com" in forwarded_hosts, (
            "trailing event lost — drain phase failed to process post-terminate events"
        )
        assert "trailing-2.example.com" in forwarded_hosts, (
            "trailing event lost — drain phase failed to process post-terminate events"
        )

    def test_drain_phase_exits_when_queue_stays_empty(self):
        """The drain phase must terminate within a bounded window when
        the queue stays empty — empty-poll counting prevents the
        thread from looping forever."""
        out_q: _queue_mod.Queue = _queue_mod.Queue()
        write_q = MagicMock()
        terminate_event = threading.Event()
        started_event = threading.Event()

        thread = threading.Thread(
            target=_network_event_reader_loop,
            args=(out_q, write_q, terminate_event, started_event),
            daemon=True,
        )
        thread.start()
        assert started_event.wait(timeout=2.0)

        # Set terminate immediately; queue stays empty.
        terminate_event.set()
        # Drain phase: 5 empty polls × 50ms ≈ 250ms upper bound.
        thread.join(timeout=1.0)
        assert not thread.is_alive(), (
            "reader thread did not exit within drain budget; the empty-poll "
            "exit condition may be broken"
        )
