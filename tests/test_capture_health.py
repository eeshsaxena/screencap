"""Tests for SCR-76 mid-recording capture-health detection.

The daemon recording path runs the engine with ``PermNoop``, so the standalone
CLI's subprocess-based TCC watcher never runs there — and that subprocess probe
is itself broken in the frozen daemon binary (SCR-69). This suite covers the
capture-side mechanism that replaces it: per-reader attempt-vs-output counters
folded into the engine's 1s supervisor loop, attributed in-process and surfaced
as ``permission_lost`` (TCC cause) or the advisory ``capture_unhealthy`` (non-TCC).

Following the repo's testing posture (see ``test_permission_revocation_engine``
and ``test_health_monitoring``), assertions target observed counter / verdict
state and the emitted stderr event stream — never internal call counts — so the
tests stay decoupled from implementation. The units exercised:
  U1 counters · U2 listener heartbeat · U3 ``_capture_health_step`` ·
  U4 ``_probe_tcc_denied`` · U5 ``_emit_capture_health_event``.

Origin plan: docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
"""

from __future__ import annotations

import ast
import inspect
import json
import queue
import sys
import textwrap
import threading
import types
from io import StringIO

import pytest

from screencap.engine import recorder


def _referenced_names(fn) -> set[str]:
    """Names a function's CODE references (imports, attributes, identifiers),
    excluding string/docstring content — so structural guards check what the
    code does, not what its prose mentions."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


@pytest.fixture(autouse=True)
def _reset_health_state():
    """Module-level health state is reset per recording in production; reset it
    per test here so direct calls to the readers/helpers don't leak across tests."""
    recorder._capture_health_counts.clear()
    recorder._listener_handles.clear()
    yield
    recorder._capture_health_counts.clear()
    recorder._listener_handles.clear()


