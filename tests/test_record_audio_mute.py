"""Engine-side mute integration inside record_audio (SCR-218 U2).

The hardware-coupled parts of ``record_audio`` (the ``sounddevice`` stream, the
FLAC writer, the poll loop) need a real mic to exercise end to end. The
*correctness-critical* logic is isolated into two seams so it is unit-testable
without hardware:

* ``_apply_audio_mute_command`` — the confirmed-state contract (KTD4): the
  ``muted_intervals`` row and the ``audio_muted`` / ``audio_unmuted`` event are
  written/emitted ONLY after the stream actually toggled, the interval start_ts
  is the command-receipt time (over-covering the stop latency, KTD3), and a
  denied unmute emits the advisory ``audio_unmute_failed`` and stays muted (R3).
* ``_make_set_muted_handler`` — the engine-main handler that stamps the
  command-receipt time and forwards ``{muted, ts}`` to the audio child.

Full capture behavior (muted spans hold no audio, audio-off never lights the mic
indicator) is covered by the manual capture QA runbook — it needs a real mic.
"""

from __future__ import annotations

import pytest

from screencap._stderr_events import (
    AUDIO_UNMUTE_FAILED_REASON_MIC_UNAVAILABLE,
    EVENT_AUDIO_MUTED,
    EVENT_AUDIO_UNMUTE_FAILED,
    EVENT_AUDIO_UNMUTED,
)
from screencap.engine.audio_mute import AudioStreamController
from screencap.engine.db import crud
from screencap.engine.db.models import MutedInterval
from screencap.engine.recorder import (
    _apply_audio_mute_command,
    _make_set_muted_handler,
)

pytestmark = pytest.mark.privacy


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
    """Build a real AudioStreamController over a fake stream factory."""
    made: list[_FakeStream] = []

    def factory():
        if construct_raises:
            raise OSError("microphone denied")
        s = _FakeStream()
        made.append(s)
        return s

    return AudioStreamController(factory, initially_muted=initially_muted), made


def _emit_recorder():
    events: list[tuple[str, dict]] = []

    def emit(name, **fields):
        events.append((name, fields))

    return events, emit


def _all_intervals(db):
    return (
        db.session.query(MutedInterval)
        .filter(MutedInterval.recording_id == db.recording.id)
        .order_by(MutedInterval.start_ts)
        .all()
    )


# --------------------------------------------------------------------------- #
# _apply_audio_mute_command — confirmed-state contract
# --------------------------------------------------------------------------- #


def test_mute_while_capturing_opens_interval_and_emits_confirmed(recording_db):
    ctrl, _made = _controller(initially_muted=False)
    ctrl.start_initial()  # capturing
    events, emit = _emit_recorder()

    result = _apply_audio_mute_command(
        ctrl,
        {"muted": True, "ts": 1010.0},
        session=recording_db.session,
        recording=recording_db.recording,
        emit=emit,
    )

    assert result == EVENT_AUDIO_MUTED
    assert ctrl.capturing is False
    # Interval opened at the command-receipt ts (over-covers stop latency), open.
    rows = _all_intervals(recording_db)
    assert len(rows) == 1
    assert rows[0].start_ts == 1010.0
    assert rows[0].end_ts is None
    # Event emitted only after the real stop (KTD4).
    assert events == [(EVENT_AUDIO_MUTED, {"muted": True})]


def test_mute_when_already_muted_is_noop(recording_db):
    # Audio-off recording never unmuted: not capturing, already muted.
    ctrl, made = _controller(initially_muted=True)
    ctrl.start_initial()  # acquires nothing
    events, emit = _emit_recorder()

    result = _apply_audio_mute_command(
        ctrl,
        {"muted": True, "ts": 1010.0},
        session=recording_db.session,
        recording=recording_db.recording,
        emit=emit,
    )

    assert result is None
    assert made == []  # no device ever constructed
    assert _all_intervals(recording_db) == []  # no interval for a no-op mute
    assert events == []  # no confirmed event for a no-op


def test_unmute_closes_interval_and_emits_confirmed(recording_db):
    ctrl, _made = _controller(initially_muted=False)
    ctrl.start_initial()
    events, emit = _emit_recorder()

    # Mute, then unmute.
    _apply_audio_mute_command(
        ctrl, {"muted": True, "ts": 1010.0},
        session=recording_db.session, recording=recording_db.recording, emit=emit,
    )
    result = _apply_audio_mute_command(
        ctrl, {"muted": False, "ts": 1025.0},
        session=recording_db.session, recording=recording_db.recording, emit=emit,
    )

    assert result == EVENT_AUDIO_UNMUTED
    assert ctrl.capturing is True
    rows = _all_intervals(recording_db)
    assert len(rows) == 1
    assert rows[0].start_ts == 1010.0
    assert rows[0].end_ts == 1025.0
    assert events[-1] == (EVENT_AUDIO_UNMUTED, {"muted": False})


