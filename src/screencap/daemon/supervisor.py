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
    from screencap.terminal_stage import TerminalResult

logger = logging.getLogger(__name__)

EngineCommandFactory = Callable[[str], list[str]]

# Bound the best-effort ``whoami`` enrichment in ``_maybe_emit_account_mismatch``
# so a slow Keychain/token-refresh I/O can't pace the serial startup sweep (each
# resume is awaited inline). On timeout the enrichment degrades to null/False
# while the gate-authoritative ``signed_in_uid`` still publishes.
_WHOAMI_ENRICH_TIMEOUT_S = 5.0

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


def _is_cloud_recording(recording_dir: Path) -> bool:
    """True iff ``recording_dir``'s frozen intent routes to the cloud (U6).

    Used to skip the post-exit terminal-stage resume for ``local`` / legacy
    recordings (they have nothing to upload). Best-effort: a missing/unreadable
    intent reads as non-cloud (conservative — no spurious resume).
    """
    try:
        from screencap.catalog import read_intent

        return read_intent(recording_dir) in ("cloud", "both")
    except Exception:  # noqa: BLE001
        return False


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
    # Search U8 / KTD5: resolve the stills-capture gate here (the defaults-resolution
    # seam). An unset ``capture_images`` follows the default-on readiness gate; an
    # explicit true is clamped OFF when encryption is required but not ready; an
    # explicit false always wins. The encryption flag itself rides the engine env
    # (see ``Supervisor._stage_corpus_key``), so only the on/off decision lands here.
    from screencap.capture_gate import gather_and_resolve

    gate = gather_and_resolve(
        explicit_capture_images=request.capture_images,
        scrub_enabled=request.scrub_enabled,
    )
    if gate.reason != "explicit_true":
        logger.info("capture gate: images=%s (%s)", gate.capture_images, gate.reason)
    args["capture_images"] = gate.capture_images
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
        # E2EE slice: the out-of-band cloud-key file staged for the engine (the
        # engine can't read the Keychain). Static — no refresh loop — and unlinked
        # in _reset_state so the master key never outlives the recording.
        self._engine_cloud_key_file: Path | None = None
        # SCR-116: the uid the staged token belonged to. The re-mint loop refuses
        # to restage a token whose uid differs (a mid-recording account switch),
        # so every chunk of this recording stays in the original namespace.
        self._engine_token_uid: str | None = None
        self._token_refresh_task: asyncio.Task[None] | None = None
        # SCR-125 U6: in-flight crash/restart terminal-stage resume tasks (the
        # engine-exit safety net + the startup sweep). Tracked as a set so the
        # idle-shutdown watchdog (``has_inflight_resume``) never tears the daemon
        # down mid-upload, and ``shutdown`` can await/cancel them. Resumes outlive
        # a single recording session, so ``_reset_state`` does NOT clear this.
        self._resume_tasks: set[asyncio.Task[Any]] = set()
        self._recovering = False
        self._finalized_seen = False
        self._stopping = False
        self._exit_handled = False
        self._operation_lock = asyncio.Lock()
        self._exit_lock = asyncio.Lock()
        # SCR-228: set while a storage-location migration holds the daemon.
        # Guarded by `_operation_lock` so it serializes against spawn/stop.
        self._migration_active = False

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

    async def acquire_migration(self, *, schema_version: int) -> None:
        """Reserve the daemon for a storage-location migration (SCR-228 U3/U4).

        Under ``_operation_lock`` so it serializes against ``spawn``/``stop``:
        refuses if a daemon-owned recording is in progress, a terminal-stage
        resume is in flight, or a migration is already active. On success sets
        ``_migration_active`` (which ``spawn`` checks under the same lock, so a
        recording.start during the migration is refused). The caller MUST pair
        this with :meth:`release_migration` in a ``finally``.
        """
        async with self._operation_lock:
            if self._migration_active:
                raise errors.StorageMigrationError(
                    "migration_in_progress",
                    "A storage migration is already in progress.",
                    schema_version=schema_version,
                )
            if self._proc is not None and self._proc.is_alive():
                raise errors.StorageMigrationError(
                    "recording_active",
                    "Stop the current recording before moving the "
                    "storage location.",
                    schema_version=schema_version,
                )
            if self.has_inflight_resume():
                raise errors.StorageMigrationError(
                    "recording_active",
                    "A recording is still finishing processing. Try again "
                    "once it completes.",
                    schema_version=schema_version,
                )
            self._migration_active = True

    def release_migration(self) -> None:
        """Clear the migration reservation (paired with acquire_migration)."""
        self._migration_active = False

    def is_migrating(self) -> bool:
        """True while a storage migration holds the daemon (SCR-228).

        Consulted by the idle-shutdown watchdog: ``/v0/storage.migrate`` is not
        in ``_ACTIVITY_PATHS`` and its handler yields the event loop across an
        ``asyncio.to_thread`` move, so without this an auto-spawned daemon could
        idle-exit mid-migration.
        """
        return self._migration_active

    async def spawn(self, request: "RecordingStartRequest") -> dict[str, Any]:
        """Claim the daemon lock, spawn the engine worker, and await started."""
        async with self._operation_lock:
            if self._migration_active:
                # SCR-228 U4: a storage migration holds the daemon; refuse to
                # start a recording rather than let it write into a directory
                # that is being relocated out from under it.
                raise errors.MigrationInProgressError(
                    schema_version=schema._RECORDING_START_API_VERSION
                )
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
                key_env = await self._stage_engine_cloud_key(request, capture_dir)
                if key_env:
                    extra_env = {**(extra_env or {}), **key_env}
                # Search U8: deliver the corpus key (via file) + RECORD_IMAGES_ENCRYPTED
                # to the engine when the gate resolved encrypted stills ON.
                corpus_env = await self._stage_corpus_key(request, capture_dir)
                if corpus_env:
                    extra_env = {**(extra_env or {}), **corpus_env}
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

    async def resume_terminal_stage(self, recording_dir: "Path | str") -> Any:
        """Resume incomplete terminal-stage work for one recording (SCR-125 U6).

        The daemon-restart / crash resume entry point. A daemon that restarts
        while a cloud recording's terminal-stage work (scrub → upload →
        sentinel) was incomplete — OR that funnels an engine exit through
        ``_handle_engine_exit`` — drives the SINGLE disk-driven terminal stage
        from disk, behind the per-recording flock, converging without
        duplicating or re-uploading already-confirmed chunks (R9/AE2). It is the
        PRIMARY uploader for the degraded path where the live upload failed all
        session and finalize deferred the backlog (U4).

        Run in a worker thread (the terminal stage is blocking PyAV/network
        work that must not stall the asyncio loop) and in ``non_blocking`` mode:
        if a live engine's own ``finalize_uploads`` (or a manual ``screencap
        upload``) currently holds the terminal flock, this resume SKIPS rather
        than racing — the holder owns the critical section (AE12).

        **Fails closed (research H4).** Auth (``NotSignedIn`` / ``AuthError``),
        upload, and promotion (``PromotionRefused``) errors are swallowed and
        return ``None`` — never propagating into the asyncio loop, never evicting,
        never writing a sentinel. ``run_terminal_stage`` already catches its own
        upload failures (a result with ``upload_warning``); this is the outer
        belt for anything that escapes (e.g. AE8 ``PromotionRefused``).
        """
        import asyncio as _asyncio

        from screencap import auth
        from screencap.terminal_stage import (
            PromotionRefused,
            TerminalStageBusy,
            run_terminal_stage,
        )

        def _run() -> Any:
            try:
                return run_terminal_stage(Path(recording_dir), non_blocking=True)
            except TerminalStageBusy:
                logger.debug(
                    "resume_terminal_stage: %s busy (held by live finalize "
                    "or manual upload); skipping", recording_dir,
                )
                return None
            except PromotionRefused as exc:
                logger.warning(
                    "resume_terminal_stage: %s has holes (%s); fail-closed, "
                    "nothing evicted", recording_dir, exc,
                )
                return None
            except (auth.NotSignedIn, auth.AuthError) as exc:
                logger.warning(
                    "resume_terminal_stage: cloud auth unavailable for %s (%s); "
                    "fail-closed (recording preserved on disk)",
                    recording_dir, type(exc).__name__,
                )
                return None
            except Exception as exc:  # noqa: BLE001 — never propagate into the loop
                logger.warning(
                    "resume_terminal_stage: convergence failed for %s (%s); "
                    "fail-closed, nothing evicted", recording_dir, exc,
                )
                return None

        result = await _asyncio.to_thread(_run)
        # SCR-171: lift an account-ownership-mismatch refusal onto /v0/events. This
        # is the single emit point for ALL daemon detection paths — startup sweep,
        # crash/restart resume, and the post-engine-exit resume funneled here by
        # ``_handle_engine_exit`` — since every one of them lands in this method.
        await self._maybe_emit_account_mismatch(result, Path(recording_dir).name)
        return result

    async def _maybe_emit_account_mismatch(
        self, result: "TerminalResult | None", recording_name: str
    ) -> None:
        """Publish an advisory ``account_mismatch`` /v0/events event (SCR-171).

        Driven off the TYPED ``TerminalResult.account_mismatch`` field, never the
        overloaded ``upload_warning`` (scrub/upload failures also set that), so a
        false event is never emitted. ``signed_in_uid`` is gate-authoritative;
        ``signed_in_email`` / ``stale`` are best-effort enrichment from ``whoami``
        read OFF-loop (it does blocking Keychain + token-refresh I/O) a beat later,
        bounded by a short timeout so a slow token refresh can't pace the serial
        startup sweep — on timeout the enrichment degrades (email null / stale
        False) while the gate-authoritative ``signed_in_uid`` still publishes.
        ``whoami`` omits ``stale`` on success, so it is normalized to ``False``.
        Fail-open: a whoami/publish failure is logged and swallowed so an advisory
        emit never breaks a resume.
        """
        mismatch = result.account_mismatch if result is not None else None
        if mismatch is None:
            return
        try:
            from screencap import auth

            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(auth.whoami),
                    timeout=_WHOAMI_ENRICH_TIMEOUT_S,
                )
            except (TimeoutError, asyncio.TimeoutError):
                # Slow token refresh: degrade email/stale enrichment; the
                # gate-authoritative signed_in_uid below is unaffected.
                info = {}
            await self._publish_daemon_event(
                _stderr_events.EVENT_ACCOUNT_MISMATCH,
                recording=recording_name,
                owner_uid=mismatch.owner_uid,
                signed_in_uid=mismatch.signed_in_uid,
                signed_in_email=info.get("email"),
                stale=info.get("stale", False),
            )
        except Exception:  # noqa: BLE001 — advisory emit must never break a resume
            logger.warning(
                "resume_terminal_stage: account_mismatch emit failed for %s",
                recording_name,
                exc_info=True,
            )

    def has_inflight_resume(self) -> bool:
        """True while any crash/restart resume worker is in flight (U6).

        Consulted by the idle-shutdown watchdog so an auto-spawned daemon never
        tears itself down mid-upload: ``current_session()`` is ``None`` after the
        engine exits, so without this the resume would be invisible.
        """
        return any(not t.done() for t in self._resume_tasks)

    def _track_resume(self, coro: Any) -> "asyncio.Task[Any]":
        """Schedule a resume coroutine as a tracked, self-pruning background task."""
        task = asyncio.create_task(coro)
        self._resume_tasks.add(task)
        task.add_done_callback(self._resume_tasks.discard)
        return task

    async def _run_resume_safely(self, recording_dir: Path) -> None:
        """Await one resume, swallowing anything that escapes (belt-and-braces)."""
        try:
            await self.resume_terminal_stage(recording_dir)
        except Exception:  # noqa: BLE001 — a resume must never crash the daemon
            logger.exception(
                "daemon resume task crashed for %s (fail-closed)", recording_dir
            )

    async def _run_startup_sweep(self) -> None:
        """Resume every cloud recording left incomplete at daemon startup (F3).

        Scans the recordings dir for recordings with an ENGINE-ORIGIN frozen
        ``chunks_expected`` and an UNSATISFIED finalize gate, and resumes each.
        It NEVER freezes-from-disk (no ``reconcile_ledger_from_disk``): a
        crash-before-finalize recording has no authoritative count, so it is not
        auto-converted to "complete" — its chunks may upload but it never gets a
        false sentinel; full recovery is via manual ``screencap upload``.

        Auth pre-flight: if not signed in, the sweep is SKIPPED entirely (the
        recordings stay on disk for a later daemon start / sign-in) — it never
        evicts or partially-converges without auth.
        """
        from screencap import auth
        from screencap.config import get_recordings_dir

        try:
            await asyncio.to_thread(auth.get_id_token)
        except Exception as exc:  # noqa: BLE001 — NotSignedIn / AuthError / Keychain
            logger.info(
                "daemon startup sweep: not signed in (%s); skipping "
                "(incomplete recordings preserved for a later attempt)",
                type(exc).__name__,
            )
            return

        try:
            recordings_dir = get_recordings_dir()
            # Skip ``<name>-scrubbed`` cloud-copy siblings: scrub_recording
            # copies recording.db (hence the ledger + a cloud .recording_intent +
            # the source's .recording_id) into them, so a scrubbed sibling looks
            # like an incomplete cloud recording (frozen chunks_expected, gate
            # unsatisfied — UPLOADED marks land on the SOURCE ledger). Resuming
            # one would upload under the source's GCS key behind a DIFFERENT flock
            # (defeating AE12) and nest a ``<name>-scrubbed-scrubbed``. Only true
            # source recordings are resume candidates.
            candidates = sorted(
                d for d in recordings_dir.iterdir()
                if d.is_dir() and not d.name.endswith("-scrubbed")
            )
        except OSError as exc:
            logger.warning("daemon startup sweep: could not scan recordings dir: %s", exc)
            return

        swept: list[str] = []
        refused: list[str] = []
        for d in candidates:
            try:
                if await asyncio.to_thread(self._recording_needs_resume, d):
                    result = await self.resume_terminal_stage(d)
                    # SCR-116 observability: a result carrying an upload_warning
                    # (e.g. account-ownership mismatch) did NOT converge — surface
                    # it at WARNING and track it apart from the truly-swept count.
                    if result is not None and result.upload_warning:
                        refused.append(d.name)
                        logger.warning(
                            "daemon startup sweep: %s not converged (%s)",
                            d.name, result.upload_warning,
                        )
                    else:
                        swept.append(d.name)
            except Exception:  # noqa: BLE001 — one bad recording never aborts the sweep
                logger.exception("daemon startup sweep: resume failed for %s", d)
        if swept:
            logger.info(
                "daemon startup sweep resumed %d incomplete recording(s): %s",
                len(swept), swept,
            )
        if refused:
            logger.warning(
                "daemon startup sweep: %d recording(s) refused convergence: %s",
                len(refused), refused,
            )

    @staticmethod
    def _recording_needs_resume(recording_dir: Path) -> bool:
        """True iff a cloud recording is ENGINE-frozen but not finalize-complete.

        Read-only: must NOT migrate the ledger schema (that would mutate
        recording.db and defeat scrubbed-copy reuse) and must NOT freeze-from-disk
        (a crash-before-finalize recording has no authoritative count — it is left
        for manual recovery, never auto-completed).
        """
        from screencap.terminal_stage import _open_ledger_readonly

        if not _is_cloud_recording(recording_dir):
            return False  # local / legacy / no-intent → not a cloud resume.
        ledger = _open_ledger_readonly(recording_dir)
        if ledger is None:
            return False  # no engine-origin ledger (legacy / not seeded).
        try:
            expected = ledger.chunks_expected()
            if not expected:
                return False  # crash-before-freeze → no authoritative count.
            return not ledger.finalize_gate_satisfied()
        except Exception:  # noqa: BLE001 — unreadable ledger → conservative skip
            return False

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
        # SCR-125 U6: in-flight resume workers — give them a brief grace to
        # finish (a converged recording's resume is a cheap no-op), then cancel.
        # A cancelled mid-upload resume is safe: the terminal stage is idempotent
        # and the recording stays on disk for the next daemon start / manual upload.
        inflight = [t for t in self._resume_tasks if not t.done()]
        if inflight:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*inflight, return_exceptions=True), timeout=5.0,
                )
            except asyncio.TimeoutError:
                for t in inflight:
                    t.cancel()
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
            self._prune_stale_engine_cloud_key_files()
            self._recovering = False
            # SCR-125 U6 F3 startup sweep — resume cloud recordings left
            # incomplete by a prior daemon/engine crash. Detached (tracked) so it
            # never blocks request handling: _recovering is already cleared, and a
            # long backlog upload must not make `spawn`/`stop` raise Reconciling.
            self._track_resume(self._run_startup_sweep())

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

    @staticmethod
    def _prune_stale_engine_cloud_key_files() -> None:
        """Delete any leftover ``engine-cloud-key-*`` files at daemon startup.

        A hard crash / SIGKILL runs no Python teardown, so a 0600 file holding a
        cloud master key can survive in the run dir. At daemon startup no live
        recording can legitimately own one (the engine that read it is gone), so
        unlink every survivor — a master key must not linger. Best-effort.
        """
        from screencap.config import get_base_dir

        run_dir = get_base_dir() / "run"
        try:
            stale = list(run_dir.glob("engine-cloud-key-*"))
        except OSError as exc:
            logger.warning("daemon: could not scan for stale cloud-key files: %s", exc)
            return
        for path in stale:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "daemon: could not remove stale cloud-key file %s: %s", path, exc
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

            # SCR-125 U6: snapshot capture_dir HERE, inside _exit_lock and BEFORE
            # _reset_state clears _session_state, so the post-exit resume always
            # has the recording dir even if a concurrent stop/shutdown raced.
            capture_dir = (self._session_state or {}).get("capture_dir")

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

            # SCR-125 U6: fire the crash/restart safety-net resume EXACTLY ONCE
            # from this single exit funnel (graceful stop, crash, SystemExit) —
            # not from a recording_finalized subscription (which would race the
            # late listener). On a graceful stop the engine's own finalize already
            # converged, so this is a cheap reconcile/no-op (or a non_blocking skip
            # if the engine still holds the flock); on the degraded/crash path it
            # is the PRIMARY uploader. It runs detached so it never blocks the exit
            # funnel, and is tracked so idle-shutdown waits for it. Only CLOUD
            # recordings need cloud convergence — a local recording has nothing to
            # upload, so we skip the resume for it entirely.
            if (
                isinstance(capture_dir, str)
                and capture_dir
                and _is_cloud_recording(Path(capture_dir))
            ):
                self._track_resume(self._run_resume_safely(Path(capture_dir)))

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

    async def _stage_corpus_key(
        self, request: "RecordingStartRequest", capture_dir: Path
    ) -> dict[str, str] | None:
        """Stage the corpus key for an ENCRYPTED-stills recording's engine (search U8).

        Re-resolves the capture gate (a pure config read, so it agrees with
        ``build_engine_worker_args``); when stills are on AND encrypted, writes the
        corpus key to a 0600 file and returns the env overlay pointing the engine at
        it (``corpus_crypto.CORPUS_KEY_FILE_ENV``) plus ``RECORD_IMAGES_ENCRYPTED=1``.
        The key bytes go via the FILE, never argv/config (``ps``-visible), mirroring
        the ID-token channel. Returns ``None`` when stills are off or plaintext.
        Fail-closed: if the key can't be staged, returns ``None`` so the engine gets
        no key — the writer then never half-writes plaintext (U2 guard)."""
        from screencap.capture_gate import gather_and_resolve

        gate = gather_and_resolve(
            explicit_capture_images=request.capture_images,
            scrub_enabled=request.scrub_enabled,
        )
        if not gate.capture_images_encrypted:
            return None
        from screencap import corpus_crypto

        try:
            key = await asyncio.to_thread(corpus_crypto.load_corpus_key)
        except Exception as exc:  # noqa: BLE001 — fail closed on any key error
            logger.warning("daemon: could not load corpus key at recording start (%s)", type(exc).__name__)
            return None
        if key is None:
            logger.warning("daemon: corpus key absent at recording start; stills will be off")
            return None
        from screencap import config

        path = config.get_base_dir() / "run" / "corpus.key"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(corpus_crypto._write_key_file, str(path), key)
        except OSError as exc:
            logger.warning("daemon: could not stage corpus key file: %s", exc)
            return None
        return {corpus_crypto.CORPUS_KEY_FILE_ENV: str(path), "RECORD_IMAGES_ENCRYPTED": "1"}

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
            # U13: force a re-mint at recording start so a recording begun right
            # after checkout carries the just-granted ``subscribed`` claim, rather
            # than the daemon's cached pre-claim token (which an enforced signer
            # would refuse for up to ~1h). The daemon holds the refresh token, so
            # force_refresh works here (the engine, which does not, cannot).
            token = await asyncio.to_thread(auth.get_id_token, force_refresh=True)
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
        # SCR-116: pin the recording to this token's account. The uid is kept in
        # memory for the re-mint guard and persisted into the recording dir so the
        # terminal stage (which may run in a daemon/CLI process that reads the
        # Keychain, not the engine token) can refuse to converge under a different
        # account. A missing uid (malformed token) or write error just leaves the
        # recording unpinned — degrades to prior behavior, never fails the start.
        uid = auth.id_token_uid(token)
        self._engine_token_uid = uid
        if uid:
            try:
                from screencap.catalog import write_owner_uid

                # The engine creates capture_dir at startup (mkdir exist_ok), but
                # it does not exist yet at stage time — create it so the pin lands.
                capture_dir.mkdir(parents=True, exist_ok=True)
                write_owner_uid(capture_dir, uid)
            except (OSError, ImportError) as exc:
                logger.warning("daemon: could not pin recording owner uid: %s", exc)
                # Degrade the in-memory re-mint guard and the on-disk pin
                # consistently: if the disk pin didn't land, the terminal stage
                # sees NO pin (read_owner_uid -> None) and won't gate, so the
                # guard must not stay armed alone — otherwise the two halves
                # disagree and the terminal stage could converge under a
                # different account. Both unpinned = prior (unpinned) behavior.
                self._engine_token_uid = None
        return {auth.ENGINE_TOKEN_FILE_ENV: str(path)}

    @staticmethod
    def _engine_token_path(capture_dir: Path) -> Path:
        from screencap.config import get_base_dir

        run_dir = get_base_dir() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"engine-token-{capture_dir.name}.jwt"

    @staticmethod
    def _atomic_write_secret_file(path: Path, payload: bytes) -> None:
        """Atomically write *payload* to *path* at mode 0600 (same-EUID only).

        ``O_NOFOLLOW`` rejects a pre-planted symlink at the tmp path so a
        same-EUID actor can't redirect the write (matches the run-dir 0600
        hardening in audit_log.py / socket.py); a failed replace unlinks the tmp
        so no readable secret residue is left in the run dir. Single audited
        implementation shared by the engine token and cloud-key writers.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(
            str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        try:
            os.replace(str(tmp), str(path))
            os.chmod(path, 0o600)  # re-assert in case the file pre-existed wider
        except OSError:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise

    @staticmethod
    def _write_engine_token_file(path: Path, token: str) -> None:
        """Atomically write *token* to *path* with mode 0600 (same-EUID only)."""
        Supervisor._atomic_write_secret_file(path, token.encode("ascii"))

    def _cleanup_engine_token_file(self) -> None:
        path = self._engine_token_file
        self._engine_token_file = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("daemon: could not remove engine token file %s: %s", path, exc)

    async def _stage_engine_cloud_key(
        self, request: "RecordingStartRequest", capture_dir: Path
    ) -> dict[str, str] | None:
        """Stage the out-of-band cloud E2EE key for a cloud recording's engine.

        Reads the device-held cloud key in the daemon's OWN ACL context (the
        engine subprocess cannot read the Keychain), writes it to a hardened 0600
        file, and returns the env overlay pointing the engine at it. Returns
        ``None`` — no key, no env var — for local recordings, when the flag is
        off, or (fail-closed) when no key is available. The engine then encrypts
        nothing, and under the flag its U2 path fails the upload closed rather
        than shipping plaintext. Static key: no refresh loop, unlike the token.
        Never raises.
        """
        if not request.cloud_intent:
            return None
        from screencap import cloud_crypto
        from screencap.config import get_cloud_e2ee_enabled

        if not get_cloud_e2ee_enabled():
            return None
        try:
            key = await asyncio.to_thread(cloud_crypto.get_cloud_kek)
        except Exception as exc:  # noqa: BLE001 — KeyringError etc.: fail closed
            logger.warning(
                "daemon: no cloud E2EE key at recording start (%s); live upload "
                "fails closed under the flag (recording stays local)",
                type(exc).__name__,
            )
            return None
        if key is None:
            logger.warning(
                "daemon: cloud E2EE flag on but no key present; live upload fails "
                "closed (sign in to create the key)"
            )
            return None
        path = self._engine_cloud_key_path(capture_dir)
        try:
            self._write_engine_cloud_key_file(path, key)
        except OSError as exc:
            logger.warning("daemon: could not stage engine cloud-key file: %s", exc)
            return None
        self._engine_cloud_key_file = path
        return {cloud_crypto.ENGINE_CLOUD_KEY_FILE_ENV: str(path)}

    @staticmethod
    def _engine_cloud_key_path(capture_dir: Path) -> Path:
        from screencap.config import get_base_dir

        run_dir = get_base_dir() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"engine-cloud-key-{capture_dir.name}.b64"

    @staticmethod
    def _write_engine_cloud_key_file(path: Path, key: bytes) -> None:
        """Atomically write the base64 cloud key to *path* at mode 0600.

        A long-lived master key warrants at least the same hardening as the
        short-lived token, so both go through ``_atomic_write_secret_file``.
        """
        Supervisor._atomic_write_secret_file(path, base64.b64encode(key))

    def _cleanup_engine_cloud_key_file(self) -> None:
        path = self._engine_cloud_key_file
        self._engine_cloud_key_file = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "daemon: could not remove engine cloud-key file %s: %s", path, exc
            )

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
            # SCR-116: refuse to restage a token for a DIFFERENT account. The user
            # ran `logout && login` as another user mid-recording, so the Keychain
            # (which _ensure_fresh prefers on refresh) now rotates to their uid.
            # Writing it would make the engine upload subsequent chunks into the
            # new user's namespace, fragmenting the recording. Skip the write (do
            # NOT update `last`) so the engine keeps account A's token and fails
            # closed on expiry; if the user switches back to A we resume re-minting.
            # Mirror the terminal-stage gate's tolerance: only refuse on a
            # DETERMINED, DIFFERENT uid. A None extraction (malformed token, uid
            # not present) is undeterminable, not a switch — fall through to the
            # normal restage rather than wrongly refusing a same-account re-mint.
            minted_uid = auth.id_token_uid(token)
            if (
                self._engine_token_uid is not None
                and minted_uid is not None
                and minted_uid != self._engine_token_uid
            ):
                logger.warning(
                    "daemon: re-minted token uid changed (account switch "
                    "mid-recording) — refusing to restage; engine keeps the "
                    "original account's token and fails closed on expiry"
                )
                continue
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
        self._cleanup_engine_cloud_key_file()
        self._engine_token_uid = None
        self._finalized_seen = False
        self._stopping = False
        self._exit_handled = False


__all__ = ["Supervisor"]
