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
from screencap.engine.recorder import (
    _network_event_reader_loop,
    _teardown_network_capture,
)


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


class TestReaderDrainBoundedByProxyLiveness:
    """Regression for PR #157 review P1.

    The 250ms empty-poll drain was too short: ``_teardown_network_capture``
    restores system proxy FIRST (osascript admin auth, can take seconds)
    and only then calls ``proxy_proc.terminate()``. The addon's
    ``done()`` hook emits ``network.tunneled`` events when SIGTERM
    finally reaches mitmproxy, well after a fixed-duration drain would
    have exited. Tying the drain exit condition to proxy liveness keeps
    the reader alive long enough to receive those final events.
    """

    def test_drain_waits_for_proxy_to_exit_before_giving_up(self):
        """Reader must NOT exit while proxy_proc.is_alive() returns True.

        Simulates a slow teardown: terminate fires immediately but the
        proxy reports alive for 300ms (longer than the legacy 250ms
        budget) before going dead and pushing one final event. The
        event MUST be forwarded.
        """
        out_q: _queue_mod.Queue = _queue_mod.Queue()
        write_q = MagicMock()
        terminate_event = threading.Event()
        started_event = threading.Event()

        # Fake proxy_proc whose is_alive() flips False after a delay.
        proxy_alive = threading.Event()
        proxy_alive.set()  # initially alive

        class FakeProxyProc:
            def is_alive(self_inner):
                return proxy_alive.is_set()

        proxy_proc = FakeProxyProc()

        thread = threading.Thread(
            target=_network_event_reader_loop,
            args=(out_q, write_q, terminate_event, started_event, proxy_proc),
            daemon=True,
        )
        thread.start()
        assert started_event.wait(timeout=2.0)

        # Terminate immediately; queue stays empty for 400ms (exceeds
        # the legacy 250ms drain budget).
        terminate_event.set()
        time.sleep(0.4)
        # Reader MUST still be alive — it's waiting on proxy.
        assert thread.is_alive(), (
            "reader exited before proxy died; drain is still time-bounded "
            "and would lose events emitted by addon done() during teardown"
        )

        # Now simulate the late event from addon done() and proxy exit.
        late_event = _make_request_event("late-from-done.example.com")
        out_q.put(late_event)
        proxy_alive.clear()  # proxy_proc now reports dead

        thread.join(timeout=2.0)
        assert not thread.is_alive(), "reader did not exit after proxy died"

        forwarded = [call.args[0] for call in write_q.put.call_args_list]
        forwarded_hosts = [getattr(e, "host", None) for e in forwarded]
        assert "late-from-done.example.com" in forwarded_hosts, (
            "late event from addon done() lost — drain exited too early"
        )

    def test_proxy_proc_none_keeps_legacy_timeout_only_path(self):
        """Test path / legacy callers pass proxy_proc=None and rely on
        the 250ms empty-poll budget. Verify that contract still holds.
        """
        out_q: _queue_mod.Queue = _queue_mod.Queue()
        write_q = MagicMock()
        terminate_event = threading.Event()
        started_event = threading.Event()

        thread = threading.Thread(
            target=_network_event_reader_loop,
            args=(out_q, write_q, terminate_event, started_event, None),
            daemon=True,
        )
        thread.start()
        assert started_event.wait(timeout=2.0)

        terminate_event.set()
        thread.join(timeout=1.0)
        assert not thread.is_alive(), (
            "proxy_proc=None path must keep the legacy timeout-only "
            "exit behavior for backward compatibility with tests / "
            "callers that don't supply a proxy_proc"
        )