def _capture_stderr(callable_):
    buf = StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        callable_()
    finally:
        sys.stderr = saved
    return [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# U1 — raw per-reader attempt/output counters (driving the REAL reader code)
# ---------------------------------------------------------------------------


class _FakeFrame:
    """Stand-in for a PIL Image — only needs to be non-None and have .size."""
    size = (100, 100)


class TestCounters:
    def _run_screen_reader(self, monkeypatch, frames):
        """Drive read_screen_events with a take_screenshot stub yielding `frames`
        (one per iteration); terminate once the list is exhausted."""
        from screencap.engine import utils

        seq = iter(frames)
        term = threading.Event()

        def fake_shot():
            try:
                return next(seq)
            finally:
                # set terminate when the next call would exhaust the sequence
                if seq.__length_hint__() == 0:
                    term.set()

        monkeypatch.setattr(utils, "take_screenshot", fake_shot)
        monkeypatch.setattr(recorder.config, "SCREEN_CAPTURE_FPS", 0)  # no throttle sleep
        rec = types.SimpleNamespace(timestamp=0.0)
        recorder.read_screen_events(
            queue.Queue(maxsize=100), term, rec, threading.Event(),
        )

    def test_screen_attempt_and_output_advance_on_good_frames(self, monkeypatch):
        self._run_screen_reader(monkeypatch, [_FakeFrame() for _ in range(5)])
        assert recorder._capture_health_counts["screen.attempt"] == 5
        assert recorder._capture_health_counts["screen.output"] == 5

    def test_screen_none_frames_increment_attempt_not_output(self, monkeypatch):
        # A None frame is the only robust "broken screen reader" content signal.
        self._run_screen_reader(monkeypatch, [None for _ in range(5)])
        assert recorder._capture_health_counts["screen.attempt"] == 5
        assert recorder._capture_health_counts.get("screen.output", 0) == 0

    def _run_window_reader(self, monkeypatch, results):
        from screencap.engine import window

        seq = iter(results)
        term = threading.Event()

        def fake_window():
            try:
                return next(seq)
            finally:
                if seq.__length_hint__() == 0:
                    term.set()

        monkeypatch.setattr(window, "get_active_window_data", fake_window)
        monkeypatch.setattr(recorder.time, "sleep", lambda *_a, **_k: None)  # no poll delay
        rec = types.SimpleNamespace(timestamp=0.0)
        recorder.read_window_events(
            queue.Queue(maxsize=100), term, rec, threading.Event(),
        )

    def test_window_unchanged_truthy_still_counts_output(self, monkeypatch):
        # A user sitting on one unchanged window is healthy (output counted
        # BEFORE the change gate), even though nothing is enqueued after #1.
        same = {"title": "Doc", "window_id": 7, "app_bundle_id": "com.x"}
        self._run_window_reader(monkeypatch, [dict(same) for _ in range(4)])
        assert recorder._capture_health_counts["window.attempt"] == 4
        assert recorder._capture_health_counts["window.output"] == 4

    def test_window_falsy_poll_increments_attempt_not_output(self, monkeypatch):
        self._run_window_reader(monkeypatch, [{} for _ in range(4)])
        assert recorder._capture_health_counts["window.attempt"] == 4
        assert recorder._capture_health_counts.get("window.output", 0) == 0

    def test_action_output_counts_when_event_produced(self):
        recorder.trigger_action_event(
            queue.Queue(maxsize=10), {"name": "click", "mouse_x": 1, "mouse_y": 2},
        )
        assert recorder._capture_health_counts["action.output"] == 1

    def test_action_output_counts_even_on_queue_full(self):
        full_q = queue.Queue(maxsize=1)
        full_q.put_nowait("occupied")
        # Non-move event → put(timeout=0.05) → queue.Full → dropped, but the
        # reader DID produce an event, so output still counts.
        recorder.trigger_action_event(full_q, {"name": "click", "mouse_x": 1, "mouse_y": 2})
        assert recorder._capture_health_counts["action.output"] == 1

    def test_counters_are_plain_ints_no_multiprocessing(self):
        recorder._health_incr("screen.attempt")
        assert isinstance(recorder._capture_health_counts["screen.attempt"], int)


# ---------------------------------------------------------------------------
# U2 — action listener-liveness heartbeat
# ---------------------------------------------------------------------------


class _Listener:
    def __init__(self, running):
        self.running = running


class TestActionListenerAlive:
    def test_no_handles_is_alive_failopen(self):
        # Start race: nothing published yet → alive.
        assert recorder._action_listener_alive() is True

    def test_running_listener_is_alive(self):
        recorder._listener_handles["keyboard"] = _Listener(True)
        recorder._listener_handles["mouse"] = _Listener(True)
        assert recorder._action_listener_alive() is True

    def test_all_listeners_not_running_is_unhealthy(self):
        recorder._listener_handles["keyboard"] = _Listener(False)
        recorder._listener_handles["mouse"] = _Listener(False)
        assert recorder._action_listener_alive() is False

    def test_any_running_listener_keeps_alive(self):
        recorder._listener_handles["keyboard"] = _Listener(False)
        recorder._listener_handles["mouse"] = _Listener(True)
        assert recorder._action_listener_alive() is True

    def test_none_handle_is_skipped_and_failopen(self):
        recorder._listener_handles["keyboard"] = None
        assert recorder._action_listener_alive() is True

    def test_sampling_error_is_failopen(self):
        class _Boom:
            @property
            def running(self):
                raise RuntimeError("bridge hiccup")

        recorder._listener_handles["keyboard"] = _Boom()
        assert recorder._action_listener_alive() is True


# ---------------------------------------------------------------------------
# U3 — pure per-tick verdict/debounce step (the primary behavioral assertion)
# ---------------------------------------------------------------------------


_ALIVE = {"screen": True, "window": True, "action": True}


def _drive(ticks, *, action_alive=True, debounce=3, alive=None):
    """Feed a sequence of (attempt_delta, output_delta) for the SCREEN reader
    through _capture_health_step and return the per-tick edge lists."""
    alive = alive or _ALIVE
    runs, emitted = {}, {}
    prev = {"screen.attempt": 0, "screen.output": 0,
            "window.attempt": 0, "window.output": 0}
    cur = dict(prev)
    out = []
    for a, o in ticks:
        cur["screen.attempt"] += a
        cur["screen.output"] += o
        # keep window healthy so only the screen dimension drives the verdict
        cur["window.attempt"] += 2
        cur["window.output"] += 2
        out.append(recorder._capture_health_step(
            prev, cur, alive, action_alive, debounce, runs, emitted,
        ))
        prev = dict(cur)
    return out


def test_attempting_without_output_fires_after_debounce_once():
    # R11: a reader attempting but producing no useful output → unhealth fires.
    edges = _drive([(20, 0)] * 5, debounce=3)
    assert edges[2] == ["screen"]
    assert all(e == [] for i, e in enumerate(edges) if i != 2)


def test_idle_reader_with_proportional_output_never_fires():
    # attempts produce output → healthy (this is "nothing to capture" idle).
    edges = _drive([(20, 20)] * 5)
    assert all(e == [] for e in edges)


def test_single_transient_tick_does_not_fire():
    edges = _drive([(20, 20), (20, 0), (20, 20), (20, 20)], debounce=3)
    assert all(e == [] for e in edges)


def test_recover_then_rebreak_re_emits():
    edges = _drive(
        [(20, 0), (20, 0), (20, 0), (20, 20), (20, 0), (20, 0), (20, 0)],
        debounce=3,
    )
    assert edges[2] == ["screen"]
    assert edges[6] == ["screen"]


def test_dead_reader_is_left_to_child_died_path():
    # not alive → never evaluated by the health check (no double-emit).
    edges = _drive([(20, 0)] * 5, alive={"screen": False, "window": True, "action": True})
    assert all(e == [] for e in edges)


def test_action_heartbeat_alive_with_no_output_is_idle_not_unhealthy():
    # AE3: idle action reader (heartbeat alive, zero action output) → no fire.
    runs, emitted = {}, {}
    edges = []
    for _ in range(5):
        edges += recorder._capture_health_step(
            {}, {}, _ALIVE, True, 3, runs, emitted,
        )
    assert edges == []


def test_action_listener_dead_fires_as_action_after_debounce():
    # AE2: listener dead while thread alive → unhealthy.
    runs, emitted = {}, {}
    fired = []
    for _ in range(4):
        fired.append(recorder._capture_health_step({}, {}, _ALIVE, False, 3, runs, emitted))
    assert fired[2] == ["action"]


def test_window_broken_poll_fires():
    runs, emitted = {}, {}
    prev = {"window.attempt": 0, "window.output": 0,
            "screen.attempt": 0, "screen.output": 0}
    cur = dict(prev)
    edges = []
    for _ in range(3):
        cur["window.attempt"] += 2          # polling
        cur["screen.attempt"] += 20         # screen healthy
        cur["screen.output"] += 20
        edges = recorder._capture_health_step(prev, cur, _ALIVE, True, 3, runs, emitted)
        prev = dict(cur)
    assert edges == ["window"]


# ---------------------------------------------------------------------------
# U4 — in-process Quartz/Accessibility labeller (fail-open, no subprocess)
# ---------------------------------------------------------------------------


def _install_fake_tcc(monkeypatch, *, screen=True, input_mon=True, ax=True,
                      quartz_import_error=False, screen_raises=False):
    monkeypatch.setattr(sys, "platform", "darwin")
    if quartz_import_error:
        monkeypatch.setitem(sys.modules, "Quartz", None)  # None → ImportError on import
        return

    def _screen():
        if screen_raises:
            raise RuntimeError("transient Quartz hiccup")
        return screen

    fake_q = types.SimpleNamespace(
        CGPreflightScreenCaptureAccess=_screen,
        CGPreflightListenEventAccess=lambda: input_mon,
    )
    fake_as = types.SimpleNamespace(
        AXIsProcessTrustedWithOptions=lambda _opts: ax,
        kAXTrustedCheckOptionPrompt="prompt",
    )
    monkeypatch.setitem(sys.modules, "Quartz", fake_q)
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake_as)


