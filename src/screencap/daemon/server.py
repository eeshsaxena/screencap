"""Uvicorn server orchestration for the Screencap daemon."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import FrameType

SignalHandler = Callable[[int, FrameType | None], None]

# `sysexits.h` semantic: "temporary failure, try again". launchd sees this
# as a recoverable condition and an operator can grep `launchctl print` for
# the value to distinguish "rogue same-EUID listener" from the generic
# exit-1 path (e.g., RogueFileAtSocketPath).
EX_TEMPFAIL = 75

logger = logging.getLogger(__name__)


def _handled_signals() -> tuple[signal.Signals, ...]:
    return (signal.SIGTERM, signal.SIGINT)


def serve(
    socket_path: str | Path | None = None,
    *,
    self_test: bool = False,
    idle_shutdown_seconds: float | None = None,
) -> int:
    """Run the daemon server or execute its hidden smoke self-test.

    ``idle_shutdown_seconds`` opts the daemon into the F3 auto-spawn
    lifecycle: when set, an idle-shutdown watchdog drains and exits the
    daemon after the configured seconds with no requests, subscribers,
    or active recordings. LaunchAgent-managed daemons leave this
    ``None`` and run all day.
    """
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
        SocketPermsDrift,
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

        # SCR-258 U4 (KTD-14): the daemon binds FIRST, then resolves the
        # encrypted-store state. A sealed (locked) / not-yet-initialized (absent) /
        # key-error / occupied-mountpoint store is a HEALTHY serving state — the
        # daemon serves ``store_state`` on ``daemon.info`` + read verbs so a locked
        # store is distinguishable from a dead daemon (never a launchd crash-loop).
        # Only attach-time operator failures (corrupted bundle, auth failure) are
        # fatal operator stops; those carry the right exit code (operator -> 1,
        # retryable -> EX_TEMPFAIL) on the container exception. This runs AFTER
        # bind so the ordering is observable (the bind-before-mount amendment).
        from screencap.container import ContainerError
        from screencap.daemon import store_lifecycle

        try:
            store_resolution = store_lifecycle.resolve_store_state()
        except ContainerError as exc:
            _print_stderr(str(exc))
            listener.close()
            cleanup_socket(resolved_socket_path)
            return exc.exit_code

        async def run() -> int:
            nonlocal server_ref, buffered_signal

            import uvicorn

            app = build_app()
            # Publish the resolved store state so ``daemon.info`` + the read verbs
            # can surface it and the supervisor (created in the lifespan) can read
            # it as the recording.start refusal flag.
            app.state.store_resolution = store_resolution
            app.state.store_state = store_resolution.state.value
            app.state.store_reason = store_resolution.reason
            if idle_shutdown_seconds is not None and idle_shutdown_seconds > 0:
                from screencap.daemon._idle_shutdown import attach as _attach_idle

                _attach_idle(app, idle_shutdown_seconds)
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

            watchdog_task: asyncio.Task | None = None
            if idle_shutdown_seconds is not None and idle_shutdown_seconds > 0:
                from screencap.daemon._idle_shutdown import run_watchdog

                async def _safe_watchdog() -> None:
                    try:
                        await run_watchdog(app, request_shutdown=request_shutdown)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "idle-shutdown watchdog raised unexpectedly; "
                            "requesting daemon shutdown (fail-safe)"
                        )
                        request_shutdown()

                watchdog_task = loop.create_task(_safe_watchdog())

                def _watchdog_done(task: asyncio.Task) -> None:
                    if task.cancelled():
                        return
                    exc = task.exception()
                    if exc is not None:
                        # Exception already logged inside _safe_watchdog.
                        logger.warning(
                            "idle-shutdown watchdog task finished with exception: %r", exc
                        )

                watchdog_task.add_done_callback(_watchdog_done)

            try:
                await server_ref.serve(sockets=[listener])
            finally:
                if watchdog_task is not None and not watchdog_task.done():
                    watchdog_task.cancel()
                if shutdown_task is not None and not shutdown_task.done():
                    await shutdown_task
                listener.close()
                cleanup_socket(resolved_socket_path)
            return 0

        return asyncio.run(run())
    except DaemonAlreadyRunning as exc:
        pid_str = str(exc.existing_pid) if exc.existing_pid is not None else "unknown"
        logger.warning("daemon socket already bound by pid=%s", pid_str)
        _print_stderr(str(exc))
        _print_stderr(f"daemon socket already bound by pid={pid_str}")
        return EX_TEMPFAIL
    except RogueFileAtSocketPath as exc:
        _print_stderr(str(exc))
        return 1
    except SocketPermsDrift as exc:
        _print_stderr(str(exc))
        return EX_TEMPFAIL
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def _print_stderr(message: str) -> None:
    from rich.console import Console

    Console(stderr=True).print(message)
