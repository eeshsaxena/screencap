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

import queue
import sys
import threading
import types

import pytest

from screencap.engine import recorder
from tests._capture_health_helpers import _ALIVE, _capture_stderr, _referenced_names


@pytest.fixture(autouse=True)
def _reset_health_state():
    """Module-level health state is reset per recording in production; reset it
    per test here so direct calls to the readers/helpers don't leak across tests."""
    recorder._capture_health_counts.clear()
    recorder._listener_handles.clear()
    yield
    recorder._capture_health_counts.clear()
    recorder._listener_handles.clear()


# ---------------------------------------------------------------------------
# U1 — raw per-reader attempt/output counters (driving the REAL reader code)
# ---------------------------------------------------------------------------


class _FakeFrame:
    """Stand-in for a PIL Image — only needs to be non-None and have .size."""
    size = (100, 100)


class TestCounters:
    def _run_screen_reader(self, monkeypatch, frames, *, display_asleep=False):
        """Drive read_screen_events with a take_screenshot stub yielding `frames`
        (one per iteration); terminate once the list is exhausted. `display_asleep`
        stubs utils.display_is_asleep() so the None-frame branch is exercised
        against a known display state (SCR-103).

        Returns the list of recorded recorder.time.sleep() call durations so a
        test can assert on the asleep-display backoff (Fix A / review #205)
        without incurring real multi-second sleeps."""
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

        sleep_durations: list = []

        def fake_sleep(duration=0, *_a, **_k):
            sleep_durations.append(duration)

        monkeypatch.setattr(utils, "take_screenshot", fake_shot)
        monkeypatch.setattr(utils, "display_is_asleep", lambda: display_asleep)
        monkeypatch.setattr(recorder.time, "sleep", fake_sleep)  # keep tests fast
        monkeypatch.setattr(recorder.config, "SCREEN_CAPTURE_FPS", 0)  # no throttle sleep
        rec = types.SimpleNamespace(timestamp=0.0)
        recorder.read_screen_events(
            queue.Queue(maxsize=100), term, rec, threading.Event(),
        )
        return sleep_durations

    def test_screen_attempt_and_output_advance_on_good_frames(self, monkeypatch):
        self._run_screen_reader(monkeypatch, [_FakeFrame() for _ in range(5)])
        assert recorder._capture_health_counts["screen.attempt"] == 5
        assert recorder._capture_health_counts["screen.output"] == 5

    def test_screen_none_frames_while_display_awake_increment_attempt_not_output(self, monkeypatch):
        # Display AWAKE + None frame = genuine screen-capture failure (incl.
        # Screen-Recording denial): the attempt/output gap must open so the stall
        # verdict + labeller still fire. A None frame is the only robust "broken
        # screen reader" content signal while the display is on.
        self._run_screen_reader(monkeypatch, [None for _ in range(5)], display_asleep=False)
        assert recorder._capture_health_counts["screen.attempt"] == 5
        assert recorder._capture_health_counts.get("screen.output", 0) == 0

    def test_screen_none_frames_while_display_asleep_are_idle_not_stall(self, monkeypatch):
        # SCR-103: a sleeping/locked display legitimately yields None — benign
        # idle, not a stall. attempt and output advance together so the gap never
        # opens and capture_unhealthy(reader=screen) does not fire.
        self._run_screen_reader(monkeypatch, [None for _ in range(5)], display_asleep=True)
        assert recorder._capture_health_counts["screen.attempt"] == 5
        assert recorder._capture_health_counts["screen.output"] == 5

    def test_screen_asleep_branch_backs_off(self, monkeypatch):
        # Fix A (review #205): the asleep-display None branch must back off to an
        # idle floor (>= 1.0s) independent of min_interval so an uncapped (fps<=0)
        # recording cannot busy-spin screencapture while the display sleeps. Each
        # None frame triggers exactly one such sleep; assert the floor is applied.
        sleeps = self._run_screen_reader(
            monkeypatch, [None for _ in range(5)], display_asleep=True
        )
        assert len(sleeps) == 5
        assert all(d >= 1.0 for d in sleeps)

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

    def test_window_no_active_window_is_idle_not_stall(self, monkeypatch):
        # SCR-103: a falsy poll is a benign no-active-window state (bare desktop,
        # Mission Control, Spotlight, menu-bar/Space focus), NOT a blind reader —
        # the window event rides CGWindowList, which needs no permission. It must
        # count as a completed (idle) poll so the attempt/output gap never opens
        # and capture_unhealthy(reader=window) does not fire.
        self._run_window_reader(monkeypatch, [{} for _ in range(4)])
        assert recorder._capture_health_counts["window.attempt"] == 4
        assert recorder._capture_health_counts["window.output"] == 4

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


