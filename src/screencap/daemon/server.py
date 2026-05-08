"""Uvicorn server orchestration for the ScreenCap daemon."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from types import FrameType
from typing import Callable

SignalHandler = Callable[[int, FrameType | None], None]


def _handled_signals() -> tuple[signal.Signals, ...]:
    return (signal.SIGTERM, signal.SIGINT)


def serve(socket_path: str | Path | None = None, *, self_test: bool = False) -> int:
    """Run the daemon server or execute its hidden smoke self-test."""
    buffered_signal: int | None = None
    server_ref = None

    def early_handler(signum: int, _frame: FrameType | None) -> None:
        nonlocal buffered_signal
        buffered_signal = signum

    previous_handlers: dict[signal.Signals, SignalHandler | int | None] = {}
    for sig in _handled_signals():
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, early_handler)

    marker = os.environ.get("SCREENCAP_DAEMON_SIGNAL_READY_MARKER")
    if marker:
        Path(marker).write_text("ready\n", encoding="utf-8")

    from screencap.daemon.socket import (
        DaemonAlreadyRunning,
        RogueFileAtSocketPath,
        bind_unix_socket,
        cleanup_socket,
        default_socket_path,
    )

    try:
        from screencap.daemon.app import build_app

        if self_test:
            app = build_app()
            if app is None:
                raise RuntimeError("daemon app construction returned None")
            with tempfile.TemporaryDirectory() as tmpdir:
                dry_run_path = Path(tmpdir) / "api.sock"
                listener = bind_unix_socket(dry_run_path)
                listener.close()
                cleanup_socket(dry_run_path)
            return 0

        resolved_socket_path = Path(socket_path).expanduser() if socket_path else default_socket_path()
        listener = bind_unix_socket(resolved_socket_path)

        async def run() -> int:
            nonlocal server_ref, buffered_signal

            import uvicorn

            app = build_app()
            config = uvicorn.Config(
                app,
                uds=str(resolved_socket_path),
                lifespan="on",
                loop="asyncio",
                http="h11",
                log_config=None,
                access_log=False,
            )
            server_ref = uvicorn.Server(config)

            def real_handler(signum: int, _frame: FrameType | None) -> None:
                nonlocal buffered_signal
                buffered_signal = signum
                server_ref.should_exit = True

            for sig in _handled_signals():
                signal.signal(sig, real_handler)

            if buffered_signal is not None:
                server_ref.should_exit = True

            try:
                await server_ref.serve(sockets=[listener])
            finally:
                listener.close()
                cleanup_socket(resolved_socket_path)
            return 0

        return asyncio.run(run())
    except (DaemonAlreadyRunning, RogueFileAtSocketPath) as exc:
        _print_stderr(str(exc))
        return 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def _print_stderr(message: str) -> None:
    try:
        from rich.console import Console

        Console(stderr=True).print(message)
    except Exception:
        print(message, file=sys.stderr)
