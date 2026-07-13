"""Engine-side capture-pause control (SCR-214 U4).

Pause is modeled on the SCR-218 mic-mute chain but gates the WHOLE capture
surface — video, screenshots, AND audio — so a paused span records NOTHING (a
genuine capture gap that reads "nothing captured", AE1), distinct from a
mic-muted span (audio-only; video still captured → "unsplit — still searchable").

The hardware-coupled parts (the real ``sounddevice`` stream, the screenshot
grab) are not exercised here; the correctness-critical seams are:

* ``AudioStreamController.apply_paused`` — the pause suppression axis and how it
  composes with mute (capture runs only when neither muted nor paused).
* ``_CapturePauseState`` + ``_make_set_paused_handler`` — the engine-main gate
  the capture threads read, plus the confirmed-event emit + idempotency.
* ``process_events`` — the AUTHORITATIVE video/screenshot gate: while paused, no
  frame reaches any writer.

``Covers AE1.`` (paused span records nothing) and ``Covers R16.`` (a
never-paused recording is byte-for-byte unchanged) are called out on the
relevant tests.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from screencap._stderr_events import (
    EVENT_RECORDING_PAUSED,
    EVENT_RECORDING_RESUMED,
)
from screencap.engine import recorder as recmod
from screencap.engine.audio_mute import AudioStreamController
from screencap.engine.recorder import (
    Event,
    _apply_audio_pause_command,
    _CapturePauseState,
    _make_set_paused_handler,
    process_events,
)

# --------------------------------------------------------------------------- #
# _CapturePauseState — the engine-main gate + idempotency source
# --------------------------------------------------------------------------- #


def test_pause_state_apply_returns_true_only_on_change() -> None:
    state = _CapturePauseState()
    assert state.paused is False
    assert state.apply(True) is True  # False -> True
    assert state.paused is True
    assert state.apply(False) is True  # True -> False
    assert state.paused is False


def test_pause_state_apply_is_idempotent() -> None:
    state = _CapturePauseState()
    assert state.apply(True) is True
    assert state.apply(True) is False  # already paused -> no change
    assert state.apply(False) is True
    assert state.apply(False) is False  # already running -> no change


# --------------------------------------------------------------------------- #
# AudioStreamController.apply_paused — the audio suppression axis (privacy)
# --------------------------------------------------------------------------- #


class _FakeStream:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.closed = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1


def _controller(*, initially_muted: bool, construct_raises: bool = False):
    made: list[_FakeStream] = []

    def factory() -> _FakeStream:
        if construct_raises:
            raise OSError("microphone denied")
        s = _FakeStream()
        made.append(s)
        return s

    return AudioStreamController(factory, initially_muted=initially_muted), made


@pytest.mark.privacy
def test_pause_stops_stream_and_reports_transition() -> None:
    ctrl, made = _controller(initially_muted=False)
    ctrl.start_initial()  # capturing
    event = ctrl.apply_paused(True)
    assert event == EVENT_RECORDING_PAUSED
    assert made[0].stopped == 1
    assert ctrl.capturing is False


@pytest.mark.privacy
def test_resume_after_pause_restarts_same_stream() -> None:
    ctrl, made = _controller(initially_muted=False)
    ctrl.start_initial()
    ctrl.apply_paused(True)
    event = ctrl.apply_paused(False)
    assert event == EVENT_RECORDING_RESUMED
    assert len(made) == 1  # reused, not reconstructed
    assert made[0].started == 2
    assert ctrl.capturing is True


@pytest.mark.privacy
def test_pause_when_already_paused_is_noop() -> None:
    ctrl, _made = _controller(initially_muted=False)
    ctrl.start_initial()
    assert ctrl.apply_paused(True) == EVENT_RECORDING_PAUSED
    assert ctrl.apply_paused(True) is None  # already paused
    assert ctrl.apply_paused(False) == EVENT_RECORDING_RESUMED
    assert ctrl.apply_paused(False) is None  # already running


@pytest.mark.privacy
def test_pause_on_audio_off_recording_acquires_no_device() -> None:
    # A --no-audio (initially muted) recording that is paused must still touch no
    # device: pausing is a no-op (nothing capturing), and resuming stays off
    # because the mute axis still suppresses capture.
    ctrl, made = _controller(initially_muted=True)
    ctrl.start_initial()
    assert ctrl.apply_paused(True) is None
    assert ctrl.apply_paused(False) is None  # muted still suppresses
    assert made == []
    assert ctrl.capturing is False


@pytest.mark.privacy
def test_resume_stays_off_until_unmute_when_also_muted() -> None:
    # Compose: running -> mute (stops) -> pause (already stopped, no-op) ->
    # unmute (paused still suppresses) -> resume (now capture starts).
    ctrl, made = _controller(initially_muted=False)
    ctrl.start_initial()  # capturing on stream #1
    assert ctrl.apply_muted(True) is not None  # stop, muted
    assert ctrl.apply_paused(True) is None  # already stopped -> no-op
    assert ctrl.apply_muted(False) is None  # paused suppresses -> no restart
    assert ctrl.capturing is False
    assert ctrl.apply_paused(False) == EVENT_RECORDING_RESUMED
    assert ctrl.capturing is True
    assert made[0].started == 2  # same stream restarted


@pytest.mark.privacy
def test_mute_stays_off_until_resume_when_also_paused() -> None:
    # Compose the other order: running -> pause (stops) -> mute (already stopped,
    # no-op) -> resume (muted still suppresses) -> unmute (now capture starts).
    ctrl, made = _controller(initially_muted=False)
    ctrl.start_initial()
    assert ctrl.apply_paused(True) == EVENT_RECORDING_PAUSED
    assert ctrl.apply_muted(True) is None  # already stopped
    assert ctrl.apply_paused(False) is None  # muted suppresses -> no restart
    assert ctrl.capturing is False
    assert ctrl.apply_muted(False) is not None  # unmute finally restarts
    assert ctrl.capturing is True
    assert made[0].started == 2


@pytest.mark.privacy
def test_resume_reacquire_failure_raises_and_stays_paused() -> None:
    # A resume that must (re)construct the device — the recording started
    # audio-off, was unmuted while paused, so the stream was never built — and
    # the device is denied: apply_paused raises and the controller stays paused
    # (no partial "resumed" state), mirroring apply_muted's contract.
    ctrl, _made = _controller(initially_muted=True, construct_raises=True)
    ctrl.start_initial()  # nothing constructed
    ctrl.apply_paused(True)  # paused (no-op on the stream)
    ctrl.apply_muted(False)  # clear mute; paused still suppresses, stream is None
    with pytest.raises(OSError):
        ctrl.apply_paused(False)  # must construct -> denied
    assert ctrl.capturing is False


# --------------------------------------------------------------------------- #
# _apply_audio_pause_command — the record_audio seam
# --------------------------------------------------------------------------- #


@pytest.mark.privacy
def test_apply_audio_pause_command_delegates() -> None:
    ctrl, made = _controller(initially_muted=False)
    ctrl.start_initial()
    assert _apply_audio_pause_command(ctrl, {"paused": True}) == EVENT_RECORDING_PAUSED
    assert ctrl.capturing is False
    assert made[0].stopped == 1


@pytest.mark.privacy
def test_apply_audio_pause_command_swallows_device_error_on_resume() -> None:
    # A denied re-acquire on resume must NOT crash the audio process — it is
    # logged and swallowed (fail-open), audio simply stays off.
    ctrl, _made = _controller(initially_muted=True, construct_raises=True)
    ctrl.start_initial()
    _apply_audio_pause_command(ctrl, {"paused": True})
    ctrl.apply_muted(False)  # clear mute; stream is None
    result = _apply_audio_pause_command(ctrl, {"paused": False})  # would raise
    assert result is None  # swallowed
    assert ctrl.capturing is False  # stays off


# --------------------------------------------------------------------------- #
# _make_set_paused_handler — engine-main gate + confirmed-event emit
# --------------------------------------------------------------------------- #


class _FakeQueue:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put(self, item, timeout=None) -> None:
        self.items.append(item)


def _handler_with_emit_capture(monkeypatch, pause_state, control_q):
    """Build a set_paused handler with emit_event captured (patched BEFORE build
    since the factory binds emit_event at build time)."""
    import screencap._stderr_events as ev

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(ev, "emit_event", lambda name, **f: emitted.append((name, f)))
    monkeypatch.setattr(recmod.utils, "get_timestamp", lambda: 500.0)
    handler = _make_set_paused_handler(pause_state, control_q)
    return handler, emitted


def test_handler_pause_sets_flag_forwards_and_emits_confirmed(monkeypatch) -> None:
    state = _CapturePauseState()
    q = _FakeQueue()
    handler, emitted = _handler_with_emit_capture(monkeypatch, state, q)

    handler({"type": "set_paused", "paused": True})

    assert state.paused is True  # video/screenshot gate set
    assert q.items == [{"paused": True, "ts": 500.0}]  # forwarded to audio child
    assert emitted == [(EVENT_RECORDING_PAUSED, {"paused": True})]  # confirmed event


def test_handler_resume_emits_resumed(monkeypatch) -> None:
    state = _CapturePauseState()
    state.apply(True)  # start paused
    q = _FakeQueue()
    handler, emitted = _handler_with_emit_capture(monkeypatch, state, q)

    handler({"type": "set_paused", "paused": False})

    assert state.paused is False
    assert q.items == [{"paused": False, "ts": 500.0}]
    assert emitted == [(EVENT_RECORDING_RESUMED, {"paused": False})]


def test_handler_pause_while_paused_is_idempotent(monkeypatch) -> None:
    state = _CapturePauseState()
    state.apply(True)  # already paused
    q = _FakeQueue()
    handler, emitted = _handler_with_emit_capture(monkeypatch, state, q)

    handler({"type": "set_paused", "paused": True})

    # No state change -> no forward to the audio child, no duplicate event.
    assert q.items == []
    assert emitted == []


def test_handler_without_queue_still_gates_and_emits(monkeypatch) -> None:
    # Defensive: a missing pause queue must not crash the reader — the in-process
    # video gate + confirmed event still fire (video is gated regardless of audio).
    state = _CapturePauseState()
    handler, emitted = _handler_with_emit_capture(monkeypatch, state, None)

    handler({"type": "set_paused", "paused": True})

    assert state.paused is True
    assert emitted == [(EVENT_RECORDING_PAUSED, {"paused": True})]


# --------------------------------------------------------------------------- #
# process_events — the AUTHORITATIVE video/screenshot gate (AE1)
# --------------------------------------------------------------------------- #


class _FakeCounter:
    def __init__(self) -> None:
        self.value = 0


def _run_process_events(monkeypatch, *, paused: bool, full_video: bool,
                        n_events: int = 3):
    """Drive ``process_events`` to completion over ``n_events`` pre-loaded screen
    events and return the (screen_q, video_q, num_screen, num_video) results.

    Deterministic: terminate is pre-set, so the loop drains the pre-loaded queue
    and exits — no threads, no timing. ``pause_state`` reflects ``paused`` for the
    whole run. With ``full_video`` each screen event deterministically enqueues a
    video event (the control), so the paused/running contrast is unambiguous.
    """
    # Keep the harness free of the AX cache thread + immediate-video determinism.
    monkeypatch.setattr(recmod.config, "RECORD_READ_ACTIVE_ELEMENT_STATE", False)
    monkeypatch.setattr(recmod.config, "RECORD_FULL_VIDEO", full_video)

    event_q: queue.Queue = queue.Queue()
    for i in range(n_events):
        # data can be any object — image writers are not run; process_events only
        # enqueues screen/video events onto the (fake) write queues.
        event_q.put(Event(1000.0 + i, "screen", object(), None))

    screen_q, video_q = _FakeQueue(), _FakeQueue()
    action_q, window_q, perf_q = _FakeQueue(), _FakeQueue(), _FakeQueue()
    num_screen, num_action = _FakeCounter(), _FakeCounter()
    num_window, num_video = _FakeCounter(), _FakeCounter()

    pause_state = _CapturePauseState()
    if paused:
        pause_state.apply(True)

    terminate = threading.Event()
    terminate.set()  # drain-then-exit
    started = threading.Event()
    recording = SimpleNamespace(timestamp=1000.0, pixel_ratio=1.0)

    process_events(
        event_q, screen_q, action_q, window_q, video_q, perf_q, recording,
        terminate, started, num_screen, num_action, num_window, num_video,
        screen_filter=None, dead_queues=set(), pause_state=pause_state,
    )
    return screen_q, video_q, num_screen, num_video


@pytest.mark.privacy
def test_process_events_paused_writes_nothing(monkeypatch) -> None:
    """Covers AE1. A paused span reaches NO writer — no screenshots, no video,
    counters stay 0 — so it records nothing (a genuine gap, not unsplit footage).
    The 3 events stand in for a span that may cross a chunk rotation: the gate
    lives in process_events, which is rotation-agnostic, so pause is honored
    regardless of where a chunk boundary falls."""
    screen_q, video_q, num_screen, num_video = _run_process_events(
        monkeypatch, paused=True, full_video=True,
    )
    assert screen_q.items == []
    assert video_q.items == []
    assert num_screen.value == 0
    assert num_video.value == 0


@pytest.mark.privacy
def test_process_events_running_writes_video(monkeypatch) -> None:
    """Control for the paused case: with the SAME setup but not paused, each
    screen event enqueues a video frame — proving the gate (not some other
    filter) is what suppresses capture while paused."""
    _screen_q, video_q, _num_screen, num_video = _run_process_events(
        monkeypatch, paused=False, full_video=True, n_events=3,
    )
    assert len(video_q.items) == 3
    assert num_video.value == 3