def _drive(ticks, *, action_alive=True, debounce=3, alive=None, recovered=False):
    """Feed a sequence of (attempt_delta, output_delta) for the SCREEN reader
    through _capture_health_step and return the per-tick edge lists.

    By default returns the per-tick UNHEALTHY edge lists (the original contract).
    Pass ``recovered=True`` to return the per-tick RECOVERY edge lists instead
    (SCR-100), so a single helper covers both halves of the paired signal."""
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
        unhealthy, recovery = recorder._capture_health_step(
            prev, cur, alive, action_alive, debounce, runs, emitted,
        )
        out.append(recovery if recovered else unhealthy)
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


def test_recovery_after_emitted_unhealthy_yields_recovered_edge_once():
    # SCR-100: a reader that crossed the unhealthy edge (emitted at tick 2) and
    # then returns healthy reports a RECOVERY edge exactly once on the healthy
    # tick — the engine signal the shell uses to clear the stale advisory — and
    # not again while it stays healthy.
    ticks = [(20, 0), (20, 0), (20, 0), (20, 20), (20, 20)]
    unhealthy = _drive(ticks, debounce=3)
    recovered = _drive(ticks, debounce=3, recovered=True)
    assert unhealthy[2] == ["screen"]            # surfaced unhealthy at the edge
    assert recovered[3] == ["screen"]            # paired recovery, once
    assert all(r == [] for i, r in enumerate(recovered) if i != 3)


def test_transient_blip_below_debounce_recovers_silently():
    # SCR-100: a single stalled tick (below debounce=3) never surfaces an
    # unhealthy edge, so its recovery must be SILENT — no advisory was shown,
    # nothing to clear. A spurious recovery edge here would clear an advisory
    # the shell never set.
    ticks = [(20, 0), (20, 20), (20, 20)]
    unhealthy = _drive(ticks, debounce=3)
    recovered = _drive(ticks, debounce=3, recovered=True)
    assert all(e == [] for e in unhealthy)
    assert all(r == [] for r in recovered)


def test_recover_then_rebreak_emits_recovery_then_unhealthy_again():
    # SCR-100 + re-show: recovery fires once on the healthy tick (index 3), then
    # a fresh unhealthy edge fires again on the rebreak (index 6) so the advisory
    # re-shows. The two halves interleave correctly across the cycle.
    ticks = [(20, 0), (20, 0), (20, 0), (20, 20), (20, 0), (20, 0), (20, 0)]
    unhealthy = _drive(ticks, debounce=3)
    recovered = _drive(ticks, debounce=3, recovered=True)
    assert unhealthy[2] == ["screen"]
    assert recovered[3] == ["screen"]
    assert unhealthy[6] == ["screen"]
    assert all(r == [] for i, r in enumerate(recovered) if i != 3)


def test_dead_reader_after_emitted_unhealthy_is_not_reported_recovered():
    # SCR-100 guard: a reader that crossed the unhealthy edge and then goes
    # DEAD (alive=False) must not be reported as recovered — it did not recover,
    # it died, and the existing record.child_died path owns that case.
    runs, emitted = {}, {}
    prev = {"screen.attempt": 0, "screen.output": 0,
            "window.attempt": 0, "window.output": 0}
    cur = dict(prev)
    recovered_per_tick = []
    for tick in range(4):
        cur["screen.attempt"] += 20            # screen stalled (attempt, no output)
        cur["window.attempt"] += 2             # window healthy throughout
        cur["window.output"] += 2
        alive = {"screen": tick < 3, "window": True, "action": True}
        _u, recovery = recorder._capture_health_step(
            prev, cur, alive, True, 3, runs, emitted,
        )
        recovered_per_tick.append(recovery)
        prev = dict(cur)
    # Edge emitted at tick 2; screen goes dead at tick 3 → no recovery reported.
    assert all(r == [] for r in recovered_per_tick)


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
        )[0]
    assert edges == []


