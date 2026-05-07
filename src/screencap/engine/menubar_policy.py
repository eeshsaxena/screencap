"""``MenubarPolicy`` seam (SCR-41, slice 4b of SCR-31).

Promotes the ``_skip_menubar_spawn``-gated spawn block and the three IPC
queues (``window_feed_q``, ``override_q``, ``disable_q``) from
``LegacyOptions`` to a pluggable policy. ``SpawnNewMenubar`` is the
standalone-CLI implementation; ``Noop`` is what ``SessionController``
workers use because the controller owns the persistent menubar.

The queues themselves are already first-class args on ``ScreenRecorder``
via ``IpcChannels``. This slice retires the ``_external_*`` injection
pattern and the ``_skip_menubar_spawn`` guard in favour of the two
concrete policy objects.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from rich.console import Console

if TYPE_CHECKING:
    from screencap.engine.screen_recorder import IpcChannels

_console = Console()


class MenubarPolicy(Protocol):
    """Menubar subprocess + IPC queue lifecycle."""

    @property
    def proc(self) -> multiprocessing.Process | None: ...

    @property
    def state_file(self) -> Path | None: ...

    @property
    def owns_channels(self) -> bool: ...

    def spawn(
        self,
        recording_name: str,
        start_time: float,
        capture_dir: Path,
        channels: IpcChannels,
        *,
        audio_enabled: bool,
        prompt_enabled: bool,
    ) -> None: ...

    def notify_processing(self) -> None: ...

    def kill(self) -> None: ...


class SpawnNewMenubar:
    """Standalone-CLI menubar policy: spawn a new menubar subprocess.

    Wraps ``_spawn_menubar`` / ``_kill_menubar`` from
    ``screencap.recorder``. Holds ``_proc`` and ``_state_file`` as
    instance state so the legacy ``start_recording`` 4-tuple adapter
    and ``DiskFullError`` can still surface them without touching
    ``ScreenRecorder``.
    """

    def __init__(self) -> None:
        self._proc: multiprocessing.Process | None = None
        self._state_file: Path | None = None

    @property
    def proc(self) -> multiprocessing.Process | None:
        return self._proc

    @property
    def state_file(self) -> Path | None:
        return self._state_file

    @property
    def owns_channels(self) -> bool:
        return True

    def spawn(
        self,
        recording_name: str,
        start_time: float,
        capture_dir: Path,
        channels: IpcChannels,
        *,
        audio_enabled: bool,
        prompt_enabled: bool,
    ) -> None:
        try:
            from screencap.recorder import _spawn_menubar

            self._state_file = capture_dir / ".menubar_state"
            self._proc = _spawn_menubar(
                recording_name,
                start_time,
                self._state_file,
                window_feed_q=channels.window_feed,
                override_q=channels.override,
                prompt_enabled=prompt_enabled,
                disable_q=channels.disable,
                audio_enabled=audio_enabled,
            )
            _console.print(
                "  [#f472b6]●[/#f472b6] [dim]Menu bar active — "
                "click the [#f472b6]red dot[/#f472b6] in your menu bar to stop[/dim]"
            )
        except Exception:
            self._proc = None

    def notify_processing(self) -> None:
        if self._state_file is None:
            return
        try:
            from screencap.menubar import STATE_PROCESSING

            self._state_file.write_text(STATE_PROCESSING)
        except Exception:
            pass

    def kill(self) -> None:
        from screencap.recorder import _kill_menubar

        _kill_menubar(self._proc, self._state_file)
        self._proc = None


class Noop:
    """Session-worker menubar policy: do nothing.

    The ``SessionController`` owns the persistent menubar and the IPC
    queues that talk to it. Workers must not spawn a competing menubar
    process or close the shared queues on exit.
    """

    @property
    def proc(self) -> multiprocessing.Process | None:
        return None

    @property
    def state_file(self) -> Path | None:
        return None

    @property
    def owns_channels(self) -> bool:
        return False

    def spawn(
        self,
        recording_name: str,
        start_time: float,
        capture_dir: Path,
        channels: IpcChannels,
        *,
        audio_enabled: bool,
        prompt_enabled: bool,
    ) -> None:
        pass

    def notify_processing(self) -> None:
        pass

    def kill(self) -> None:
        pass
