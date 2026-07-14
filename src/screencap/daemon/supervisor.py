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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from screencap import _stderr_events
from screencap.daemon import errors, schema
from screencap.daemon.event_bus import CursorOutOfRangeError, EventBus, _Subscription
from screencap.pidfile import CLAIMANT_DAEMON

if TYPE_CHECKING:
    from screencap.capture_gate import CaptureGateResult
    from screencap.daemon.schema import RecordingStartRequest
    from screencap.terminal_stage import TerminalResult

logger = logging.getLogger(__name__)

EngineCommandFactory = Callable[[str], list[str]]

# SCR-214 U2/KTD8: the shared ``recording.start`` gate (permission + paywall),
# injected by the daemon app so the internal ambient auto-start runs the exact
# same checks as the HTTP path. Awaitable, raises the same typed errors.
StartGate = Callable[[], Awaitable[None]]

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
# Upper bound on a single engine stdin control-line write (SCR-254 polish). A
# healthy write is instantaneous; this only fires if the engine's stdin reader
# stalled and the pipe buffer filled, so it errs generous to avoid false trips.
_SEND_COMMAND_TIMEOUT_S = 5.0


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
            # SCR-218 U1: stdin is the daemon->engine control channel (was
            # DEVNULL). The daemon writes NDJSON command lines here via
            # ``send_line``; the engine reads them in ``engine.control_channel``.
            stdin=subprocess.PIPE,
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

    def send_line(self, line: str) -> None:
        """Write one already-newline-terminated command line to the engine's
        stdin control channel (SCR-218 U1). Raises on a closed/broken pipe; the
        caller (``Supervisor.send_command``) degrades that to a False result."""
        stdin = self._popen.stdin
        if stdin is None:
            raise BrokenPipeError("engine stdin is not a pipe")
        stdin.write(line.encode("utf-8"))
        stdin.flush()

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


def _ambient_rearm_params() -> tuple[float, float, int, float]:
    """Backoff/ceiling knobs for ambient re-arm (SCR-214 U2), env-overridable.

    Returns ``(base_delay, max_delay, max_retries, quiescence)``:

    * ``base_delay`` — first backoff after a rapid crash; doubles each retry.
    * ``max_delay`` — cap on the exponential backoff.
    * ``max_retries`` — consecutive rapid-failure ceiling; past it we surface a
      degraded state instead of thrash-respawning.
    * ``quiescence`` — a run lasting at least this long (or a clean rc==0 exit)
      is treated as stable: it resets the crash budget and re-arms promptly.

    Read fresh each time so tests can shrink the delays / ceiling via env.
    """
    base = _float_env("SCREENCAP_AMBIENT_REARM_BASE_DELAY", 2.0)
    max_delay = _float_env("SCREENCAP_AMBIENT_REARM_MAX_DELAY", 300.0)
    max_retries = int(_float_env("SCREENCAP_AMBIENT_REARM_MAX_RETRIES", 5))
    quiescence = _float_env("SCREENCAP_AMBIENT_REARM_QUIESCENCE", 60.0)
    return base, max_delay, max_retries, quiescence


def _is_yyyymmdd(token: str) -> bool:
    """Whether ``token`` is an 8-digit ``YYYYMMDD`` day stamp (SCR-214 U3)."""
    return len(token) == 8 and token.isdigit()


def _ambient_day_name(clock: Callable[[], float]) -> str:
    """``ambient-YYYYMMDD`` for the LOCAL day of ``clock()`` (SCR-214 U3).

    A module-level function (not just a method) so the deterministic per-day dir
    naming and the day-roll change-detection share ONE definition of "what local
    day is it", and so :meth:`Supervisor._allocate_ambient_capture_dir` can call it
    with a graceful ``time.time`` fallback (the U1 ``_AllocOnly`` test double binds
    only the alloc methods, with no ``_clock`` instance state). ``time.localtime``
    collapses any DST / timezone shift to one unambiguous local day.
    """
    return time.strftime("ambient-%Y%m%d", time.localtime(clock()))


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
    gate: "CaptureGateResult | None" = None,
) -> dict[str, Any]:
    """Build the arg dict the engine worker is dispatched with.

    A ``RecordingStartRequest`` model dump plus the ``name`` / ``output_dir`` /
    ``capture_dir_hint`` the supervisor injects. The ``_*_q`` queue keys are
    created downstream by the engine worker command itself, so they are
    intentionally absent here. Shared with the dispatch integration test so the
    arg shape stays in lockstep with production.

    ``gate`` is the ONE capture-gate resolution for this spawn: ``Supervisor.spawn``
    resolves it once and threads the SAME result into both this function (the
    on/off decision) and ``_stage_corpus_key`` (the key/encryption decision), so the
    two can never diverge and leave stills on with encryption off (plaintext). When
    called without a gate (direct test / non-spawn callers) it resolves its own.
    """
    args = request.model_dump()
    args["name"] = name
    args["output_dir"] = str(capture_dir)
    args["capture_dir_hint"] = str(capture_dir)
    # Search U8 / KTD5: resolve the stills-capture gate (the defaults-resolution
    # seam). An unset ``capture_images`` follows the default-on readiness gate; an
    # explicit true is clamped OFF when encryption is required but not ready; an
    # explicit false always wins. The encryption flag itself rides the engine env
    # (see ``Supervisor._stage_corpus_key``), so only the on/off decision lands here.
    if gate is None:
        from screencap.capture_gate import gather_and_resolve

        gate = gather_and_resolve(
            explicit_capture_images=request.capture_images,
            scrub_enabled=request.scrub_enabled,
        )
    if gate.reason != "explicit_true":
        logger.info("capture gate: images=%s (%s)", gate.capture_images, gate.reason)
    args["capture_images"] = gate.capture_images
    return args


