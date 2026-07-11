"""Unit tests for the in-engine stdin control channel (SCR-218 U1).

The control channel is the daemon->engine command path: the daemon writes
NDJSON command lines to the engine's stdin (Supervisor.send_command), and this
module's reader parses each line and dispatches by ``type`` to a handler the
audio subsystem registers. These tests cover the parse/dispatch/reader half in
isolation (no subprocess); the supervisor-write half is in
tests/daemon/test_supervisor_send_command.py.
"""

from __future__ import annotations

import io
import threading

from screencap.engine import control_channel


def _fresh_registry() -> None:
    # Each test starts from a clean handler registry.
    for command_type in list(control_channel._HANDLERS):  # type: ignore[attr-defined]
        control_channel.unregister_handler(command_type)


def test_dispatch_routes_to_registered_handler_by_type() -> None:
    _fresh_registry()
    seen: list[dict] = []
    control_channel.register_handler("set_muted", seen.append)

    control_channel.dispatch_command('{"type": "set_muted", "muted": true}')

    assert seen == [{"type": "set_muted", "muted": True}]


def test_unknown_type_is_ignored() -> None:
    _fresh_registry()
    seen: list[dict] = []
    control_channel.register_handler("set_muted", seen.append)

    # No handler registered for "something_else" -> dropped, no raise.
    control_channel.dispatch_command('{"type": "something_else"}')

    assert seen == []


def test_malformed_line_is_ignored() -> None:
    _fresh_registry()
    # A partial / non-JSON line must not raise or kill the reader.
    control_channel.dispatch_command("{not json")
    control_channel.dispatch_command("")
    control_channel.dispatch_command("   ")
    control_channel.dispatch_command("[1, 2, 3]")  # JSON, but not an object


def test_handler_exception_is_swallowed() -> None:
    _fresh_registry()

    def boom(_command: dict) -> None:
        raise RuntimeError("handler blew up")

    control_channel.register_handler("set_muted", boom)

    # A raising handler must not propagate; the reader must survive it.
    control_channel.dispatch_command('{"type": "set_muted", "muted": false}')


def test_reader_feeds_multiple_lines_then_stops_at_eof() -> None:
    _fresh_registry()
    seen: list[dict] = []
    control_channel.register_handler("set_muted", seen.append)

    stream = io.StringIO(
        '{"type": "set_muted", "muted": true}\n'
        "{garbage}\n"
        '{"type": "set_muted", "muted": false}\n'
    )
    # Returns cleanly at EOF (no hang); malformed middle line is skipped.
    control_channel.run_stdin_reader(stream=stream)

    assert seen == [
        {"type": "set_muted", "muted": True},
        {"type": "set_muted", "muted": False},
    ]


def test_reader_honors_stop_event() -> None:
    _fresh_registry()
    seen: list[dict] = []
    control_channel.register_handler("set_muted", seen.append)
    stop = threading.Event()
    stop.set()

    control_channel.run_stdin_reader(
        stream=io.StringIO('{"type": "set_muted", "muted": true}\n'),
        stop_event=stop,
    )

    # Stop already set -> reader returns without dispatching.
    assert seen == []


def test_reader_returns_when_stream_is_none() -> None:
    _fresh_registry()
    # A None stream (e.g. detached stdin) must be a clean no-op, not a crash.
    control_channel.run_stdin_reader(stream=None)
