"""Mic-stream mute controller (SCR-218 U2).

Owns the ``sounddevice.InputStream`` lifecycle for live mute/unmute inside the
``record_audio`` process. Extracted from ``record_audio`` so its correctness —
which is hardware-independent — is unit-testable with a fake stream.

Two properties matter most:

* **Lazy device acquisition.** *Constructing* an ``InputStream`` calls
  ``Pa_OpenStream``, which opens the CoreAudio input device and evaluates
  microphone TCC — it does not defer to ``.start()``. So a recording that
  started audio-off must not construct the stream until the first unmute, or it
  would light the mic-in-use indicator (or trip a permission prompt from the
  background daemon) on a recording the user explicitly asked to be silent.
* **Confirmed transitions.** ``apply_muted`` returns the event name only when
  capture *actually* changed, so the parent emits ``audio_muted`` /
  ``audio_unmuted`` after the real stop/start — never on the mere request.

The controller is not thread-safe on its own; the caller serializes toggles on
the ``record_audio`` main thread and guards the shared FLAC writer separately.
"""

from __future__ import annotations

from typing import Any, Callable

from screencap._stderr_events import (
    EVENT_AUDIO_MUTED,
    EVENT_AUDIO_UNMUTED,
    EVENT_RECORDING_PAUSED,
    EVENT_RECORDING_RESUMED,
)

StreamFactory = Callable[[], Any]

# Single-source the event names from _stderr_events (which documents them as the
# rename-in-one-place constants); a local literal would reintroduce the drift.
EVENT_MUTED = EVENT_AUDIO_MUTED
EVENT_UNMUTED = EVENT_AUDIO_UNMUTED
# SCR-214 U4: transition markers returned by ``apply_paused``. The audio child
# does NOT emit these on the event stream (engine-main is the sole emitter, since
# a muted/audio-off recording has no stream to toggle yet must still confirm
# pause because video stopped). They exist so ``apply_paused``'s confirmed-
# transition contract is unit-testable, mirroring EVENT_MUTED / EVENT_UNMUTED.
EVENT_PAUSED = EVENT_RECORDING_PAUSED
EVENT_RESUMED = EVENT_RECORDING_RESUMED


class AudioStreamController:
    def __init__(self, stream_factory: StreamFactory, *, initially_muted: bool) -> None:
        self._make_stream = stream_factory
        self._stream: Any | None = None
        self._capturing = False
        self._muted = bool(initially_muted)
        # SCR-214 U4: pause is a SECOND, independent suppression axis. Capture
        # runs only when NEITHER muted NOR paused, so pause and mute compose
        # (resuming a paused-then-unmuted stream must not start capture while the
        # other axis still suppresses it). Constructed unpaused, so a recording
        # that is never paused behaves exactly as before (R16).
        self._paused = False

    @property
    def capturing(self) -> bool:
        return self._capturing

    def start_initial(self) -> None:
        """Acquire and start the device at process start *only* if the recording
        started with audio on (and is not paused). If it started muted or paused,
        acquire nothing."""
        if not self._muted and not self._paused:
            self._stream = self._make_stream()
            self._stream.start()
            self._capturing = True

    def apply_muted(self, muted: bool) -> str | None:
        """Idempotently drive the stream toward ``muted``.

        Returns ``EVENT_MUTED`` / ``EVENT_UNMUTED`` when capture actually
        changed, or ``None`` when already in the requested state. Raises if
        acquiring the device on unmute fails (e.g. denied mic); the controller
        stays not-capturing so the recording remains muted.

        ``_muted`` is updated only *after* the transition succeeds — a raised
        device-open on unmute leaves ``_muted`` at its prior (muted) value, so
        internal state never claims "unmuted" while the mic is actually silent.
        """
        muted = bool(muted)
        if muted:
            if self._capturing:
                self._stream.stop()
                self._capturing = False
                self._muted = True
                return EVENT_MUTED
            self._muted = True
            return None
        # Unmute. If the recording is ALSO paused, capture must stay off — clear
        # only the mute intent so a later resume starts the stream (SCR-214 U4).
        if self._paused:
            self._muted = False
            return None
        # Acquire lazily on first use, then start. If _make_stream raises,
        # _muted is left unchanged (still muted) — no partial "unmuted" state.
        if not self._capturing:
            if self._stream is None:
                self._stream = self._make_stream()  # opens the device (may raise)
            self._stream.start()
            self._capturing = True
            self._muted = False
            return EVENT_UNMUTED
        self._muted = False
        return None

    def apply_paused(self, paused: bool) -> str | None:
        """Idempotently drive the stream toward ``paused`` (SCR-214 U4).

        Pause is independent of mute: capture runs only when neither muted nor
        paused. Returns ``EVENT_PAUSED`` / ``EVENT_RESUMED`` when capture actually
        changed, or ``None`` when the stream was already in the requested state
        (e.g. resuming a recording that is still mic-muted is a no-op). Raises if
        re-acquiring the device on resume fails; ``_paused`` is left True so the
        stream stays off — mirroring ``apply_muted``'s no-partial-state contract.
        """
        paused = bool(paused)
        if paused:
            if self._capturing:
                self._stream.stop()
                self._capturing = False
                self._paused = True
                return EVENT_PAUSED
            self._paused = True
            return None
        # Resume. If the recording is ALSO mic-muted, capture must stay off —
        # clear only the pause intent so a later unmute starts the stream.
        if self._muted:
            self._paused = False
            return None
        if not self._capturing:
            if self._stream is None:
                self._stream = self._make_stream()  # opens the device (may raise)
            self._stream.start()
            self._capturing = True
            self._paused = False
            return EVENT_RESUMED
        self._paused = False
        return None

    def shutdown(self) -> None:
        """Stop and close the stream if it was ever constructed. Idempotent and
        swallows teardown errors — a broken device on the way out is not fatal."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        self._capturing = False