def test_action_listener_dead_fires_as_action_after_debounce():
    # AE2: listener dead while thread alive → unhealthy.
    runs, emitted = {}, {}
    fired = []
    for _ in range(4):
        fired.append(recorder._capture_health_step({}, {}, _ALIVE, False, 3, runs, emitted)[0])
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
        edges.append(recorder._capture_health_step(prev, cur, _ALIVE, True, 3, runs, emitted)[0])
        prev = dict(cur)
    # debounce=3: the first two stalled ticks must NOT fire; only the third
    # (the edge) does. Pinning each tick guards the window-reader debounce
    # boundary, not just the terminal value.
    assert edges[0] == []
    assert edges[1] == []
    assert edges[2] == ["window"]


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

    def test_only_screen_recording_label_is_terminal(self):
        # SCR-101: screen_recording denial is genuinely fatal (the screen capture
        # is dead) → terminal permission_lost. accessibility / input_monitoring
        # are best-guess attributions for a window/action stall whose true cause
        # is NOT those permissions (window output rides CGWindowList, action
        # rides the input listener), so they must stay advisory and never
        # self-stop an otherwise-healthy screen+audio recording.
        def _sink(et, **f):
            return None
        assert recorder._emit_capture_health_event("screen", "screen_recording", 1.0, _sink) == "permission_lost"
        assert recorder._emit_capture_health_event("window", "accessibility", 1.0, _sink) == "capture_unhealthy"
        assert recorder._emit_capture_health_event("action", "input_monitoring", 1.0, _sink) == "capture_unhealthy"

    def test_accessibility_label_emits_advisory_not_terminal(self):
        # SCR-101 regression: a window-reader stall mislabelled "accessibility"
        # (the labeller checks _ax first for the window reader) must NOT emit the
        # terminal permission_lost the shell turns into stop(). It surfaces as
        # the advisory capture_unhealthy, and the reason reflects the symptom.
        captured = []
        t = recorder._emit_capture_health_event(
            "window", "accessibility", 7.0, lambda et, **f: captured.append((et, f)),
        )
        assert t == "capture_unhealthy"
        assert captured == [
            ("capture_unhealthy", {"reason": "reader_stalled", "reader": "window", "elapsed": 7.0})
        ]

    def test_input_monitoring_label_emits_advisory_listener_dead(self):
        # SCR-101: an action-listener stall mislabelled "input_monitoring" stays
        # advisory; the reason still reflects the symptom (listener_dead).
        captured = []
        t = recorder._emit_capture_health_event(
            "action", "input_monitoring", 7.0, lambda et, **f: captured.append((et, f)),
        )
        assert t == "capture_unhealthy"
        assert captured == [
            ("capture_unhealthy", {"reason": "listener_dead", "reader": "action", "elapsed": 7.0})
        ]

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


# ---------------------------------------------------------------------------
# Window meta: empty-desktop guard (SCR-103 root mechanism for Case 1)
# ---------------------------------------------------------------------------


