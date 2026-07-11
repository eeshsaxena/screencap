"""In-engine stdin control channel (SCR-218 U1).

The daemon has no general way to reach a *running* engine: it can only SIGTERM
it, and stdin was ``DEVNULL``. This module makes the engine's stdin a
line-delimited JSON command reader — the exact mirror of the stderr-events-up
channel (``screencap._stderr_events.emit_event``). The daemon-side writer is
``Supervisor.send_command`` (``screencap.daemon.supervisor``).

Ownership is deliberately split: *this module* owns only the reader and the
``type``-dispatch registry. The command *handlers* live with the subsystem they
drive — the audio subsystem registers the ``set_muted`` handler in
``recorder.py`` (SCR-218 U2). Keeping the channel generic (``type``-dispatched)
lets future mid-recording controls reuse it without touching this file.

Robustness rules (a control channel that crashes the engine is worse than no
channel): a malformed line, an unknown ``type``, or a raising handler is logged
and dropped — never propagated. The reader runs on a daemon thread and returns
cleanly at EOF (the daemon closed the write end) or when a stop event is set, so
it never blocks recording teardown.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any, Callable, TextIO

CommandHandler = Callable[[dict[str, Any]], None]

logger = logging.getLogger(__name__)

# Module-global in the engine process. Guarded by ``_LOCK`` because the reader
# thread dispatches while the recording setup thread registers handlers.
_HANDLERS: dict[str, CommandHandler] = {}
_LOCK = threading.Lock()


def register_handler(command_type: str, handler: CommandHandler) -> None:
    """Register (or replace) the handler for one command ``type``."""
    with _LOCK:
        _HANDLERS[command_type] = handler


def unregister_handler(command_type: str) -> None:
    """Remove a handler; a no-op if none is registered."""
    with _LOCK:
        _HANDLERS.pop(command_type, None)


def dispatch_command(line: str) -> None:
    """Parse one NDJSON command line and dispatch it to its handler.

    A blank/malformed line, a non-object payload, an unknown ``type``, or a
    handler that raises are all logged and dropped — never propagated — so one
    bad command can never kill the reader or the recording.
    """
    line = line.strip()
    if not line:
        return
    try:
        command = json.loads(line)
    except (ValueError, TypeError):
        logger.warning("control_channel: dropping malformed command line")
        return
    if not isinstance(command, dict):
        logger.warning("control_channel: dropping non-object command")
        return
    command_type = command.get("type")
    with _LOCK:
        handler = _HANDLERS.get(command_type)
    if handler is None:
        logger.warning("control_channel: no handler for command type %r", command_type)
        return
    try:
        handler(command)
    except Exception:  # noqa: BLE001 — a bad handler must not kill the reader.
        logger.exception("control_channel: handler for %r raised", command_type)


def run_stdin_reader(
    stream: TextIO | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Read NDJSON command lines from ``stream`` (default ``sys.stdin``) until
    EOF or ``stop_event``.

    Designed to run on a daemon thread. EOF (the daemon closed the pipe or the
    engine is being torn down) and a closed stream both return cleanly, so this
    never blocks recording teardown.
    """
    stream = sys.stdin if stream is None else stream
    if stream is None:
        return
    try:
        for line in stream:
            if stop_event is not None and stop_event.is_set():
                return
            dispatch_command(line)
    except (ValueError, OSError):
        # Stream closed underneath us (broken pipe / detached stdin).
        return


def start_reader_thread(stop_event: threading.Event | None = None) -> threading.Thread:
    """Start the stdin reader on a daemon thread and return it."""
    thread = threading.Thread(
        target=run_stdin_reader,
        kwargs={"stop_event": stop_event},
        name="control-channel-reader",
        daemon=True,
    )
    thread.start()
    return thread