def test_unmute_denied_emits_advisory_and_stays_muted(recording_db):
    # Audio-off recording; the first unmute must construct the device, which is
    # denied. R3: surface a visible advisory, never silently no-op, stay muted.
    ctrl, _made = _controller(initially_muted=True, construct_raises=True)
    ctrl.start_initial()  # nothing constructed
    events, emit = _emit_recorder()

    result = _apply_audio_mute_command(
        ctrl,
        {"muted": False, "ts": 1030.0},
        session=recording_db.session,
        recording=recording_db.recording,
        emit=emit,
    )

    assert result == EVENT_AUDIO_UNMUTE_FAILED
    assert ctrl.capturing is False  # stays muted
    # No unmute event, an advisory with a closed-set reason instead.
    names = [n for n, _ in events]
    assert EVENT_AUDIO_UNMUTED not in names
    assert events == [
        (EVENT_AUDIO_UNMUTE_FAILED,
         {"reason": AUDIO_UNMUTE_FAILED_REASON_MIC_UNAVAILABLE}),
    ]


def test_denied_unmute_does_not_close_an_open_interval(recording_db):
    # A mute opened an interval; if the unmute's stream re-start fails (e.g. the
    # device went away mid-recording), the interval must NOT be closed — the
    # recording is still muted, so the span stays open (U6 → muted to chunk end).
    class _FlakyStream:
        def __init__(self) -> None:
            self.started = 0

        def start(self) -> None:
            self.started += 1
            if self.started > 1:  # re-start after mute fails
                raise OSError("device disappeared")

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    ctrl = AudioStreamController(lambda: _FlakyStream(), initially_muted=False)
    ctrl.start_initial()  # start() #1 → capturing
    events, emit = _emit_recorder()
    _apply_audio_mute_command(
        ctrl, {"muted": True, "ts": 1010.0},
        session=recording_db.session, recording=recording_db.recording, emit=emit,
    )
    result = _apply_audio_mute_command(
        ctrl, {"muted": False, "ts": 1020.0},  # start() #2 → raises
        session=recording_db.session, recording=recording_db.recording, emit=emit,
    )

    assert result == EVENT_AUDIO_UNMUTE_FAILED
    assert ctrl.capturing is False  # still muted
    rows = _all_intervals(recording_db)
    assert len(rows) == 1
    assert rows[0].end_ts is None  # still open — the failed unmute left it open


def test_command_ts_is_used_verbatim_as_start_ts(recording_db):
    # The interval start_ts is the handler-stamped command-receipt time, not a
    # fresh clock read at write time — that is what over-covers the stop latency.
    ctrl, _made = _controller(initially_muted=False)
    ctrl.start_initial()
    _events, emit = _emit_recorder()

    _apply_audio_mute_command(
        ctrl, {"muted": True, "ts": 42.5},
        session=recording_db.session, recording=recording_db.recording, emit=emit,
    )

    assert _all_intervals(recording_db)[0].start_ts == 42.5


# --------------------------------------------------------------------------- #
# _make_set_muted_handler — engine-main forward
# --------------------------------------------------------------------------- #


class _FakeQueue:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put(self, item, timeout=None) -> None:
        self.items.append(item)


def test_handler_stamps_receipt_ts_and_forwards(monkeypatch):
    from screencap.engine import recorder as recmod

    monkeypatch.setattr(recmod.utils, "get_timestamp", lambda: 777.0)
    q = _FakeQueue()
    handler = _make_set_muted_handler(q)

    handler({"type": "set_muted", "muted": True})

    assert q.items == [{"muted": True, "ts": 777.0}]


def test_handler_coerces_muted_to_bool(monkeypatch):
    from screencap.engine import recorder as recmod

    monkeypatch.setattr(recmod.utils, "get_timestamp", lambda: 1.0)
    q = _FakeQueue()
    _make_set_muted_handler(q)({"type": "set_muted", "muted": 0})

    assert q.items[0]["muted"] is False


def test_handler_without_queue_does_not_raise(monkeypatch):
    from screencap.engine import recorder as recmod

    monkeypatch.setattr(recmod.utils, "get_timestamp", lambda: 1.0)
    # No queue wired (defensive): the handler must swallow, not crash the reader.
    _make_set_muted_handler(None)({"type": "set_muted", "muted": True})
