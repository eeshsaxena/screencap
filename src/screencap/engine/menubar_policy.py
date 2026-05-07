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

import logging
import multiprocessing
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from rich.console import Console

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from screencap.engine.screen_recorder import IpcChannels

_console = Console()


# ---------------------------------------------------------------------------
# Menu bar subprocess helpers
# ---------------------------------------------------------------------------


def _spawn_menubar(
    recording_name: str,
    start_time: float,
    state_file: Path,
    window_feed_q: multiprocessing.Queue | None = None,
    override_q: multiprocessing.Queue | None = None,
    prompt_enabled: bool = True,
    disable_q: multiprocessing.Queue | None = None,
    *,
    audio_enabled: bool = True,
) -> multiprocessing.Process | None:
    """Spawn the menu bar status item as a daemon subprocess.

    Returns the Process object on success, or None if spawn fails.
    The process is daemonic so it is killed when the parent exits.

    Args:
        prompt_enabled: When True, the menubar shows a non-activating
            NSPanel the first time a never-seen ``(app, domain)`` pair
            becomes the frontmost window during the recording.
        disable_q: Optional queue the menubar writes to when the user
            toggles a target to ``exclude``. The recorder's scrub worker
            consumes this queue to retroactively delete already-captured
            rows for that target.
        audio_enabled: Initial state of the "Audio (next recording)"
            toggle shown in the menu.  Legacy / non-session path only —
            in session mode the :class:`SessionController` passes the
            value directly to ``_run_menubar``.
    """
    from screencap.menubar import _run_menubar

    proc = multiprocessing.Process(
        target=_run_menubar,
        args=(os.getpid(), recording_name, start_time, str(state_file),
              window_feed_q, override_q, prompt_enabled, disable_q),
        kwargs={"audio_enabled": audio_enabled},
        daemon=True,
        name="menubar",
    )
    proc.start()
    return proc


def _kill_menubar(
    proc: multiprocessing.Process | None,
    state_file: Path | None = None,
) -> None:
    """Terminate the menu bar subprocess.  Safe to call multiple times."""
    if proc is None:
        return
    # Signal via state file first (allows clean AppKit shutdown)
    if state_file is not None:
        try:
            from screencap.menubar import STATE_DONE
            state_file.write_text(STATE_DONE)
        except Exception:
            pass
    # SIGKILL immediately — the menu bar is a UI helper, no data to flush.
    pid = getattr(proc, "pid", None)
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


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
        except Exception as exc:  # noqa: BLE001
            self._proc = None
            _logger.warning("menubar spawn failed: %r", exc, exc_info=True)
            _console.print(
                f"  [yellow]Menu bar unavailable[/yellow] [dim]({exc!r}) — "
                "stop with [bold]screencap stop[/bold] or Ctrl+C[/dim]",
            )

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
