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
            loop = asyncio.get_running_loop()
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
            uvicorn_handle_exit = server_ref.handle_exit
            shutdown_task: asyncio.Task | None = None

            async def shutdown_app_state() -> None:
                if hasattr(app.state, "supervisor"):
                    await app.state.supervisor.shutdown()
                if hasattr(app.state, "event_bus"):
                    await app.state.event_bus.shutdown()
                server_ref.should_exit = True

            def request_shutdown() -> None:
                nonlocal shutdown_task
                if shutdown_task is None or shutdown_task.done():
                    shutdown_task = loop.create_task(shutdown_app_state())

            def handle_exit(signum: int, frame: FrameType | None) -> None:
                if server_ref.should_exit:
                    uvicorn_handle_exit(signum, frame)
                    return
                loop.call_soon_threadsafe(request_shutdown)

            server_ref.handle_exit = handle_exit

            def real_handler(signum: int, _frame: FrameType | None) -> None:
                nonlocal buffered_signal
                buffered_signal = signum
                request_shutdown()

            for sig in _handled_signals():
                signal.signal(sig, real_handler)

            if buffered_signal is not None:
                request_shutdown()

            try:
                await server_ref.serve(sockets=[listener])
            finally:
                if shutdown_task is not None and not shutdown_task.done():
                    await shutdown_task
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
