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

StreamFactory = Callable[[], Any]

EVENT_MUTED = "audio_muted"
EVENT_UNMUTED = "audio_unmuted"


class AudioStreamController:
    def __init__(self, stream_factory: StreamFactory, *, initially_muted: bool) -> None:
        self._make_stream = stream_factory
        self._stream: Any | None = None
        self._capturing = False
        self._muted = bool(initially_muted)

    @property
    def capturing(self) -> bool:
        return self._capturing

    def start_initial(self) -> None:
        """Acquire and start the device at process start *only* if the recording
        started with audio on. If it started muted, acquire nothing."""
        if not self._muted:
            self._stream = self._make_stream()
            self._stream.start()
            self._capturing = True

    def apply_muted(self, muted: bool) -> str | None:
        """Idempotently drive the stream toward ``muted``.

        Returns ``EVENT_MUTED`` / ``EVENT_UNMUTED`` when capture actually
        changed, or ``None`` when already in the requested state. Raises if
        acquiring the device on unmute fails (e.g. denied mic); the controller
        stays not-capturing so the recording remains muted.
        """
        self._muted = bool(muted)
        if muted:
            if self._capturing:
                self._stream.stop()
                self._capturing = False
                return EVENT_MUTED
            return None
        # Unmute: acquire lazily on first use, then start.
        if not self._capturing:
            if self._stream is None:
                self._stream = self._make_stream()  # opens the device (may raise)
            self._stream.start()
            self._capturing = True
            return EVENT_UNMUTED
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