def _ambient_dir_finalized(candidate: Path) -> bool:
    """Whether an existing ``ambient-YYYYMMDD`` dir has already been finalized.

    Reopening a finalized day and appending fresh chunks would push past the
    frozen closed set (``chunks_expected``) and corrupt the ledger, so
    :meth:`Supervisor._allocate_ambient_capture_dir` must NOT reopen one
    (SCR-214 U1). A day is finalized when either:

    * the terminal stage wrote the completeness sentinel
      (``recording_complete.json`` — its last write), or
    * ``recording.db``'s ``recording.chunks_expected`` is frozen (non-null),
      which the final chunk rotation / terminal stage sets when the recording
      closes. A local ambient recording never writes the cloud sentinel, so this
      ledger check is the load-bearing signal for the common local case.

    Fail-safe: a missing/unreadable DB, or a pre-U1 DB without the column, reads
    as *not finalized* (the dir is treated as reopenable) — a fresh same-day dir
    is only ever forked when finalization is positively proven.
    """
    if (candidate / "recording_complete.json").exists():
        return True
    db = candidate / "recording.db"
    if not db.exists():
        return False
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT chunks_expected FROM recording LIMIT 1"
        ).fetchone()
        return row is not None and row[0] is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


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
        start_gate: StartGate | None = None,
        clock: Callable[[], float] | None = None,
        ambient_day_tick_interval: float | None = None,
        ambient_seg_tick_interval: float | None = None,
    ) -> None:
        self._bus = event_bus
        self._engine_command_factory = engine_command_factory or _default_engine_command
        # SCR-214 U3: injectable wall-clock so the day-boundary roll can be
        # exercised deterministically (a test can cross local midnight without
        # waiting). Defaults to ``time.time`` in production.
        self._clock = clock or time.time
        # SCR-214 U3: cadence of the day-roll watch. Cheap (a string compare), so
        # a coarse default is fine; env/param-overridable so tests can tick fast.
        self._ambient_day_tick_interval = (
            ambient_day_tick_interval
            if ambient_day_tick_interval is not None
            else _float_env("SCREENCAP_AMBIENT_DAY_TICK_INTERVAL", 30.0)
        )
        # SCR-214 U6/KTD4: cadence of the incremental-segmentation sweep. Heavier
        # than the day-roll watch (it re-runs the terminal-stage segmenter over the
        # growing ambient day), so a coarser default; env/param-overridable so
        # tests can tick fast. Consequence (KTD4): today's Journal labels refresh
        # on this interval, not per closed chunk.
        self._ambient_seg_tick_interval = (
            ambient_seg_tick_interval
            if ambient_seg_tick_interval is not None
            else _float_env("SCREENCAP_AMBIENT_SEG_TICK_INTERVAL", 300.0)
        )
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
        self._corpus_key_file: Path | None = None
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

        # SCR-214 U2/KTD8: supervised always-on ambient capture.
        # ``_start_gate`` (injected by the app) is the shared permission+paywall
        # gate the internal ambient spawn runs — identical to the HTTP path.
        self._start_gate = start_gate
        # True while the current live ``_proc`` is the ambient stream (drives
        # idle-shutdown + re-arm). ``_ambient_spawn_at`` is its monotonic start
        # time, used to tell a stable run from a rapid crash.
        self._ambient_active = False
        self._ambient_spawn_at: float | None = None
        # Surfaced blocked/degraded reason the app can read (None = healthy):
        # permission/paywall denial, or the retry ceiling after repeated crashes.
        self._ambient_degraded: str | None = None
        # Consecutive rapid-failure count toward the re-arm ceiling.
        self._ambient_retry_count = 0
        # The pending auto-start / backoff re-arm task (keeps the daemon alive
        # across the backoff gap so the always-on stream isn't idle-shut-down).
        self._ambient_spawn_task: asyncio.Task[Any] | None = None
        # SCR-214 U3: the long-lived day-boundary watch task (rolls ambient to the
        # next per-day dir at local midnight / on the first post-wake tick), and a
        # guard flag marking an in-progress roll so the engine-exit funnel treats
        # the roll's intentional stop as a roll — NOT a crash to re-arm/back off.
        self._ambient_day_task: asyncio.Task[Any] | None = None
        self._ambient_rolling = False
        # SCR-214 U6/KTD4: the long-lived incremental-segmentation watch task
        # (re-runs the terminal-stage segmenter over the live ambient day so
        # today's Journal fills before the stream stops). Persists across re-arms
        # and day rolls; cancelled in ``shutdown``.
        self._ambient_seg_task: asyncio.Task[Any] | None = None
        # SCR-214 U6: change-detection key for the last segmentation sweep — a cheap
        # fingerprint of the ambient day's completed-manifest set + kept-task count.
        # The sweep SKIPS a tick whose fingerprint is unchanged (a no-op re-segment
        # on an idle always-on recorder). ``None`` (no completed manifests yet /
        # unreadable) fails OPEN — the pass runs — so we only skip on positive
        # evidence nothing changed.
        self._last_segmentation_key: tuple[Any, ...] | None = None
        # Set at the top of ``shutdown`` so a torn-down daemon never re-arms.
        self._shutting_down = False

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

    async def send_command(self, command: dict[str, Any]) -> bool:
        """Write one NDJSON command line to the running engine's stdin control
        channel (SCR-218 U1).

        Returns ``True`` if the line was written, ``False`` if there is no live
        engine or the pipe is broken. Serialized under ``_operation_lock`` so it
        cannot race a spawn/stop that swaps or tears down ``_proc``; a mute that
        loses that race simply finds no live engine and returns ``False`` rather
        than raising. Broken-pipe/OS errors on the write are likewise swallowed
        into a ``False`` result — the engine is on its way out.
        """
        line = json.dumps(command) + "\n"
        async with self._operation_lock:
            proc = self._proc
            if proc is None or not proc.is_alive():
                return False
            try:
                # Bound the pipe write (SCR-254 polish). Today only tiny, low-rate
                # mute lines ride this channel, so a write never blocks — but if a
                # future control handler let the engine's stdin reader stall, a
                # full pipe buffer would wedge ``send_line`` while it holds
                # ``_operation_lock``, blocking every spawn/stop. ``wait_for``
                # abandons the await on timeout (the orphaned thread's write drains
                # or dies with the engine), so the lock is always released; a
                # timed-out write is treated as an unavailable engine.
                await asyncio.wait_for(
                    asyncio.to_thread(proc.send_line, line),
                    timeout=_SEND_COMMAND_TIMEOUT_S,
                )
                return True
            except (BrokenPipeError, ValueError, OSError):
                return False
            except asyncio.TimeoutError:
                logger.warning(
                    "send_command: engine stdin write timed out; treating engine "
                    "as unavailable"
                )
                return False

    async def set_muted(self, muted: bool) -> bool:
        """Forward a mute/unmute request to the running engine (SCR-218 U4).

        Returns ``True`` if forwarded, ``False`` if there is no live engine. It
        deliberately does NOT write mute state into ``_session_state`` — the
        engine's confirmed ``audio_muted`` / ``audio_unmuted`` event is the sole
        writer (KTD4/U5), so the snapshot never reports a request that may have
        failed at the actual ``stream.stop()``.
        """
        return await self.send_command({"type": "set_muted", "muted": bool(muted)})

    async def set_paused(self, paused: bool) -> bool:
        """Forward a pause/resume request to the running engine (SCR-214 U4).

        A pass-through mirroring :meth:`set_muted`: returns ``True`` if forwarded,
        ``False`` if there is no live engine. It deliberately does NOT write pause
        state into ``_session_state`` — the engine's confirmed ``recording_paused``
        / ``recording_resumed`` event (emitted only after capture is actually
        gated, KTD7) is the sole writer, so the snapshot never reports a request
        that may have raced engine teardown.
        """
        return await self.send_command({"type": "set_paused", "paused": bool(paused)})

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

            # Search U8 / KTD5: resolve the capture gate ONCE and thread the same
            # result into the args (on/off) and _stage_corpus_key (key/encryption),
            # so the two decisions can never diverge (a between-reads key flip must
            # not yield stills-on + encryption-off = plaintext).
            from screencap.capture_gate import gather_and_resolve

            gate = gather_and_resolve(
                explicit_capture_images=request.capture_images,
                scrub_enabled=request.scrub_enabled,
            )
            args = self._worker_args(request, name=name, capture_dir=capture_dir, gate=gate)
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
                corpus_env = await self._stage_corpus_key(request, capture_dir, gate=gate)
                if corpus_env:
                    extra_env = {**(extra_env or {}), **corpus_env}
                elif gate.capture_images_encrypted:
                    # Encryption was REQUIRED but the key could not be staged. The
                    # engine's U2 guard only blocks plaintext when RECORD_IMAGES_ENCRYPTED
                    # is set; without it, stills-on would write PLAINTEXT. Clamp stills
                    # off and re-encode before the spawn so a staging failure can never
                    # fall through to plaintext capture (never-plaintext invariant).
                    logger.warning(
                        "daemon: corpus key staging failed while encryption required; "
                        "clamping stills OFF for this recording (never plaintext)"
                    )
                    args["capture_images"] = False
                    encoded_args = base64.b64encode(
                        json.dumps(args, separators=(",", ":")).encode("utf-8")
                    ).decode("ascii")
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
        # SCR-214 U2: mark the daemon as tearing down BEFORE the engine exit is
        # handled, so `_handle_engine_exit` does not re-arm the ambient stream
        # into a dying loop. Cancel any pending ambient re-arm for the same reason.
        self._shutting_down = True
        # Cancel the pending ambient re-arm (U2), the day-boundary watch (U3 — a
        # torn-down daemon never rolls), and the incremental-segmentation watch (U6
        # — never kicks off a new pass).
        for _ambient_task in (
            self._ambient_spawn_task,
            self._ambient_day_task,
            self._ambient_seg_task,
        ):
            if _ambient_task is not None and not _ambient_task.done():
                _ambient_task.cancel()
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
            self._prune_stale_corpus_key_files()
            self._recovering = False
            # SCR-125 U6 F3 startup sweep — resume cloud recordings left
            # incomplete by a prior daemon/engine crash. Detached (tracked) so it
            # never blocks request handling: _recovering is already cleared, and a
            # long backlog upload must not make `spawn`/`stop` raise Reconciling.
            self._track_resume(self._run_startup_sweep())
            # SCR-214 U2: now that reconcile has cleared `_recovering`, auto-start
            # the always-on ambient stream if the user enabled it. A no-op when
            # ambient is disabled (the default); scheduled detached so a slow
            # gate/spawn never blocks reconcile completion.
            self._maybe_autostart_ambient()

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

    @staticmethod
    def _prune_stale_corpus_key_files() -> None:
        """Delete any leftover ``corpus-key-*`` files at daemon startup.

        A hard crash / SIGKILL runs no Python teardown, so a 0600 file holding the
        corpus key can survive in the run dir. At daemon startup no live recording
        can legitimately own one (the engine that read it is gone), so unlink every
        survivor — the corpus key is Keychain-entitlement protected and a lingering
        plaintext copy would let any same-EUID process decrypt the corpus. Best-effort.
        """
        from screencap.config import get_base_dir

        run_dir = get_base_dir() / "run"
        try:
            stale = list(run_dir.glob("corpus-key-*"))
        except OSError as exc:
            logger.warning("daemon: could not scan for stale corpus key files: %s", exc)
            return
        for path in stale:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "daemon: could not remove stale corpus key file %s: %s", path, exc
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

            # SCR-214 U2: snapshot whether THIS exit was the ambient stream (and
            # how long it ran) before teardown, so the re-arm decision below has
            # it. The engine is gone → no longer "active"; re-arm (if any) revives.
            was_ambient = self._ambient_active
            ambient_spawn_at = self._ambient_spawn_at
            if was_ambient:
                self._ambient_active = False

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

            # SCR-214 U2: re-arm the always-on ambient stream after its engine
            # exits (unless the daemon is shutting down or ambient was disabled).
            # A clean/long-lived exit re-arms promptly; a rapid crash loop backs
            # off and surfaces a degraded state at the ceiling rather than thrash.
            #
            # SCR-214 U3: a DAY-BOUNDARY ROLL is an intentional stop, not a crash.
            # ``_roll_ambient_day`` owns the respawn (into the next per-day dir), so
            # skip the exit-funnel re-arm here — otherwise the roll would both
            # re-arm the old day AND spawn the new one (double spawn), or a rc!=0
            # force-stop of a short (test-clock) run would wrongly count toward the
            # crash-loop backoff ceiling.
            if was_ambient and not self._ambient_rolling:
                run_duration = (
                    time.monotonic() - ambient_spawn_at
                    if ambient_spawn_at is not None
                    else 0.0
                )
                self._schedule_ambient_rearm(rc, run_duration)
            elif not was_ambient:
                # SCR-214 reliability: a NON-ambient (explicit) recording just
                # exited, FREEING the recording lock. If ambient is enabled but was
                # DEFERRED by that held lock — ``_spawn_ambient`` swallowed a
                # ``LockContendedError`` at reconcile / day-roll and never started —
                # the ``was_ambient`` re-arm above would never fire for it, so
                # ambient would stay silently down until the next daemon restart.
                # Attempt the same gated auto-start now that the lock is free (the
                # lock frees exactly when the explicit recording exits).
                # ``_maybe_autostart_ambient`` is idempotent + self-guarding — a
                # no-op when ambient is already active/pending, the daemon is
                # shutting down, ambient is disabled, or it already hit its degraded
                # ceiling — so this can never double-spawn and a persistent failure
                # stays surfaced as degraded rather than retrying forever.
                self._maybe_autostart_ambient()

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
        # SCR-218 U5: the pump is the SOLE writer of mute state. The engine emits
        # these only after the mic stream actually toggles (KTD4), so the
        # snapshot / events reflect confirmed capture, never the mute request.
        elif event_type == _stderr_events.EVENT_AUDIO_MUTED:
            self._session_state["muted"] = True
        elif event_type == _stderr_events.EVENT_AUDIO_UNMUTED:
            self._session_state["muted"] = False
        # SCR-214 U4: the confirmed pause events are the SOLE writer of pause
        # state. Engine-main emits them only after the video/screenshot capture
        # gate is actually set (KTD7), so the snapshot / events reflect gated
        # capture, never the pause request.
        elif event_type == _stderr_events.EVENT_RECORDING_PAUSED:
            self._session_state["paused"] = True
        elif event_type == _stderr_events.EVENT_RECORDING_RESUMED:
            self._session_state["paused"] = False

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
        # SCR-214 U1 / KTD1: ambient capture is a single continuous per-day
        # recording. Resolve a DETERMINISTIC ``ambient-YYYYMMDD`` dir (local day)
        # and reopen today's if it already exists and is not finalized, so a
        # daemon restart or re-enable mid-day keeps ONE dir per day rather than
        # forking ``ambient-YYYYMMDD-2`` through the collision-suffix loop below.
        if getattr(request, "ambient", False):
            return self._allocate_ambient_capture_dir(get_recordings_dir())
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

    def _allocate_ambient_capture_dir(
        self, recordings_dir: Path
    ) -> tuple[str, Path]:
        """Resolve-or-reopen the deterministic per-day ambient dir (SCR-214 U1).

        Returns ``ambient-YYYYMMDD`` for the local day. When today's dir already
        exists and is NOT finalized it is reopened verbatim (no ``-2`` suffix), so
        the day stays a single continuous container across a daemon restart or a
        mid-day re-enable (KTD1). When it exists AND is finalized, reopening would
        corrupt the frozen ledger (:func:`_ambient_dir_finalized`), so a fresh
        suffixed dir is forked instead — the rare edge where a day was already
        closed (e.g. a manual stop) but ambient restarts on the same calendar day.
        """
        # ``getattr`` fallback keeps the U1 ``_AllocOnly`` test double (which binds
        # this method with no ``_clock`` instance state) working on real wall time.
        day_name = _ambient_day_name(getattr(self, "_clock", time.time))
        candidate = recordings_dir / day_name
        if not candidate.exists():
            return day_name, candidate
        if not _ambient_dir_finalized(candidate):
            return day_name, candidate
        # Finalized: do not reopen. Fork a suffixed dir so capture still proceeds.
        for index in range(2, 1000):
            name = f"{day_name}-{index}"
            candidate = recordings_dir / name
            if not candidate.exists():
                return name, candidate
        raise RuntimeError(f"could not allocate capture dir for {day_name!r}")

    # ------------------------------------------------------------------
    # SCR-214 U2/KTD8: supervised always-on ambient capture
    # ------------------------------------------------------------------

    def ambient_supervision_active(self) -> bool:
        """True while ambient capture is running OR a re-arm is pending.

        Mirrors :meth:`has_inflight_resume` for the idle-shutdown watchdog: an
        active always-on recording, or the backoff gap before its re-arm fires,
        must keep the (auto-spawned) daemon alive so ambient is never silently
        dropped. A degraded/ceiling'd ambient holds nothing pending, so it does
        NOT keep the daemon alive (nothing left to supervise).
        """
        if self._ambient_active:
            return True
        task = self._ambient_spawn_task
        return task is not None and not task.done()

    def ambient_state(self) -> dict[str, Any]:
        """Surfaced ambient supervision state for the app (SCR-214 U2/U12).

        ``enabled`` / ``autostart`` reflect config; ``active`` is a live ambient
        recording; ``paused`` is that recording's CONFIRMED pause state (U4);
        ``recording`` is its name (so the app can target ``recording.pause`` /
        ``.resume`` at it); ``degraded`` is a human-readable blocked/ceiling
        reason (None = healthy) so the app can show *why* an enabled ambient is
        not recording instead of a silent on-with-nothing-captured toggle.

        ``paused`` / ``recording`` are read from the live session snapshot ONLY
        while ``_ambient_active`` (the live ``_proc`` is the ambient stream), so an
        explicit user recording is never mis-reported as the ambient one; both are
        their inert defaults (``False`` / ``None``) whenever nothing ambient is
        live. ``paused`` defaults to ``False`` until the engine's confirmed
        ``recording_paused`` event first writes it (KTD7).
        """
        from screencap.config import get_ambient_autostart, get_ambient_enabled

        recording: str | None = None
        paused = False
        session = self._session_state
        if self._ambient_active and session is not None:
            name = session.get("recording_name")
            if isinstance(name, str):
                recording = name
            paused = bool(session.get("paused", False))

        return {
            "enabled": get_ambient_enabled(),
            "autostart": get_ambient_autostart(),
            "active": self._ambient_active,
            "paused": paused,
            "degraded": self._ambient_degraded,
            "recording": recording,
            "retry_count": self._ambient_retry_count,
        }

    def _maybe_autostart_ambient(self) -> None:
        """Kick the ambient auto-start once the recording lock is free (SCR-214 U2/R2).

        Called at TWO points: after reconcile clears (the daemon-boot auto-start),
        and from the engine-exit funnel when a NON-ambient recording exits and
        frees a lock that had DEFERRED ambient. A no-op when ambient is disabled
        (``get_ambient_enabled() → False``, the default). Otherwise schedules the
        gated spawn detached so a slow gate or engine startup never blocks the
        caller. Tracked in ``_ambient_spawn_task`` so idle-shutdown + ``shutdown``
        see it.

        Idempotent + self-guarding so the exit-funnel caller can never double-spawn
        onto a live ambient stream, over a spawn / backoff re-arm already pending,
        or past the degraded ceiling — a persistently-failing ambient must stay
        surfaced as degraded, never silently retried forever. (These guards are
        inert at the reconcile call: at boot ambient is never active, pending, or
        degraded.)
        """
        from screencap.config import get_ambient_enabled

        if self._shutting_down or not get_ambient_enabled():
            return
        # Never double-spawn: ambient already live, or the retry ceiling already
        # reached (surfaced as degraded — a persistent failure stays surfaced).
        if self._ambient_active or self._ambient_degraded is not None:
            return
        # A spawn attempt / backoff re-arm is already pending — it will start (or
        # re-defer) ambient on its own; scheduling another would double-spawn.
        pending = self._ambient_spawn_task
        if pending is not None and not pending.done():
            return
        self._ambient_spawn_task = asyncio.create_task(self._spawn_ambient())
        # SCR-214 U3: start the day-boundary watch alongside auto-start so it is
        # live for the whole ambient lifetime (it persists across re-arms and
        # rolls). No-op if already running.
        self._start_ambient_day_watch()
        # SCR-214 U6: start the incremental-segmentation watch alongside auto-start
        # so today's Journal fills as the day progresses (R7). Also persists across
        # re-arms and rolls. No-op if already running.
        self._start_ambient_segmentation_watch()

    async def stop_ambient_now(self) -> bool:
        """Stop the live ambient recording now, if one is running (SCR-214 U12).

        The runtime counterpart to :meth:`_maybe_autostart_ambient` for the
        ``ambient.set`` verb's ``enabled=False`` path. It stops ONLY the always-on
        ambient stream — never an explicit user recording — by gating on
        ``_ambient_active`` (True exactly while the live ``_proc`` is the ambient
        stream). A no-op returning ``False`` when no ambient recording is live, so
        an explicit recording or an idle daemon is left untouched.

        The caller MUST have already flipped ``get_ambient_enabled()`` to False:
        ``stop`` funnels through ``_handle_engine_exit`` whose ambient re-arm
        (:meth:`_schedule_ambient_rearm`) re-reads that config and, seeing it
        False, does NOT respawn — so this stop is final, not a bounce. Returns
        True iff an ambient recording was actually stopped.
        """
        if not self._ambient_active:
            return False
        await self.stop()
        return True

    def _build_ambient_request(self) -> "RecordingStartRequest":
        """Build the internal always-on ambient ``RecordingStartRequest`` (U2).

        Hard-pins local-only (``cloud_intent=False``, no ``force_mode``)
        regardless of ``get_upload_default()`` — KTD2: the always-on stream must
        never upload; U1's intent freeze + the ``lock_policy`` assertion enforce
        it downstream. ``ambient=True`` routes the deterministic per-day
        ``ambient-YYYYMMDD`` dir and forces the audio substream on (R3).
        """
        from screencap.daemon.schema import RecordingStartRequest

        return RecordingStartRequest(
            ambient=True,
            cloud_intent=False,
            force_mode=None,
        )

    async def _spawn_ambient(self) -> None:
        """Gate + spawn the internal ambient recording (SCR-214 U2/KTD8).

        Runs the SHARED start gate (permission + paywall) first — the same gate
        the HTTP ``recording.start`` path runs — so a future gate can't be added
        there but missed here. A gate denial records a surfaced degraded state
        and does NOT spawn. A ``LockContendedError`` means another recording
        already holds the lock (an explicit start won the race); that is not a
        failure — a later exit re-arm or the next boot retries. Any other spawn
        error is treated as a rapid failure toward the backoff ceiling.
        """
        if self._shutting_down:
            return
        if self._start_gate is not None:
            try:
                await self._start_gate()
            except errors.DaemonAPIError as exc:
                self._set_ambient_degraded(f"blocked: {exc.error_code}")
                return
            except Exception:  # noqa: BLE001 — a gate error must not crash the supervisor
                logger.warning("ambient start gate raised; not spawning", exc_info=True)
                self._set_ambient_degraded("blocked: gate_error")
                return

        request = self._build_ambient_request()
        try:
            await self.spawn(request)
        except errors.LockContendedError:
            logger.info("ambient auto-start deferred: recording lock already held")
            return
        except errors.ReconcilingError:
            logger.info("ambient auto-start deferred: still reconciling")
            return
        except Exception:  # noqa: BLE001 — never let a spawn error crash the supervisor
            logger.warning("ambient auto-start spawn failed", exc_info=True)
            self._backoff_and_reschedule(
                ceiling_reason="ambient spawn repeatedly failed; "
                "re-arm paused at the retry ceiling"
            )
            return

        # Spawn succeeded — ambient is live and healthy.
        self._ambient_active = True
        self._ambient_spawn_at = time.monotonic()
        self._ambient_degraded = None

    def _schedule_ambient_rearm(self, rc: int, run_duration: float) -> None:
        """Decide whether/how to re-arm ambient after its engine exited (U2).

        A clean exit (``rc == 0``) or a run that lasted past the quiescence
        window resets the crash budget and re-arms promptly. A rapid non-zero
        exit engages exponential backoff and, past the retry ceiling, surfaces a
        degraded state instead of thrash-respawning.
        """
        if self._shutting_down:
            return
        from screencap.config import get_ambient_enabled

        if not get_ambient_enabled():
            # The user turned ambient off — do not re-arm; clear the crash budget.
            self._ambient_retry_count = 0
            return

        _base, _max_delay, _max_retries, quiescence = _ambient_rearm_params()
        if rc == 0 or run_duration >= quiescence:
            self._ambient_retry_count = 0
            self._ambient_spawn_task = asyncio.create_task(
                self._ambient_rearm_after(0.0)
            )
            return

        self._backoff_and_reschedule(
            ceiling_reason="engine exited rapidly and repeatedly; "
            "ambient re-arm paused at the retry ceiling"
        )

    def _backoff_and_reschedule(self, *, ceiling_reason: str) -> None:
        """Increment the crash budget and schedule a backed-off re-arm (U2).

        Past ``max_retries`` consecutive rapid failures, stop re-arming and
        surface ``ceiling_reason`` as the degraded state — the anti-thrash valve.
        """
        base, max_delay, max_retries, _quiescence = _ambient_rearm_params()
        self._ambient_retry_count += 1
        if self._ambient_retry_count > max_retries:
            self._set_ambient_degraded(ceiling_reason)
            return
        delay = min(base * (2 ** (self._ambient_retry_count - 1)), max_delay)
        self._ambient_spawn_task = asyncio.create_task(
            self._ambient_rearm_after(delay)
        )

    async def _ambient_rearm_after(self, delay: float) -> None:
        """Sleep ``delay`` then re-spawn ambient, unless torn down / disabled."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._shutting_down:
            return
        from screencap.config import get_ambient_enabled

        if not get_ambient_enabled():
            return
        await self._spawn_ambient()

    def _set_ambient_degraded(self, reason: str) -> None:
        """Record + log the surfaced ambient degraded/blocked reason (U2)."""
        logger.warning("ambient capture degraded: %s", reason)
        self._ambient_degraded = reason

    # ------------------------------------------------------------------
    # SCR-214 U3/KTD1/R5: day-boundary recording roll
    # ------------------------------------------------------------------

    def _local_day_name(self) -> str:
        """Current local calendar day as the ambient dir name (SCR-214 U3).

        ``ambient-YYYYMMDD`` for the local day of the injectable clock — the
        day-roll watch's change-detection side of the shared :func:`_ambient_day_name`
        definition (``_allocate_ambient_capture_dir`` is the naming side), so the
        two can never disagree on "what day is it".
        """
        return _ambient_day_name(self._clock)

    def _active_ambient_day(self) -> str | None:
        """The day-container name of the live ambient recording, or ``None``.

        Derives ``ambient-YYYYMMDD`` from the current session's recording name,
        stripping any collision suffix (a forked ``ambient-YYYYMMDD-2`` still
        compares by its DAY) so a same-day fork never triggers a spurious roll.
        Returns ``None`` when nothing ambient is live.
        """
        session = self._session_state
        if not self._ambient_active or not session:
            return None
        name = session.get("recording_name")
        if not isinstance(name, str):
            return None
        parts = name.split("-")
        if len(parts) >= 2 and parts[0] == "ambient" and _is_yyyymmdd(parts[1]):
            return f"ambient-{parts[1]}"
        return None

    def _start_watch(
        self, task_attr: str, coro_factory: "Callable[[], Any]"
    ) -> None:
        """Idempotently start a long-lived watch task stored on ``task_attr``.

        Shared by the day-boundary (U3) and incremental-segmentation (U6) watches:
        a no-op if the task on ``task_attr`` is still running, else create it from
        ``coro_factory`` (a zero-arg callable returning the watch coroutine).
        """
        existing = getattr(self, task_attr)
        if existing is not None and not existing.done():
            return
        setattr(self, task_attr, asyncio.create_task(coro_factory()))

    async def _periodic_watch(
        self,
        interval: float,
        tick: "Callable[[], Any]",
        failure_msg: str,
    ) -> None:
        """Shared driver for the long-lived ambient watches (U3/U6).

        Loops until shutdown: sleep ``interval`` (a cancel is a clean exit), then
        run ``tick`` (a zero-arg coroutine factory) fail-open — a tick error is
        logged with ``failure_msg`` and the watch keeps going. The distinct
        per-watch work lives entirely in ``tick``; the sleep cadence, the
        cancel/shutdown guards, and the fail-open belt are identical across watches.
        """
        while not self._shutting_down:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self._shutting_down:
                return
            try:
                await tick()
            except Exception:  # noqa: BLE001 — a tick error must not kill the watch
                logger.warning(failure_msg, exc_info=True)

    def _start_ambient_day_watch(self) -> None:
        """Start the long-lived day-boundary watch task (idempotent)."""
        self._start_watch(
            "_ambient_day_task",
            lambda: self._periodic_watch(
                self._ambient_day_tick_interval,
                self._ambient_day_watch_tick,
                "ambient day-roll failed; will retry on the next tick",
            ),
        )

    async def _ambient_day_watch_tick(self) -> None:
        """Roll the ambient recording at the LOCAL day boundary (SCR-214 U3/R5).

        Per-tick body of the day-boundary watch (driven by :meth:`_periodic_watch`).
        Compares the live ambient recording's day (from its ``ambient-YYYYMMDD`` dir
        name) against the current local day; on an advance it performs a stop→start
        roll to the next per-day dir.

        WAKE-SAFE by construction: the roll is driven by an *observed* date change,
        NOT a fired-at-midnight timer. While the Mac sleeps the daemon (and this
        watch) is suspended, so no tick fires at the real midnight; the FIRST tick
        after wake sees the advanced date and rolls then. No macOS wake
        notification is needed for correctness. The check is cheap (a string
        compare) and rolls at most once per boundary — post-roll the live day
        equals ``now`` again, so the next tick is a no-op.
        """
        active_day = self._active_ambient_day()
        if active_day is None or active_day == self._local_day_name():
            return
        await self._roll_ambient_day()

    async def _roll_ambient_day(self) -> None:
        """Close today's ambient recording and open the next day's (U3/R5/KTD1).

        A RECORDING-level roll (new per-day dir), not an unbounded single dir, so
        day-grouping, ``timeline.day``, and per-day retention stay clean.

        1. ``stop()`` drives the live ambient recording through its NORMAL terminal
           path (finalize → final segmentation pass — NOT reimplemented here). That
           stop force-closes the currently-open chunk via
           ``ChunkedVideoWriter.close()``, so a chunk left open across midnight
           (e.g. during sleep) is attributed to exactly ONE day: the day it STARTED
           in (day N). The new day's dir opens only afterwards, so no chunk can
           straddle two per-day dirs.
        2. U2's ambient spawn path opens the fresh ``ambient-YYYYMMDD`` dir for the
           now-current local day (day N+1), via the same shared start gate.

        ``_ambient_rolling`` marks the stop as an INTENTIONAL roll so the
        engine-exit funnel skips its own re-arm/backoff (this method owns the
        respawn) — an intentional stop is not a crash.
        """
        if self._ambient_rolling or not self._ambient_active or self._shutting_down:
            return
        self._ambient_rolling = True
        try:
            await self.stop()
            if self._shutting_down:
                return
            await self._spawn_ambient()
        finally:
            self._ambient_rolling = False

    # ------------------------------------------------------------------
    # SCR-214 U6/KTD4/R7: incremental segmentation of the live ambient day
    # ------------------------------------------------------------------

    def _active_ambient_dir(self) -> "Path | None":
        """The on-disk dir of the live ambient recording, or ``None`` (U6).

        Derives the recording dir from the current session's ``capture_dir`` when
        an ambient stream is live, so the incremental segmenter targets exactly the
        dir the engine is writing. Returns ``None`` when nothing ambient is live
        (mirrors :meth:`_active_ambient_day`, but yields the path the segmenter
        needs rather than the day name the roll watch compares).
        """
        session = self._session_state
        if not self._ambient_active or not session:
            return None
        capture_dir = session.get("capture_dir")
        if not isinstance(capture_dir, str):
            return None
        return Path(capture_dir)

    def _start_ambient_segmentation_watch(self) -> None:
        """Start the long-lived incremental-segmentation watch task (idempotent, U6)."""
        self._start_watch(
            "_ambient_seg_task",
            lambda: self._periodic_watch(
                self._ambient_seg_tick_interval,
                self._ambient_segmentation_watch_tick,
                "ambient incremental segmentation tick failed; will retry on "
                "the next tick",
            ),
        )

    async def _ambient_segmentation_watch_tick(self) -> None:
        """Re-segment the live ambient day so today's Journal fills (U6/R7).

        Per-tick body of the incremental-segmentation sweep (env
        ``SCREENCAP_AMBIENT_SEG_TICK_INTERVAL``, default 300s), driven by
        :meth:`_periodic_watch`. For the active ambient recording it runs one
        incremental segmentation pass on a WORKER THREAD (mirroring
        ``resume_terminal_stage``'s non-blocking pattern so the asyncio loop is never
        blocked by the terminal-stage segmenter). Started alongside ambient
        auto-start and cancelled in ``shutdown``; persists across re-arms and day
        rolls (KTD4). A tick is a no-op when nothing ambient is live, or while a day
        roll is in progress (the recording is mid stop→start).

        CHANGE-DETECTION (U6): the pass SKIPS when the ambient day's fingerprint
        (completed-manifest set + kept-task count) is unchanged since the last pass —
        a full-day re-segment on an idle always-on recorder is a no-op that produces
        identical output. An unknown fingerprint (no completed manifests yet /
        unreadable) fails OPEN and runs the pass, so we skip only on positive
        evidence nothing changed. The kept-task count is in the key so a user
        curation edit still forces a re-carve even with no new footage.
        """
        # A roll is stopping/reopening the recording — let it settle rather than
        # segmenting a dir mid-teardown (the finalize's own final pass covers it).
        if self._ambient_rolling:
            return
        recording_dir = self._active_ambient_dir()
        if recording_dir is None:
            return
        key = self._segmentation_fingerprint(recording_dir)
        if key is not None and key == self._last_segmentation_key:
            return
        await self._run_incremental_segmentation(recording_dir)
        self._last_segmentation_key = key

    def _segmentation_fingerprint(
        self, recording_dir: Path
    ) -> "tuple[Any, ...] | None":
        """A cheap change-key for the ambient day, or ``None`` to force a pass (U6).

        Combines the completed-manifest set (``chunk_*_manifest.json`` count, highest
        chunk index, max manifest mtime — statted, never re-read) with the kept
        (user/edited) task-row count, plus the dir name so a day roll never aliases a
        prior day's key. Returns ``None`` — meaning "run the pass" (fail-open) — when
        there is no completed manifest yet or anything is unreadable, so a skip only
        ever happens on positive evidence of an unchanged, non-empty state.
        """
        try:
            manifests = list(recording_dir.glob("chunk_*_manifest.json"))
        except OSError:
            return None
        if not manifests:
            return None
        try:
            count = len(manifests)
            highest_index = max(
                int(m.name.split("_")[1]) for m in manifests
            )
            max_mtime = max(m.stat().st_mtime_ns for m in manifests)
            kept = self._kept_task_row_count(recording_dir)
        except (OSError, ValueError, IndexError):
            return None
        return (recording_dir.name, count, highest_index, max_mtime, kept)

    def _kept_task_row_count(self, recording_dir: Path) -> int:
        """Count the recording's KEPT (user/edited) task rows for the change-key (U6).

        Reads the local-only ``recording.db`` task-segment store and counts the
        protected (``source='user'`` OR edited) rows, so a user curation edit shifts
        the segmentation fingerprint and forces a re-carve. A missing DB yields ``0``;
        a read error propagates to the fingerprint's fail-open ``None``.
        """
        from screencap.pipeline_state import (
            PipelineLedger,
            task_row_is_protected,
        )

        db_path = recording_dir / "recording.db"
        if not db_path.exists():
            return 0
        rows = PipelineLedger(db_path).read_task_segments()
        return sum(1 for r in rows if task_row_is_protected(r))

    async def _run_incremental_segmentation(self, recording_dir: Path) -> None:
        """Run one incremental segmentation pass off-loop (U6/KTD4).

        Mirrors ``resume_terminal_stage``'s worker-thread + non-blocking-flock
        pattern: the terminal-stage segmenter is blocking work (DB reads, provider
        call) that must not stall the event loop, so it runs in a worker thread; it
        acquires the per-recording terminal flock NON-BLOCKING, so a concurrent
        finalize that holds the flock makes this pass SKIP (``TerminalStageBusy``)
        rather than race (AE12). Strictly fail-open — every error is swallowed so a
        bad pass never crashes the watch or the daemon (the R11 privacy strip is
        fail-closed INSIDE the segmenter, independent of this fail-open outer belt).
        """
        from screencap.terminal_stage import (
            TerminalStageBusy,
            run_incremental_segmentation,
        )

        def _run() -> None:
            try:
                run_incremental_segmentation(recording_dir, non_blocking=True)
            except TerminalStageBusy:
                logger.debug(
                    "ambient incremental segmentation: %s busy (a finalize/upload "
                    "holds the flock); skipping this tick", recording_dir,
                )
            except Exception as exc:  # noqa: BLE001 — never propagate into the loop
                logger.warning(
                    "ambient incremental segmentation failed for %s (%s); fail-open",
                    recording_dir, exc,
                )

        await asyncio.to_thread(_run)

    def _worker_args(
        self,
        request: "RecordingStartRequest",
        *,
        name: str,
        capture_dir: Path,
        gate: "CaptureGateResult | None" = None,
    ) -> dict[str, Any]:
        return build_engine_worker_args(
            request, name=name, capture_dir=capture_dir, gate=gate
        )

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
        self,
        request: "RecordingStartRequest",
        capture_dir: Path,
        *,
        gate: "CaptureGateResult",
    ) -> dict[str, str] | None:
        """Stage the corpus key for an ENCRYPTED-stills recording's engine (search U8).

        Consumes the SAME ``gate`` ``spawn`` resolved for ``build_engine_worker_args``
        (single-resolution invariant — the on/off and encryption decisions can't
        diverge). When stills are on AND encrypted, writes the corpus key to a
        per-recording 0600 file and returns the env overlay pointing the engine at it
        (``corpus_crypto.CORPUS_KEY_FILE_ENV``) plus ``RECORD_IMAGES_ENCRYPTED=1``.
        The key bytes go via the FILE, never argv/config (``ps``-visible), mirroring
        the ID-token and cloud-key channels.

        The staged file is tracked (``self._corpus_key_file``), unlinked on teardown
        (``_cleanup_corpus_key_file``) and pruned at daemon startup
        (``_prune_stale_corpus_key_files``) — the corpus key is Keychain-entitlement
        protected, so a persistent plaintext 0600 copy must not outlive the recording
        (it would let any same-EUID process decrypt the corpus, undoing that bar).

        Returns ``None`` when stills are off or plaintext. When encryption was
        required but the key can't be staged this returns ``None`` too; ``spawn`` then
        clamps stills OFF so the engine never captures plaintext."""
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

        path = self._corpus_key_path(capture_dir)
        try:
            await asyncio.to_thread(corpus_crypto.write_key_file, str(path), key)
        except OSError as exc:
            logger.warning("daemon: could not stage corpus key file: %s", exc)
            return None
        self._corpus_key_file = path
        return {corpus_crypto.CORPUS_KEY_FILE_ENV: str(path), "RECORD_IMAGES_ENCRYPTED": "1"}

    @staticmethod
    def _corpus_key_path(capture_dir: Path) -> Path:
        from screencap.config import get_base_dir

        run_dir = get_base_dir() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"corpus-key-{capture_dir.name}.key"

    def _cleanup_corpus_key_file(self) -> None:
        path = self._corpus_key_file
        self._corpus_key_file = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("daemon: could not remove corpus key file %s: %s", path, exc)

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
        from screencap.config import get_cloud_e2ee_enabled, invalidate_config_cache

        # The config cache is per-process with no cross-process invalidation
        # (config.py) — a `screencap e2ee enable/disable` run between
        # recordings only invalidates the CLI process's cache. Re-read from
        # disk before every staging decision so the toggle reaches this
        # long-running daemon without a restart.
        invalidate_config_cache()
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
        self._cleanup_corpus_key_file()
        self._engine_token_uid = None
        self._finalized_seen = False
        self._stopping = False
        self._exit_handled = False


__all__ = ["Supervisor"]