class TestProbeTccDenied:
    def test_screen_denied_labels_screen_recording(self, monkeypatch):
        _install_fake_tcc(monkeypatch, screen=False)
        assert recorder._probe_tcc_denied("screen") == "screen_recording"

    def test_input_denied_labels_input_monitoring(self, monkeypatch):
        _install_fake_tcc(monkeypatch, input_mon=False)
        assert recorder._probe_tcc_denied("action") == "input_monitoring"

    def test_accessibility_denied_labels_accessibility(self, monkeypatch):
        _install_fake_tcc(monkeypatch, ax=False)
        assert recorder._probe_tcc_denied("window") == "accessibility"

    def test_all_granted_is_inconclusive(self, monkeypatch):
        _install_fake_tcc(monkeypatch)  # all True
        assert recorder._probe_tcc_denied("screen") is None

    def test_reader_bias_picks_most_likely_when_multiple_denied(self, monkeypatch):
        # screen AND accessibility denied; a window-reader symptom should
        # attribute to accessibility first (its most likely cause).
        _install_fake_tcc(monkeypatch, screen=False, ax=False)
        assert recorder._probe_tcc_denied("window") == "accessibility"
        assert recorder._probe_tcc_denied("screen") == "screen_recording"

    def test_quartz_import_failure_is_inconclusive(self, monkeypatch):
        _install_fake_tcc(monkeypatch, quartz_import_error=True)
        assert recorder._probe_tcc_denied("screen") is None

    def test_call_raises_is_failopen_inconclusive(self, monkeypatch):
        # AE4: a labeller call raising must not propagate; returns inconclusive.
        _install_fake_tcc(monkeypatch, screen_raises=True, input_mon=True, ax=True)
        assert recorder._probe_tcc_denied("screen") is None

    def test_non_darwin_is_inconclusive(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert recorder._probe_tcc_denied("screen") is None

    def test_labeller_uses_no_subprocess(self):
        # The whole point vs the SCR-69 broken probe: NO subprocess invocation,
        # no _check_permission_fresh. Check actual code references (AST), not
        # prose — the docstring legitimately mentions the osascript subprocess
        # it deliberately avoids.
        names = _referenced_names(recorder._probe_tcc_denied)
        assert "subprocess" not in names
        assert "_check_permission_fresh" not in names
        assert "executable" not in names  # would appear in sys.executable


# ---------------------------------------------------------------------------
# U5 — event emission mapping (permission_lost reuse + advisory capture_unhealthy)
# ---------------------------------------------------------------------------


class TestEmitCaptureHealthEvent:
    def test_tcc_label_emits_permission_lost(self):
        captured = []
        t = recorder._emit_capture_health_event(
            "screen", "screen_recording", 12.0, lambda et, **f: captured.append((et, f)),
        )
        assert t == "permission_lost"
        assert captured == [("permission_lost", {"permission": "screen_recording", "elapsed": 12.0})]

    def test_inconclusive_reader_emits_capture_unhealthy_reader_stalled(self):
        captured = []
        t = recorder._emit_capture_health_event(
            "screen", None, 5.0, lambda et, **f: captured.append((et, f)),
        )
        assert t == "capture_unhealthy"
        assert captured[0][1] == {"reason": "reader_stalled", "reader": "screen", "elapsed": 5.0}

    def test_inconclusive_action_emits_listener_dead(self):
        captured = []
        recorder._emit_capture_health_event(
            "action", None, 5.0, lambda et, **f: captured.append((et, f)),
        )
        assert captured[0][1]["reason"] == "listener_dead"

    def test_reason_is_from_closed_set(self):
        from screencap._stderr_events import CAPTURE_UNHEALTHY_REASONS
        for reader in ("screen", "window", "action"):
            captured = []
            recorder._emit_capture_health_event(
                reader, None, 1.0, lambda et, **f: captured.append((et, f)),
            )
            assert captured[0][1]["reason"] in CAPTURE_UNHEALTHY_REASONS

    def test_real_emit_permission_lost_line_shape(self):
        from screencap._stderr_events import emit_event
        evts = _capture_stderr(
            lambda: recorder._emit_capture_health_event("screen", "screen_recording", 9.5, emit_event)
        )
        assert len(evts) == 1
        assert evts[0]["type"] == "permission_lost"
        assert evts[0]["permission"] == "screen_recording"
        assert isinstance(evts[0]["elapsed"], float)

    def test_real_emit_capture_unhealthy_line_shape(self):
        from screencap._stderr_events import emit_event
        evts = _capture_stderr(
            lambda: recorder._emit_capture_health_event("window", None, 9.5, emit_event)
        )
        assert len(evts) == 1
        assert evts[0]["type"] == "capture_unhealthy"
        assert evts[0]["reason"] == "reader_stalled"
        assert evts[0]["reader"] == "window"
        # advisory: no exit_code field
        assert "exit_code" not in evts[0]