class TestTeardownOrdering:
    """Regression for PR #157 round-4 P1.

    The network writer was using the global ``terminate_processing``
    event, which fires BEFORE ``_teardown_network_capture`` runs.
    The writer exited before the proxy was even SIGTERMed, so the
    addon's ``done()``-emitted final events (notably
    ``network.tunneled``) ended up in ``network_write_q`` with no
    consumer. The fix introduced a dedicated ``writer_terminate_event``
    that ``_teardown_network_capture`` sets ONLY AFTER the reader has
    joined.

    These tests lock the ordering invariants so a future refactor
    that flips the steps re-introduces the silent-drop window
    immediately rather than mysteriously losing events at runtime.
    """

    def _build_state(self, ordering: list[str]):
        """Construct a state dict whose mocks record their call order
        into ``ordering``. Returns the dict for direct use with
        ``_teardown_network_capture``.
        """
        from pathlib import Path
        from unittest.mock import MagicMock

        # Each mock appends a tag when its method fires. The teardown
        # must produce a sequence that satisfies the documented
        # invariant: proxy.terminate -> proxy.join -> reader.join ->
        # writer_terminate.set -> writer.join.
        proxy_proc = MagicMock()
        proxy_proc.is_alive.side_effect = [True, False]  # alive, then dead
        proxy_proc.terminate.side_effect = lambda: ordering.append("proxy.terminate")
        proxy_proc.join.side_effect = lambda timeout=None: ordering.append("proxy.join")
        proxy_proc.kill = MagicMock()

        reader_thread = MagicMock()
        reader_thread.is_alive.return_value = True
        reader_thread.join.side_effect = lambda timeout=None: ordering.append("reader.join")

        writer_terminate_event = MagicMock()
        writer_terminate_event.set.side_effect = lambda: ordering.append(
            "writer_terminate.set",
        )

        writer_proc = MagicMock()
        writer_proc.is_alive.return_value = False  # exits cleanly after event set
        writer_proc.join.side_effect = lambda timeout=None: ordering.append(
            "writer.join",
        )

        # The teardown also touches system-proxy state and writes a
        # marker file. Stub those to no-ops; this test cares about
        # the writer/reader/proxy ordering only.
        recording_dir = MagicMock(spec=Path)
        recording_dir.__truediv__ = lambda self_inner, other: MagicMock(
            write_text=MagicMock(),
        )

        return {
            "snapshot": {},
            "durable_snapshot_path": MagicMock(spec=Path),
            "recording_dir": recording_dir,
            "services_at_start": [],
            "proxy_proc": proxy_proc,
            "reader_thread": reader_thread,
            "writer_proc": writer_proc,
            "writer_terminate_event": writer_terminate_event,
        }

    def test_writer_terminate_set_after_reader_joins(self):
        """``writer_terminate_event.set()`` MUST happen AFTER
        ``reader_thread.join()`` returns. Setting it earlier (e.g.
        before reader exits) would let the writer exit while the
        reader is still feeding it events from the addon's done()
        hook — silently dropping final events.
        """
        from unittest.mock import patch

        ordering: list[str] = []
        state = self._build_state(ordering)

        # _teardown_network_capture also calls into the system-proxy
        # restore path; stub the imports it triggers.
        with (
            patch("screencap.network.system_proxy.restore_all"),
            patch("screencap.network.system_proxy.list_active_services",
                  return_value=[]),
            patch("screencap.network.system_proxy.services_changed_marker"),
            patch("screencap.network.lifecycle.delete_sentinel"),
            patch("screencap.network.lifecycle.delete_network_child_handoff"),
        ):
            _teardown_network_capture(state)

        # Critical invariants: writer_terminate.set comes AFTER
        # reader.join AND AFTER proxy.terminate.
        reader_idx = ordering.index("reader.join")
        writer_set_idx = ordering.index("writer_terminate.set")
        proxy_terminate_idx = ordering.index("proxy.terminate")
        writer_join_idx = ordering.index("writer.join")

        assert writer_set_idx > reader_idx, (
            f"writer_terminate.set fired BEFORE reader.join. Order was: "
            f"{ordering}. The writer would exit while the reader is "
            f"still forwarding done()-emitted events, dropping them."
        )
        assert writer_set_idx > proxy_terminate_idx, (
            f"writer_terminate.set fired BEFORE proxy.terminate. Order: "
            f"{ordering}. The writer must outlive proxy termination so "
            f"final events from addon done() can land."
        )
        assert writer_join_idx > writer_set_idx, (
            f"writer.join called BEFORE writer_terminate.set. Order: "
            f"{ordering}. The writer would never see the terminate "
            f"signal and the join would time out / force-terminate."
        )

    def test_writer_terminate_event_optional_for_legacy_state(self):
        """If a state dict has no ``writer_terminate_event`` key (e.g.
        partially-built during cleanup-on-setup-failure), teardown
        must not crash.
        """
        from unittest.mock import patch

        ordering: list[str] = []
        state = self._build_state(ordering)
        del state["writer_terminate_event"]
        del state["writer_proc"]

        with (
            patch("screencap.network.system_proxy.restore_all"),
            patch("screencap.network.system_proxy.list_active_services",
                  return_value=[]),
            patch("screencap.network.system_proxy.services_changed_marker"),
            patch("screencap.network.lifecycle.delete_sentinel"),
            patch("screencap.network.lifecycle.delete_network_child_handoff"),
        ):
            # Should not raise.
            _teardown_network_capture(state)
