"""Unit tests for the mic-stream mute controller (SCR-218 U2).

The controller owns the InputStream lifecycle for mute/unmute. Its core
correctness properties are hardware-independent and tested here with a fake
stream: the mic device is acquired *lazily* (constructing the stream is what
opens CoreAudio + evaluates TCC), so an audio-off recording that is never
unmuted must never construct a stream; toggles are idempotent; and each real
toggle reports the confirmed transition so the parent can emit the event only
after capture actually changed.
"""

from __future__ import annotations

import pytest

from screencap.engine.audio_mute import AudioStreamController


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


def _make_controller(initially_muted: bool):
    made: list[_FakeStream] = []

    def factory() -> _FakeStream:
        s = _FakeStream()
        made.append(s)
        return s

    return AudioStreamController(factory, initially_muted=initially_muted), made


def test_audio_on_start_constructs_and_starts_stream() -> None:
    ctrl, made = _make_controller(initially_muted=False)
    ctrl.start_initial()
    assert len(made) == 1
    assert made[0].started == 1
    assert ctrl.capturing is True


def test_audio_off_start_acquires_no_device() -> None:
    ctrl, made = _make_controller(initially_muted=True)
    ctrl.start_initial()
    # The whole point: a --no-audio recording never constructs the stream,
    # so the mic device / TCC is never touched.
    assert made == []
    assert ctrl.capturing is False


def test_mute_stops_stream_and_reports_transition() -> None:
    ctrl, made = _make_controller(initially_muted=False)
    ctrl.start_initial()
    event = ctrl.apply_muted(True)
    assert event == "audio_muted"
    assert made[0].stopped == 1
    assert ctrl.capturing is False


def test_unmute_after_mute_restarts_same_stream() -> None:
    ctrl, made = _make_controller(initially_muted=False)
    ctrl.start_initial()
    ctrl.apply_muted(True)
    event = ctrl.apply_muted(False)
    assert event == "audio_unmuted"
    assert len(made) == 1  # reused, not reconstructed
    assert made[0].started == 2
    assert ctrl.capturing is True


def test_unmute_on_audio_off_recording_lazily_constructs() -> None:
    ctrl, made = _make_controller(initially_muted=True)
    ctrl.start_initial()
    assert made == []  # nothing acquired yet
    event = ctrl.apply_muted(False)
    assert event == "audio_unmuted"
    assert len(made) == 1  # device acquired only now
    assert made[0].started == 1
    assert ctrl.capturing is True


def test_idempotent_toggles_report_no_change() -> None:
    ctrl, _made = _make_controller(initially_muted=False)
    ctrl.start_initial()
    assert ctrl.apply_muted(False) is None  # already capturing
    ctrl.apply_muted(True)
    assert ctrl.apply_muted(True) is None  # already muted


def test_denied_device_on_unmute_raises_and_stays_stopped() -> None:
    def factory():
        raise OSError("microphone access denied")

    ctrl = AudioStreamController(factory, initially_muted=True)
    ctrl.start_initial()
    with pytest.raises(OSError):
        ctrl.apply_muted(False)
    # Failed acquisition leaves the controller not-capturing (stays muted).
    assert ctrl.capturing is False


def test_shutdown_stops_and_closes_when_constructed() -> None:
    ctrl, made = _make_controller(initially_muted=False)
    ctrl.start_initial()
    ctrl.shutdown()
    assert made[0].stopped >= 1
    assert made[0].closed == 1


def test_shutdown_is_noop_when_never_constructed() -> None:
    ctrl, made = _make_controller(initially_muted=True)
    ctrl.start_initial()
    ctrl.shutdown()  # never unmuted -> nothing to close
    assert made == []


# --- SCR-288: policy-skip (factory returns None) ---------------------------

def _make_scripted_controller(initially_muted: bool, script: list):
    """A factory that yields ``script[i]`` per call — a ``_FakeStream`` or ``None``.

    ``None`` models the Bluetooth-aware mic-source policy choosing to skip (only
    a Bluetooth input exists and nobody else holds the mic).
    """
    made: list[_FakeStream] = []
    calls = [0]

    def factory():
        item = script[calls[0]] if calls[0] < len(script) else None
        calls[0] += 1
        if item is not None:
            made.append(item)
        return item

    return AudioStreamController(factory, initially_muted=initially_muted), made, calls


def test_skip_on_start_stays_not_capturing() -> None:
    """An audio-on start whose policy skips leaves the controller not-capturing."""
    ctrl, made, calls = _make_scripted_controller(False, script=[None])
    ctrl.start_initial()
    assert calls[0] == 1  # the policy was consulted
    assert made == []  # but no stream opened
    assert ctrl.capturing is False


def test_skip_on_unmute_reports_no_transition() -> None:
    """A skip on unmute returns None (no audio_unmuted) so the muted span stays open."""
    ctrl, made, calls = _make_scripted_controller(True, script=[None])
    ctrl.start_initial()
    assert ctrl.apply_muted(False) is None  # skipped -> no confirmed transition
    assert ctrl.capturing is False
    assert made == []


def test_unmute_reevaluates_policy_after_a_skip() -> None:
    """R7: a later unmute re-consults the policy — a prior skip is not sticky.

    Models AirPods-only-not-in-a-call (skip), then a call starts and the next
    unmute captures.
    """
    stream = _FakeStream()
    ctrl, made, calls = _make_scripted_controller(True, script=[None, stream])
    ctrl.start_initial()
    assert ctrl.apply_muted(False) is None  # first open skips
    event = ctrl.apply_muted(False)  # re-evaluated -> now captures
    assert event == "audio_unmuted"
    assert ctrl.capturing is True
    assert made == [stream]
    assert calls[0] == 2  # factory consulted on each open


def test_skip_on_resume_reports_no_transition() -> None:
    """A skip on resume (pause->resume acquisition site) reports no transition."""
    ctrl, made, calls = _make_scripted_controller(True, script=[None])
    ctrl.start_initial()
    ctrl.apply_paused(True)  # paused (no stream yet)
    ctrl.apply_muted(False)  # clear mute intent; still paused, no stream
    assert ctrl.apply_paused(False) is None  # resume -> policy skips
    assert ctrl.capturing is False
    assert made == []
