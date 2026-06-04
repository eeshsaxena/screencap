"""Engine subprocess supervision for daemon-owned recordings."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import fcntl
import json
import logging
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from screencap import _stderr_events
from screencap.daemon import errors, schema
from screencap.daemon.event_bus import CursorOutOfRangeError, EventBus, _Subscription
from screencap.pidfile import CLAIMANT_DAEMON

if TYPE_CHECKING:
    from screencap.daemon.schema import RecordingStartRequest

logger = logging.getLogger(__name__)

EngineCommandFactory = Callable[[str], list[str]]

__all__ = ["Supervisor", "_extra_output_dir_allowlist"]

# Extra output-dir allowlist entries — populated by test fixtures or callers
# that legitimately need a path outside the default recordings root.
# The canonical allowlist is ``[get_recordings_dir().resolve()]``; entries
# here are checked *in addition to* that default. Tests should monkeypatch
# this list rather than hard-coding a path assumption in production code.
_extra_output_dir_allowlist: list[Path] = []

# Engine-stderr kernel pipe widening — buys headroom for `_stderr_pump`
# against engine-side burst writes so a briefly-slow pump drain does not
# transitively block the engine's `sys.stderr.write`. 1 MiB is ~16× the
# typical macOS/Linux default; the EINVAL fallback at 128 KiB covers
# kernels that cap below 1 MiB. Linux-only at the syscall level —
# `F_SETPIPE_SZ` is absent on macOS, where the helper degrades to no-op
# and the pipe stays at the kernel default. If production traces ever
# show pump stalls past this ceiling, that's the trigger for the
# deferred TKT-D approach (2): asyncio queue + drop policy.
_STDERR_PIPE_SIZE = 1 << 20  # 1 MiB
_STDERR_PIPE_FALLBACK = 1 << 17  # 128 KiB


def _widen_stderr_pipe(proc: subprocess.Popen[Any]) -> int | None:
    """Resize the kernel stderr pipe for ``proc`` to ``_STDERR_PIPE_SIZE``.

    Returns the size that was applied, or ``None`` when widening was not
    available (no ``F_SETPIPE_SZ`` on this platform) or failed in any other
    way. Never raises — pipe sizing is a diagnostic safety net, not a
    correctness invariant. No-op on macOS (``F_SETPIPE_SZ`` absent).
    """
    set_pipe_sz = getattr(fcntl, "F_SETPIPE_SZ", None)
    if set_pipe_sz is None:
        logger.info(
            "engine stderr pipe widening unavailable: fcntl.F_SETPIPE_SZ not on this platform"
        )
        return None

    stderr = getattr(proc, "stderr", None)
    if stderr is None:
        return None

    try:
        fd = stderr.fileno()
    except (AttributeError, OSError):
        return None

    for size in (_STDERR_PIPE_SIZE, _STDERR_PIPE_FALLBACK):
        try:
            fcntl.fcntl(fd, set_pipe_sz, size)
        except OSError as exc:
            if exc.errno == errno.EINVAL and size == _STDERR_PIPE_SIZE:
                continue
            logger.warning(
                "engine stderr pipe widening failed (size=%d): %s", size, exc
            )
            return None
        logger.info("engine stderr pipe widened to %d bytes", size)
        return size
    return None


class _PopenEngineProcess:
    """Subprocess wrapper exposing a uniform ``is_alive``/``terminate``/``kill``/``stderr``
    interface so ``Supervisor`` is testable against a protocol rather than directly
    against ``subprocess.Popen``."""

    def __init__(self, argv: list[str], *, extra_env: dict[str, str] | None = None) -> None:
        self._popen = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            # extra_env carries the out-of-band engine ID-token *file path* (never
            # the token itself, never argv) for cloud recordings — see
            # Supervisor._stage_engine_token. It is set ONLY on the engine
            # subprocess, never on the daemon's own os.environ, so the daemon's
            # auth.get_id_token() keeps reading the Keychain.
            env={**os.environ, "PYTHONUNBUFFERED": "1", **(extra_env or {})},
        )
        # Widen the kernel stderr pipe as close to the Popen as possible so
        # the engine never gets a chance to write into a default-sized pipe.
        _widen_stderr_pipe(self._popen)

    @property
    def pid(self) -> int:
        return int(self._popen.pid)

    @property
    def stderr(self) -> Any:
        return self._popen.stderr

    @property
    def returncode(self) -> int | None:
        return self._popen.poll()

    def is_alive(self) -> bool:
        return self._popen.poll() is None

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()

    async def wait(self, timeout: float | None = None) -> int:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._popen.wait),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _default_engine_command(encoded_args: str) -> list[str]:
    override = os.environ.get("SCREENCAP_DAEMON_ENGINE_COMMAND")
    if override:
        decoded = json.loads(override)
        if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
            raise ValueError("SCREENCAP_DAEMON_ENGINE_COMMAND must be a JSON string list")
        return [
            item.replace("{encoded_args}", encoded_args)
            for item in decoded
        ]

    if getattr(sys, "frozen", False):
        return [sys.executable, "_engine-worker", encoded_args]
    return [sys.executable, "-m", "screencap", "_engine-worker", encoded_args]


def build_engine_worker_args(
    request: "RecordingStartRequest",
    *,
    name: str,
    capture_dir: Path,
) -> dict[str, Any]:
    """Build the arg dict the engine worker is dispatched with.

    A ``RecordingStartRequest`` model dump plus the ``name`` / ``output_dir`` /
    ``capture_dir_hint`` the supervisor injects. The ``_*_q`` queue keys are
    created downstream by the engine worker command itself, so they are
    intentionally absent here. Shared with the dispatch integration test so the
    arg shape stays in lockstep with production.
    """
    args = request.model_dump()
    args["name"] = name
    args["output_dir"] = str(capture_dir)
    args["capture_dir_hint"] = str(capture_dir)
    return args


class Supervisor:
    """Own daemon recording lifecycle, crash recovery, and stderr bridging."""

    def __init__(
        self,
        event_bus: EventBus,
        *,
        engine_command_factory: EngineCommandFactory | None = None,
        reconcile_on_init: bool = True,
        poll_interval: float | None = None,
        startup_timeout: float | None = None,
        stop_timeout: float | None = None,
        reconcile_grace: float | None = None,
    ) -> None:
        self._bus = event_bus
        self._engine_command_factory = engine_command_factory or _default_engine_command
        self._poll_interval = (
            poll_interval
            if poll_interval is not None
            else _float_env("SCREENCAP_DAEMON_POLL_INTERVAL", 1.0)
        )
        self._startup_timeout = (
            startup_timeout
            if startup_timeout is not None
            else _float_env("SCREENCAP_DAEMON_STARTUP_TIMEOUT", 10.0)
        )
        self._stop_timeout = (
            stop_timeout
            if stop_timeout is not None
            else _float_env("SCREENCAP_DAEMON_STOP_TIMEOUT", 30.0)
        )
        self._reconcile_grace = (
            reconcile_grace
            if reconcile_grace is not None
            else _float_env("SCREENCAP_DAEMON_RECONCILE_GRACE", 30.0)
        )

        self._proc: _PopenEngineProcess | None = None
        self._engine_pid: int | None = None
        self._session_state: dict[str, Any] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        # Out-of-band engine ID-token seam (cloud recordings only). The file holds
        # the short-lived token; the refresh task re-mints it for recordings that
        # outlast the ~1h token. Both are torn down in _reset_state.
        self._engine_token_file: Path | None = None
        self._token_refresh_task: asyncio.Task[None] | None = None
        self._recovering = False
        self._finalized_seen = False
        self._stopping = False
        self._exit_handled = False
        self._operation_lock = asyncio.Lock()
        self._exit_lock = asyncio.Lock()

        if reconcile_on_init:
            self.start_reconcile()

    def start_reconcile(self) -> None:
        """Start startup orphan reconciliation in the background."""
        if self._reconcile_task is None or self._reconcile_task.done():
            self._recovering = True
            self._reconcile_task = asyncio.create_task(self._reconcile())

    def is_recovering(self) -> bool:
        return self._recovering

    def current_session(self) -> dict[str, Any] | None:
        if self._session_state is None:
            return None
        snapshot = dict(self._session_state)
        if self._proc is not None:
            snapshot["engine_pid"] = self._proc.pid
        elif self._engine_pid is not None:
            snapshot["engine_pid"] = self._engine_pid
        return snapshot

    async def spawn(self, request: "RecordingStartRequest") -> dict[str, Any]:
        """Claim the daemon lock, spawn the engine worker, and await started."""
        async with self._operation_lock:
            if self._recovering:
                raise errors.ReconcilingError(
                    schema_version=schema._RECORDING_START_API_VERSION
                )
            if self._proc is not None and self._proc.is_alive():
                owner = self._owner_payload()
                raise errors.LockContendedError(
                    owner,
                    schema_version=schema._RECORDING_START_API_VERSION,
                )

            from screencap import pidfile

            try:
                pidfile.claim_lock(None, claimant=CLAIMANT_DAEMON)
            except pidfile.LockContended as exc:
                raise errors.LockContendedError(
                    exc.owner,
                    schema_version=schema._RECORDING_START_API_VERSION,
                ) from None

            name, capture_dir = self._allocate_capture_dir(request)
            started_at = time.time()
            started_by = request.started_by
            pidfile.update_lock_metadata(
                capture_dir,
                recording_started_at=started_at,
                recording_name=name,
                started_by=started_by,
            )

            args = self._worker_args(request, name=name, capture_dir=capture_dir)
            encoded_args = base64.b64encode(
                json.dumps(args, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            # Capture the cursor BEFORE the engine spawn so the response gives
            # callers an atomic late-join boundary: every event from this
            # recording (starting with `started`) carries cursor > start_cursor.
            start_cursor = self._bus.current_cursor()
            started_sub = await self._bus.subscribe()
            try:
                extra_env = await self._stage_engine_token(request, capture_dir)
                command = self._engine_command_factory(encoded_args)
                proc = _PopenEngineProcess(command, extra_env=extra_env)
                self._proc = proc
                self._engine_pid = proc.pid
                self._finalized_seen = False
                self._stopping = False
                self._exit_handled = False
                self._session_state = {
                    "session_id": name,
                    "recording_name": name,
                    "capture_dir": str(capture_dir),
                    "started_at": started_at,
                    "started_by": started_by,
                    "engine_pid": proc.pid,
                }
                pidfile.update_lock_metadata(capture_dir, engine_pid=proc.pid)
                self._stderr_task = asyncio.create_task(self._stderr_pump(proc))
                self._poll_task = asyncio.create_task(self._exit_poll(proc))
                if extra_env is not None:
                    # Long-recording re-mint: rewrite the token file before the
                    # ~1h ID token expires. Self-terminates when the engine exits.
                    self._token_refresh_task = asyncio.create_task(
                        self._token_refresh_loop(proc)
                    )
                await self._wait_on_subscription(
                    started_sub,
                    _stderr_events.EVENT_STARTED,
                    timeout=self._startup_timeout,
                )
            except Exception:
                # Cancel the background pump/poll tasks before tearing down so
                # they don't race the lock release with a late `_handle_engine_exit`.
                for _t in (self._stderr_task, self._poll_task, self._token_refresh_task):
                    if _t is not None and not _t.done():
                        _t.cancel()
                try:
                    await self._terminate_current_process(force=True)
                finally:
                    self._release_daemon_lock()
                    self._reset_state()
                raise
            finally:
                await self._bus.remove(started_sub)

            return {
                "session_id": name,
                "started_at": started_at,
                "engine_pid": proc.pid,
                "cursor": start_cursor,
            }

    async def stop(
        self,
        force: bool = False,
        expected_claimant_pid: int | None = None,
        expected_started_at: float | None = None,
    ) -> dict[str, Any]:
        """Stop the active recording or a CAS-approved cross-claimant holder."""
        async with self._operation_lock:
            if self._recovering:
                raise errors.ReconcilingError(
                    schema_version=schema._RECORDING_STOP_API_VERSION
                )
            from screencap import pidfile

            metadata = pidfile.read_lock_metadata()
            if metadata is None or not pidfile.lock_is_active():
                self._clear_stale_lock_if_unheld()
                return {"stopped": False, "final_state": "no_recording"}

            claimant = metadata.get("claimant")
            if claimant != CLAIMANT_DAEMON:
                if not force:
                    raise errors.NotOwnedByDaemonError(
                        claimant,
                        schema_version=schema._RECORDING_STOP_API_VERSION,
                    )
                self._verify_force_cas(
                    metadata,
                    claimant=claimant,
                    expected_claimant_pid=expected_claimant_pid,
                    expected_started_at=expected_started_at,
                )
                stopped = await self._terminate_pid(
                    int(metadata["pid"]),
                    grace=self._stop_timeout,
                )
                return {
                    "stopped": bool(stopped),
                    "final_state": "stopped" if stopped else "not_terminated",
                }

            # TOCTOU defense (TKT-A): capture the bus cursor BEFORE the
            # is_alive() check so the late `final_sub` subscription can
            # replay any `recording_finalized` published by `_exit_poll`
            # in the await gap. Without this, the engine can self-exit
            # between is_alive() returning True and the live-only
            # subscribe() landing, and stop() blocks for the full
            # stop_timeout while the event sits unobserved on the bus.
            pre_check_cursor = self._bus.current_cursor()

            # Cancel _exit_poll before the late subscribe so a concurrent
            # iteration cannot publish `recording_finalized` *and* run
            # _handle_engine_exit between the cursor capture and the
            # subscription — which would double-release the lock and
            # double-publish the finalized event from stop()'s own teardown.
            await self._cancel_exit_poll()

            if self._proc is None or not self._proc.is_alive():
                engine_pid = metadata.get("engine_pid")
                if isinstance(engine_pid, int):
                    stopped = await self._terminate_pid(engine_pid, grace=self._stop_timeout)
                    self._release_daemon_lock()
                    return {
                        "stopped": bool(stopped),
                        "final_state": "stopped" if stopped else "not_terminated",
                    }
                self._release_daemon_lock()
                return {"stopped": False, "final_state": "no_engine"}

            final_sub = await self._subscribe_with_replay_fallback(
                pre_check_cursor, label="stop"
            )
            proc = self._proc
            self._stopping = True
            try:
                proc.terminate()
                try:
                    await self._wait_on_subscription(
                        final_sub,
                        _stderr_events.EVENT_RECORDING_FINALIZED,
                        timeout=self._stop_timeout,
                    )
                    final_state = "stopped"
                except asyncio.TimeoutError:
                    if proc.is_alive():
                        proc.kill()
                    final_state = "force_stopped"
                try:
                    rc = await proc.wait(timeout=1.0)
                except asyncio.TimeoutError:
                    if proc.is_alive():
                        proc.kill()
                    rc = await proc.wait(timeout=2.0)
                    final_state = "force_stopped"
                await self._handle_engine_exit(proc, rc)
            finally:
                await self._bus.remove(final_sub)
            return {"stopped": True, "final_state": final_state}

    async def reconcile_orphans(self) -> None:
        """Public hook for tests and explicit startup reconciliation."""
        await self._reconcile()

    async def shutdown(self) -> None:
        """Stop any owned engine and release the daemon lock."""
        if self._proc is not None and self._proc.is_alive():
            proc = self._proc
            self._stopping = True
            # Same TOCTOU defense as Supervisor.stop(): capture the cursor
            # before `terminate()` so the late subscription replays any
            # `recording_finalized` the engine's SIGTERM handler publishes
            # in the await gap.
            pre_terminate_cursor = self._bus.current_cursor()
            # Cancel _exit_poll before the late subscribe so a concurrent
            # iteration cannot run _handle_engine_exit between the cursor
            # capture and the subscription (same race as Supervisor.stop()).
            await self._cancel_exit_poll()
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            final_sub = await self._subscribe_with_replay_fallback(
                pre_terminate_cursor, label="shutdown"
            )
            try:
                try:
                    await self._wait_on_subscription(
                        final_sub,
                        _stderr_events.EVENT_RECORDING_FINALIZED,
                        timeout=self._stop_timeout,
                    )
                except asyncio.TimeoutError:
                    if proc.is_alive():
                        proc.kill()
                try:
                    rc = await proc.wait(timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    try:
                        rc = await proc.wait(timeout=2.0)
                    except asyncio.TimeoutError:
                        # Zombie post-SIGKILL — proceed with teardown so
                        # `_release_daemon_lock` + `_reset_state` below still run.
                        rc = -9
                await self._handle_engine_exit(proc, rc)
            finally:
                await self._bus.remove(final_sub)

        if self._reconcile_task is not None and not self._reconcile_task.done():
            try:
                await asyncio.wait_for(self._reconcile_task, timeout=self._reconcile_grace + 1.0)
            except asyncio.TimeoutError:
                self._reconcile_task.cancel()
        for task in (self._stderr_task, self._poll_task):
            if task is not None and not task.done():
                task.cancel()
        self._release_daemon_lock()
        self._reset_state()

    async def _reconcile(self) -> None:
        from screencap import pidfile

        self._recovering = True
        try:
            metadata = pidfile.read_lock_metadata()
            if not metadata or metadata.get("claimant") != CLAIMANT_DAEMON:
                return
            engine_pid = metadata.get("engine_pid") or metadata.get("pid")
            if not isinstance(engine_pid, int):
                self._clear_stale_lock_if_unheld()
                self._mark_catalog_terminated_unexpectedly(metadata.get("capture_dir"))
                await self._publish_daemon_event(
                    _stderr_events.EVENT_PREVIOUS_SESSION_RECOVERED
                )
                return
            if self._pid_exists(engine_pid):
                await self._terminate_pid(engine_pid, grace=self._reconcile_grace)
                self._mark_catalog_terminated_unexpectedly(metadata.get("capture_dir"))
                await self._publish_daemon_event(
                    _stderr_events.EVENT_PREVIOUS_SESSION_FORCE_TERMINATED
                )
            else:
                self._mark_catalog_terminated_unexpectedly(metadata.get("capture_dir"))
                await self._publish_daemon_event(
                    _stderr_events.EVENT_PREVIOUS_SESSION_RECOVERED
                )
            self._clear_stale_lock_if_unheld()
        finally:
            self._prune_stale_engine_token_files()
            self._recovering = False

    @staticmethod
    def _prune_stale_engine_token_files() -> None:
        """Delete any leftover ``engine-token-*`` files at daemon startup.

        On a clean stop ``_cleanup_engine_token_file`` removes the live token, but
        a hard crash / SIGKILL runs no Python teardown, so a 0600 file holding a
        short-lived ID token can survive in the run dir until expiry. At daemon
        startup no live recording can legitimately own one (the engine that read it
        is gone), so unlink every survivor. The glob covers both the canonical
        ``.jwt`` and a ``.jwt.tmp`` residue from a crash between write and rename
        (which also holds a live token). Best-effort: a failure is logged and never
        blocks reconciliation, mirroring ``_cleanup_engine_token_file``.
        """
        from screencap.config import get_base_dir

        run_dir = get_base_dir() / "run"
        try:
            stale = list(run_dir.glob("engine-token-*"))
        except OSError as exc:
            logger.warning("daemon: could not scan for stale engine token files: %s", exc)
            return
        for path in stale:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "daemon: could not remove stale engine token file %s: %s", path, exc
                )

    async def _stderr_pump(self, proc: _PopenEngineProcess) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        while True:
            line = await asyncio.to_thread(stderr.readline)
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8").rstrip("\n"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("malformed engine stderr line: %r", line[:200])
                continue
            if not isinstance(event, dict):
                continue
            self._observe_event(event)
            await self._bus.publish(event)

    async def _exit_poll(self, proc: _PopenEngineProcess) -> None:
        while proc.is_alive():
            await asyncio.sleep(self._poll_interval)
        await self._handle_engine_exit(proc, proc.returncode or 0)

    async def _handle_engine_exit(self, proc: _PopenEngineProcess, rc: int) -> None:
        async with self._exit_lock:
            if self._exit_handled or self._proc is not proc:
                return
            self._exit_handled = True

            task = self._stderr_task
            if task is not None and task is not asyncio.current_task() and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                except asyncio.TimeoutError:
                    task.cancel()
                except Exception:
                    logger.exception("engine stderr pump failed")

            if not self._finalized_seen:
                if rc != 0 and not self._stopping:
                    self._mark_catalog_terminated_unexpectedly(
                        (self._session_state or {}).get("capture_dir")
                    )
                    await self._publish_daemon_event(
                        _stderr_events.EVENT_ENGINE_CRASHED,
                        exit_code=rc,
                    )
                started_at = (self._session_state or {}).get(
                    "started_at", time.time()
                )
                await self._publish_daemon_event(
                    _stderr_events.EVENT_RECORDING_FINALIZED,
                    name=(self._session_state or {}).get("recording_name"),
                    duration_seconds=max(0.0, time.time() - started_at),
                    # Crash path: `force_stopped` reflects whether the engine
                    # exited non-zero. Operator-initiated stops have already
                    # delivered the engine's own finalized event, so a
                    # synthesized event here is purely the crash signal.
                    force_stopped=(rc != 0),
                    disk_full=False,
                )
                self._finalized_seen = True

            self._release_daemon_lock()
            self._reset_state()

    def _observe_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == _stderr_events.EVENT_RECORDING_FINALIZED:
            self._finalized_seen = True
        if self._session_state is None:
            return
        if event_type == _stderr_events.EVENT_CHUNK_FINALIZED:
            frames = event.get("frames_written", event.get("frame_count"))
            if isinstance(frames, int):
                self._session_state["frames_written"] = frames

    async def _wait_on_subscription(
        self,
        sub: _Subscription,
        event_type: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            event = await asyncio.wait_for(sub.queue.get(), timeout=remaining)
            if event.get("type") == event_type:
                return event

    def _verify_force_cas(
        self,
        metadata: dict[str, Any],
        *,
        claimant: str | None,
        expected_claimant_pid: int | None,
        expected_started_at: float | None,
    ) -> None:
        if expected_claimant_pid is None or expected_started_at is None:
            raise errors.NotOwnedByDaemonError(
                claimant,
                schema_version=schema._RECORDING_STOP_API_VERSION,
                hint=(
                    "force=true requires expected_claimant_pid + "
                    "expected_started_at; call session.snapshot first"
                ),
            )
        actual_pid = metadata.get("pid")
        actual_started_at = metadata.get("started_at")
        # `started_at` round-trips as a JSON float through the wire; use
        # `math.isclose` instead of `!=` so micro-rounding from the
        # serialize/deserialize hop doesn't fail an otherwise-valid CAS.
        started_at_match = (
            isinstance(actual_started_at, (int, float))
            and isinstance(expected_started_at, (int, float))
            and math.isclose(
                float(actual_started_at),
                float(expected_started_at),
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
        )
        if actual_pid != expected_claimant_pid or not started_at_match:
            raise errors.ForceMismatchError(
                schema_version=schema._RECORDING_STOP_API_VERSION
            )

    def _allocate_capture_dir(self, request: "RecordingStartRequest") -> tuple[str, Path]:
        from screencap.config import get_recordings_dir
        from screencap.daemon._name_validation import validate_recording_name

        requested_name = request.name
        requested_output = request.output_dir
        # Defense-in-depth: the ``recording.start`` HTTP handler already
        # validates ``name`` for path traversal at the request boundary,
        # but the supervisor is the engine-spawn site for any future
        # internal caller (recovery flows, MCP tools, daemon-internal
        # cron jobs). Re-validate so the gate is single-sourced.
        if requested_name is not None:
            validate_recording_name(requested_name)
        base_name = requested_name or time.strftime("rec-%Y%m%dT%H%M%S")
        if requested_output:
            capture_dir = Path(requested_output).expanduser().resolve()
            recordings_dir = get_recordings_dir().resolve()
            allowed_roots: list[Path] = [recordings_dir, *_extra_output_dir_allowlist]
            if not any(
                capture_dir == root or capture_dir.is_relative_to(root)
                for root in allowed_roots
            ):
                raise errors.InvalidOutputDirError(
                    "output_dir must be inside the recordings root",
                    schema_version=schema._RECORDING_START_API_VERSION,
                )
            return base_name, capture_dir
        recordings_dir = get_recordings_dir()
        candidate = recordings_dir / base_name
        if not candidate.exists():
            return base_name, candidate
        for index in range(2, 1000):
            name = f"{base_name}-{index}"
            candidate = recordings_dir / name
            if not candidate.exists():
                return name, candidate
        raise RuntimeError(f"could not allocate capture dir for {base_name!r}")

    def _worker_args(
        self,
        request: "RecordingStartRequest",
        *,
        name: str,
        capture_dir: Path,
    ) -> dict[str, Any]:
        return build_engine_worker_args(request, name=name, capture_dir=capture_dir)

    def _owner_payload(self) -> dict[str, Any]:
        from screencap import pidfile

        return pidfile.read_lock_metadata() or {
            "pid": os.getpid(),
            "claimant": CLAIMANT_DAEMON,
            "engine_pid": self._engine_pid,
        }

    async def _terminate_current_process(self, *, force: bool = False) -> None:
        if self._proc is None or not self._proc.is_alive():
            return
        if force:
            self._proc.kill()
        else:
            self._proc.terminate()
        try:
            rc = await self._proc.wait(timeout=2.0)
        except asyncio.TimeoutError:
            self._proc.kill()
            try:
                rc = await self._proc.wait(timeout=2.0)
            except asyncio.TimeoutError:
                # Zombie post-SIGKILL — treat as killed so callers (notably
                # `spawn()`'s failure path) still complete teardown.
                rc = -9
        await self._handle_engine_exit(self._proc, rc)

    async def _terminate_pid(self, pid: int, *, grace: float) -> bool:
        return await asyncio.to_thread(self._terminate_pid_sync, pid, grace)

    @staticmethod
    def _terminate_pid_sync(pid: int, grace: float) -> bool:
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        try:
            proc.terminate()
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if not psutil.pid_exists(pid):
                    return True
                try:
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return True
                time.sleep(0.05)
            if psutil.pid_exists(pid):
                proc.kill()
                kill_deadline = time.monotonic() + 2.0
                while time.monotonic() < kill_deadline:
                    if not psutil.pid_exists(pid):
                        return True
                    try:
                        if proc.status() == psutil.STATUS_ZOMBIE:
                            return True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        return True
                    time.sleep(0.05)
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return False

    async def _publish_daemon_event(self, event_type: str, **fields: Any) -> None:
        event = {
            "type": event_type,
            "schema_version": _stderr_events.EVENT_SCHEMA_VERSION,
            "ts": time.time(),
            **fields,
        }
        self._observe_event(event)
        await self._bus.publish(event)

    @staticmethod
    def _mark_catalog_terminated_unexpectedly(capture_dir: object) -> None:
        if not isinstance(capture_dir, str) or not capture_dir:
            return
        db_path = Path(capture_dir) / "recording.db"
        if not db_path.is_file():
            return
        try:
            from screencap.recording_db import has_column, has_table, open_recording_db

            with open_recording_db(db_path, read_only=False) as conn:
                if not has_table(conn, "recording") or not has_column(
                    conn, "recording", "config"
                ):
                    return
                row = conn.execute(
                    "SELECT id, config FROM recording ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    return
                config = row[1]
                if isinstance(config, str):
                    try:
                        config_payload = json.loads(config) if config else {}
                    except json.JSONDecodeError:
                        config_payload = {}
                elif isinstance(config, dict):
                    config_payload = dict(config)
                else:
                    config_payload = {}
                config_payload["daemon_terminal_state"] = "terminated_unexpectedly"
                conn.execute(
                    "UPDATE recording SET config=? WHERE id=?",
                    (json.dumps(config_payload), row[0]),
                )
                conn.commit()
        except Exception:
            logger.exception("failed to mark catalog row terminated_unexpectedly")

    def _release_daemon_lock(self) -> None:
        from screencap import pidfile

        try:
            pidfile.release_lock()
        finally:
            self._clear_stale_lock_if_unheld()

    @staticmethod
    def _clear_stale_lock_if_unheld() -> None:
        from screencap import pidfile

        try:
            if not pidfile.lock_is_active():
                pidfile.LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    async def _subscribe_with_replay_fallback(
        self, cursor: int, *, label: str
    ) -> _Subscription:
        """``subscribe(since=cursor)`` with a live-only fallback on eviction.

        Shared by ``stop()`` and ``shutdown()``. Replay coverage of the
        await gap is the desired path; if a burst of unrelated events
        evicted ``cursor`` from the ring between capture and this call,
        log the loss with ``label`` and fall back to a live-only
        subscription. Worst case the caller's ``_wait_on_subscription``
        hits its timeout and the force-stop path runs.
        """
        try:
            return await self._bus.subscribe(since=cursor)
        except CursorOutOfRangeError:
            logger.warning(
                "%s(): replay cursor=%d aged out before subscribe; "
                "falling back to live-only delivery",
                label,
                cursor,
            )
            return await self._bus.subscribe()

    async def _cancel_exit_poll(self) -> None:
        """Cancel the engine exit poll task before a late subscribe.

        Same shape as the cancellation block in ``spawn()``'s except arm:
        check not None and not done, cancel, then await with
        ``CancelledError`` suppressed so the helper never raises into the
        caller. No-op if the task is absent or already finished. The
        attribute is cleared on success so subsequent teardown paths
        (``_reset_state``) do not re-cancel.
        """
        task = self._poll_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._poll_task = None

    async def _stage_engine_token(
        self, request: "RecordingStartRequest", capture_dir: Path
    ) -> dict[str, str] | None:
        """Stage the out-of-band ID token for a cloud recording's engine.

        Reads the ID token in the daemon's OWN ACL context (the engine subprocess
        cannot — different Keychain ACL identity), writes it to a 0600 file, and
        returns the env overlay pointing the engine at it (via
        ``auth.ENGINE_TOKEN_FILE_ENV``). Returns ``None`` — no token, no env var —
        for local recordings and, fail-closed, whenever no token is available (not
        signed in / Keychain error / transient auth failure): the engine then gets
        no token, the live upload fails closed (chunks FAILED, nothing deleted),
        and the recording still proceeds locally. Never raises.
        """
        if not request.cloud_intent:
            return None
        from screencap import auth

        try:
            token = await asyncio.to_thread(auth.get_id_token)
        except Exception as exc:  # noqa: BLE001 — NotSignedIn/AuthError/KeyringError: fail closed
            logger.warning(
                "daemon: no cloud auth token at recording start (%s); live upload "
                "will fail closed (recording stays local)",
                type(exc).__name__,
            )
            return None
        path = self._engine_token_path(capture_dir)
        try:
            self._write_engine_token_file(path, token)
        except OSError as exc:
            logger.warning("daemon: could not stage engine token file: %s", exc)
            return None
        self._engine_token_file = path
        return {auth.ENGINE_TOKEN_FILE_ENV: str(path)}

    @staticmethod
    def _engine_token_path(capture_dir: Path) -> Path:
        from screencap.config import get_base_dir

        run_dir = get_base_dir() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"engine-token-{capture_dir.name}.jwt"

    @staticmethod
    def _write_engine_token_file(path: Path, token: str) -> None:
        """Atomically write *token* to *path* with mode 0600 (same-EUID only)."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        # O_NOFOLLOW: reject a pre-planted symlink at the tmp path so a same-EUID
        # actor can't redirect the live token write — matches the run-dir 0600
        # hardening convention in audit_log.py / socket.py.
        fd = os.open(
            str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(fd, token.encode("ascii"))
        finally:
            os.close(fd)
        try:
            os.replace(str(tmp), str(path))
            os.chmod(path, 0o600)  # re-assert in case the file pre-existed wider
        except OSError:
            # The .tmp holds a live ID token; _cleanup_engine_token_file only
            # removes the canonical .jwt, so unlink the residue before re-raising
            # rather than leaking a same-EUID-readable token in the run dir.
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise

    def _cleanup_engine_token_file(self) -> None:
        path = self._engine_token_file
        self._engine_token_file = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("daemon: could not remove engine token file %s: %s", path, exc)

    async def _token_refresh_loop(self, proc: "_PopenEngineProcess") -> None:
        """Re-mint + rewrite the engine token file while *proc* is alive.

        A live recording can outlast the ~1h ID token, and the engine holds no
        refresh token. The daemon (which does) re-mints on a timer and rewrites the
        0600 file in place; the engine reads it fresh on each cloud call. A re-mint
        failure is logged but never aborts the recording — a stale token just makes
        the engine fail closed (no delete) on the next 401. Self-terminates when the
        engine exits.
        """
        interval = _float_env("SCREENCAP_DAEMON_TOKEN_REFRESH_INTERVAL", 240.0)
        from screencap import auth

        last: str | None = None
        while self._proc is proc and proc.is_alive():
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            path = self._engine_token_file
            if self._proc is not proc or not proc.is_alive() or path is None:
                return
            try:
                token = await asyncio.to_thread(auth.get_id_token)
            except Exception as exc:  # noqa: BLE001 — never abort the recording
                logger.warning(
                    "daemon: token re-mint failed (%s); engine will fail closed on expiry",
                    type(exc).__name__,
                )
                continue
            if token == last:
                continue  # unchanged — not yet within the refresh buffer
            try:
                self._write_engine_token_file(path, token)
                last = token
            except OSError as exc:
                logger.warning("daemon: token re-mint rewrite failed: %s", exc)

    def _reset_state(self) -> None:
        self._proc = None
        self._engine_pid = None
        self._session_state = None
        self._stderr_task = None
        self._poll_task = None
        if self._token_refresh_task is not None and not self._token_refresh_task.done():
            self._token_refresh_task.cancel()
        self._token_refresh_task = None
        self._cleanup_engine_token_file()
        self._finalized_seen = False
        self._stopping = False
        self._exit_handled = False


__all__ = ["Supervisor"]