def test_get_active_window_meta_empty_desktop_returns_falsy(monkeypatch):
    # SCR-103: on a bare desktop the layer-0 / non-Window-Server filter yields an
    # empty list. The old code did `active_windows_info[0]` → IndexError on every
    # poll (caught upstream → {} → falsy poll, plus per-poll warning spam). The
    # guard returns a falsy meta explicitly instead of raising, so the no-window
    # state is an ordinary benign result rather than an exception.
    _macos = pytest.importorskip("screencap.engine.window._macos")
    # Only a Window Server window + a non-layer-0 window → filtered list empty.
    fake_windows = [
        {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Window Server"},
        {"kCGWindowLayer": 25, "kCGWindowOwnerName": "Dock"},
    ]
    monkeypatch.setattr(
        _macos.Quartz, "CGWindowListCopyWindowInfo", lambda *_a, **_k: fake_windows
    )
    assert not _macos.get_active_window_meta()


def test_get_active_window_meta_none_window_list_returns_falsy(monkeypatch):
    # SCR-108: CGWindowListCopyWindowInfo can return None on API failure. Without
    # the `if windows is None` guard (which its sibling get_all_window_geometries
    # already has), `for win in windows` raised TypeError on every poll — caught
    # upstream → {} → falsy poll, plus per-poll warning spam. The guard returns a
    # falsy meta explicitly, matching the documented bare-desktop contract rather
    # than riding the broad upstream except.
    _macos = pytest.importorskip("screencap.engine.window._macos")
    monkeypatch.setattr(
        _macos.Quartz, "CGWindowListCopyWindowInfo", lambda *_a, **_k: None
    )
    assert _macos.get_active_window_meta() == {}


def test_get_active_window_meta_none_window_list_warns_rate_limited(monkeypatch):
    # SCR-108: a None return is a genuine CGWindowListCopyWindowInfo failure (not a
    # benign bare desktop), so the guard emits an operator warning — but rate-limited
    # so a sustained failure does not re-introduce the per-poll spam the old TypeError
    # path produced. Two rapid None polls inside one throttle window → exactly one warning.
    _macos = pytest.importorskip("screencap.engine.window._macos")
    monkeypatch.setattr(
        _macos.Quartz, "CGWindowListCopyWindowInfo", lambda *_a, **_k: None
    )
    warnings: list[str] = []
    monkeypatch.setattr(_macos.logger, "warning", lambda msg, *_a, **_k: warnings.append(msg))
    # Deterministic: reset the module throttle and freeze the clock so both calls
    # fall inside one interval regardless of test order.
    monkeypatch.setattr(_macos, "_last_window_list_fail_warn_s", 0.0)
    monkeypatch.setattr(_macos.time, "monotonic", lambda: 1000.0)

    assert _macos.get_active_window_meta() == {}
    assert _macos.get_active_window_meta() == {}
    assert len(warnings) == 1


def test_get_active_window_state_empty_meta_returns_none(monkeypatch):
    # SCR-103: when get_active_window_meta() returns {} (bare desktop, Mission
    # Control, etc.), get_active_window_state must return None without raising a
    # KeyError on the meta dict — mirroring the empty-meta guard.
    _macos = pytest.importorskip("screencap.engine.window._macos")
    monkeypatch.setattr(_macos, "get_active_window_meta", lambda: {})
    assert _macos.get_active_window_state(read_window_data=False) is None


# ---------------------------------------------------------------------------
# utils.display_is_asleep — direct unit coverage (SCR-103 idle-vs-stall input)
# ---------------------------------------------------------------------------


class TestDisplayIsAsleep:
    def test_non_darwin_returns_false(self, monkeypatch):
        # Off macOS there is no Quartz display-sleep concept → fail-toward-awake.
        from screencap.engine import utils

        monkeypatch.setattr(utils.sys, "platform", "linux")
        assert utils.display_is_asleep() is False

    def test_exception_path_returns_false(self, monkeypatch):
        # A Quartz import/call error must be swallowed (logged at debug) and
        # err toward "awake" so a genuine capture failure still surfaces.
        from screencap.engine import utils

        monkeypatch.setattr(utils.sys, "platform", "darwin")

        class _BoomQuartz:
            def CGMainDisplayID(self):
                raise RuntimeError("quartz hiccup")

            def CGDisplayIsAsleep(self, _id):
                raise RuntimeError("quartz hiccup")

        monkeypatch.setitem(sys.modules, "Quartz", _BoomQuartz())
        assert utils.display_is_asleep() is False

    def test_darwin_true_and_false(self, monkeypatch):
        # With a fake Quartz, CGDisplayIsAsleep returning 1 → True, 0 → False.
        from screencap.engine import utils

        monkeypatch.setattr(utils.sys, "platform", "darwin")

        asleep_values = iter([1, 0])
        fake_q = types.SimpleNamespace(
            CGMainDisplayID=lambda: 0,
            CGDisplayIsAsleep=lambda _id: next(asleep_values),
        )
        monkeypatch.setitem(sys.modules, "Quartz", fake_q)
        assert utils.display_is_asleep() is True
        assert utils.display_is_asleep() is False
