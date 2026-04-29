"""Session Controller — long-lived orchestrator for back-to-back recordings.

``screencap start`` used to be a one-shot: one CLI invocation recorded
exactly one capture, inline-post-processed it, and hard exited. The
:class:`SessionController` turns it into a persistent process that

1. spawns the menu bar once for the whole session,
2. runs each recording in a fresh subprocess worker (isolated engine
   state + child processes), and
3. detaches post-processing into its own subprocess so a completed
   recording can be transcribed / uploaded / named while the next
   recording is already capturing.

The controller owns the signal handlers (SIGINT/SIGTERM). Recording
workers detach from the controller's process group via
:func:`os.setpgrp` and ignore ``SIGINT`` so that Ctrl+C on the tty is
funnelled through the controller's 3-tap logic rather than being
delivered to every descendant at once.
"""

# ruff: noqa: I001
#
# The import order in this file is load-bearing: ``screencap._startup``
# must be imported BEFORE ``multiprocessing`` so its PYTHONWARNINGS
# filter is installed before the resource_tracker subprocess is
# lazily spawned. Auto-sorting the imports would break this invariant.

from __future__ import annotations

from screencap import _startup  # noqa: F401

import atexit
import json
import multiprocessing
import os
import queue as _queue_mod
import signal
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import logging

from rich.console import Console

from screencap._startup import close_queues_safely as _close_queues_safely

console = Console()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State + data classes
# ---------------------------------------------------------------------------


class SessionState(str, Enum):
    """Top-level controller state.

    ``IDLE`` — no recording active; menubar shows "▶ Start Recording".
    ``RECORDING`` — a Recording Worker subprocess is alive; menubar shows
    "■ Stop Recording".
    ``STOPPING`` — transient state while a stop click is being dispatched
    (the worker has been SIGTERM'd but hasn't been handed off to the
    ``_finishing_workers`` list yet).

    The terminal "shutdown" condition is tracked separately via
    :attr:`SessionController._shutdown_requested` because it is
    orthogonal to the current state — a shutdown can be requested while
    RECORDING, STOPPING, or IDLE and must still honor the transition
    rules before the main loop exits.
    """

    IDLE = "idle"
    RECORDING = "recording"
    STOPPING = "stopping"


@dataclass
class _RWQueues:
    """The per-recording queue set the controller hands to each worker."""

    window_feed_q: "multiprocessing.Queue"
    override_q: "multiprocessing.Queue"
    disable_q: "multiprocessing.Queue"


@dataclass
class _RecordingWorker:
    """Bookkeeping for an active Recording Worker subprocess."""

    name: str
    capture_dir: Path
    proc: "multiprocessing.Process"
    queues: _RWQueues
    started_at: float
    forwarder_thread: threading.Thread | None = None


@dataclass
class PostProcessJob:
    """Bookkeeping for a pending/running Post-Process Worker subprocess."""

    name: str
    capture_dir: Path
    args: dict
    proc: "multiprocessing.Process | None" = None
    started_at: float = 0.0


def _read_recording_ready(capture_dir: Path) -> dict:
    """Parse the ``.recording_ready`` completion marker, or return ``{}``.

    Written by :func:`run_recording_worker` on clean exit; consumed by
    the controller (to pick up ``disk_full``) and by
    :func:`_postprocess_pipeline` (to recover ``elapsed`` for the
    summary, since the post-process worker runs out-of-process).
    """
    try:
        return json.loads((capture_dir / ".recording_ready").read_text())
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Recording Worker subprocess entry point
# ---------------------------------------------------------------------------


def run_recording_worker(args: dict) -> None:
    """Recording Worker subprocess entry point.

    Detaches from the controller's process group so the tty's SIGINT does
    not reach us (the controller owns Ctrl+C), then invokes
    :func:`screencap.recorder.start_recording` with worker-mode flags set.

    Writes ``.recording_ready`` (on clean exit) or ``.recording_error.log``
    (on exception) into the capture_dir so the controller can observe the
    outcome without having to share Python state across the process
    boundary.
    """
    # Detach from the controller's process group so Ctrl+C on the tty is
    # delivered only to the controller. Best-effort; on platforms where this
    # fails we fall back to the signal.signal(SIGINT, SIG_IGN) below.
    try:
        os.setpgrp()
    except OSError:
        pass

    # Controller owns SIGINT semantics — ignore it here.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # SIGTERM is handled by start_recording's own _sigterm_handler, which
    # calls recorder.stop() for a graceful unwind.

    from screencap.recorder import DiskFullError, start_recording

    capture_dir_hint = Path(args.get("capture_dir_hint", ""))
    disk_full = False
    capture_dir: Path | None = None
    elapsed: float = 0.0

    try:
        capture_dir, elapsed, _mb_proc, _mb_state = start_recording(
            args["name"],
            description=args.get("description"),
            audio=args.get("audio"),
            output_dir=args.get("output_dir"),
            wifi_metrics=args.get("wifi_metrics"),
            app_versions=args.get("app_versions"),
            force_clean=args.get("force_clean", False),
            capture_video=args.get("capture_video"),
            capture_images=args.get("capture_images"),
            capture_window_data=args.get("capture_window_data"),
            verbose=args.get("verbose", False),
            chunk_duration=args.get("chunk_duration"),
            live_upload=args.get("live_upload", True),
            force_mode=args.get("force_mode"),
            cloud_intent=args.get("cloud_intent", False),
            keep_local=args.get("keep_local", True),
            intent_source=args.get("intent_source", "flag"),
            segmentation_mode=args.get("segmentation_mode", "llm"),
            scrub_enabled=args.get("scrub_enabled", True),
            show_on_website=args.get("show_on_website", True),
            network=args.get("network", False),
            # Worker-mode injection points ------------------------------------
            _external_window_feed_q=args["_window_feed_q"],
            _external_override_q=args["_override_q"],
            _external_disable_q=args["_disable_q"],
            _skip_menubar_spawn=True,
            _skip_pidfile=True,
            _skip_sigint_handler=True,
            network_handoff_ready=args.get("_network_handoff_ready"),
        )
    except DiskFullError as exc:
        disk_full = True
        capture_dir = exc.capture_dir
        elapsed = exc.elapsed
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            target = capture_dir or capture_dir_hint
            if target:
                target = Path(target)
                target.mkdir(parents=True, exist_ok=True)
                (target / ".recording_error.log").write_text(
                    f"{type(exc).__name__}: {exc}\n",
                )
        except Exception:
            pass
        raise

    # Completion marker — the controller polls for this to know the capture
    # finished cleanly before spawning the post-process worker.
    try:
        if capture_dir is not None:
            (capture_dir / ".recording_ready").write_text(
                json.dumps(
                    {
                        "elapsed": elapsed,
                        "completed_at": time.time(),
                        "disk_full": disk_full,
                    },
                    indent=2,
                ),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Post-Process Worker subprocess entry point
# ---------------------------------------------------------------------------


_POSTPROCESS_TIMEOUT_S = int(os.environ.get("SCREENCAP_POSTPROCESS_TIMEOUT", "900"))


def run_postprocess_worker(args: dict) -> None:
    """Post-Process Worker subprocess entry point.

    Runs the auto-export → auto-transcribe → auto-name → unclassified-apps
    pipeline that used to live inline in ``cli.py:386-450``. Detaches from
    the controller's process group and ignores ``SIGINT`` so a Ctrl+C on
    the controller's tty does not cancel post-processing.

    A SIGALRM watchdog (default 15 min, override via
    ``SCREENCAP_POSTPROCESS_TIMEOUT`` env var) aborts the pipeline if any
    stage hangs — prevents the worker from blocking session shutdown
    indefinitely when a provider / network call wedges.
    """
    try:
        os.setpgrp()
    except OSError:
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Let SIGTERM kill us (used by the controller's force-quit path).

    def _timeout_handler(signum, frame):
        raise TimeoutError(
            f"post-processing exceeded {_POSTPROCESS_TIMEOUT_S}s timeout"
        )

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(_POSTPROCESS_TIMEOUT_S)

    from screencap.session import _postprocess_pipeline  # local import

    capture_dir = Path(args["capture_dir"])
    try:
        _postprocess_pipeline(
            capture_dir=capture_dir,
            name=args["name"],
            audio=args.get("audio", True),
            output=args.get("output"),
            auto_name_enabled=args.get("auto_name_enabled", True),
            local_only=args.get("local_only", False),
            disk_full=args.get("disk_full", False),
            verbose=args.get("verbose", False),
        )
    except TimeoutError as exc:
        console.print(f"[red]Post-processing timed out:[/red] {exc}")
    finally:
        signal.alarm(0)
        try:
            (capture_dir / ".postprocess_done").write_text(
                json.dumps({"completed_at": time.time()}),
            )
        except Exception:
            pass


def _postprocess_pipeline(
    *,
    capture_dir: Path,
    name: str,
    audio: bool,
    output: str | None,
    auto_name_enabled: bool,
    local_only: bool,
    disk_full: bool,
    verbose: bool,
) -> None:
    """The post-recording pipeline: export, transcribe, auto-name, report.

    Lifted verbatim from the old inline block in ``cli.py:386-450`` so the
    Post-Process Worker subprocess preserves the current user-visible
    behaviour. Intentionally not refactored beyond what is needed to run
    outside of the CLI function's local scope.
    """
    final_name = name
    final_dir = capture_dir

    # Check for menubar rename file (written by the menubar subprocess when
    # the user edits the recording name inline).
    try:
        from screencap.menubar import RENAME_FILENAME

        rename_file = capture_dir / RENAME_FILENAME
        if rename_file.exists():
            new_name = rename_file.read_text().strip()
            if new_name and new_name != name:
                new_dir = capture_dir.parent / new_name
                if not new_dir.exists():
                    capture_dir.rename(new_dir)
                    final_name = new_name
                    final_dir = new_dir
                    capture_dir = new_dir
            rename_file.unlink(missing_ok=True)
    except Exception:
        pass

    # Auto-export events.jsonl for downstream scrubbing.
    try:
        from screencap.cli import _auto_export  # reuse CLI helper

        _auto_export(capture_dir)
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]Warning:[/yellow] auto-export failed: {exc}",
        )

    if auto_name_enabled and not disk_full and final_name == name:
        has_chunk_transcripts = any(capture_dir.glob("transcript_*.txt"))
        audio_path = capture_dir / "audio.flac"
        if (
            audio
            and not has_chunk_transcripts
            and audio_path.exists()
            and audio_path.stat().st_size >= 1024
        ):
            try:
                from screencap.cli import _auto_transcribe

                _auto_transcribe(capture_dir, audio_path)
            except Exception as exc:  # noqa: BLE001
                console.print(
                    f"[yellow]Warning:[/yellow] auto-transcribe failed: {exc}",
                )

        # LLM auto-naming may rename the capture dir
        skip_rename = output is not None
        try:
            from screencap.namer import auto_name as do_auto_name

            with console.status("[bold]Generating name...[/bold]"):
                final_dir = do_auto_name(
                    capture_dir,
                    local_only=local_only,
                    skip_rename=skip_rename,
                )
            final_name = final_dir.name
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]Warning:[/yellow] auto-name failed: {exc}",
            )

    # Summary + unclassified-apps report (best-effort, non-fatal)
    try:
        from screencap.recorder import print_summary, print_upload_followup

        # We don't have elapsed inside the post-process worker — read it
        # from .recording_ready if present.
        try:
            elapsed = float(_read_recording_ready(final_dir).get("elapsed", 0.0))
        except (TypeError, ValueError):
            elapsed = 0.0
        print_upload_followup(final_name, final_dir)
        print_summary(final_name, final_dir, elapsed)
    except Exception:
        pass

    try:
        from screencap.cli import _report_unclassified_apps

        _report_unclassified_apps(final_dir)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session Controller
# ---------------------------------------------------------------------------


class SessionController:
    """Long-lived controller process that orchestrates back-to-back recordings.

    Instantiate with the resolved CLI args from ``cli.start()``, then call
    :meth:`run`. The method blocks until the session is shut down (via the
    Quit button, ``screencap stop``, or Ctrl+C).
    """

    def __init__(self, cli_args: dict[str, Any]) -> None:
        self._cli_args = cli_args
        # ``_state`` is mutated only from the main loop thread. Signal
        # handlers and the per-worker forwarder thread read it but do
        # not write it, so no lock is required.
        self._state: SessionState = SessionState.IDLE

        self._current_worker: _RecordingWorker | None = None
        # Recording workers whose capture has stopped but whose cleanup
        # (ChunkProcessor drain, DB checkpoint, sentinel upload, etc.)
        # is still running in their subprocess. Each entry is
        # ``(worker, sigterm_sent_at)``; the main loop reaps dead ones
        # and enqueues their PPW, so stop_click returns to IDLE
        # instantly instead of blocking the controller for 30–300 s.
        self._finishing_workers: list[tuple[_RecordingWorker, float]] = []
        self._pending_jobs: "OrderedDict[str, PostProcessJob]" = OrderedDict()
        self._active_postprocess: PostProcessJob | None = None

        self._ctrl_c_count = 0
        self._shutdown_requested = False
        # Tracks whether ``_on_start_click`` has ever been called in this
        # session. Used to decide if the CLI-supplied ``--name`` should
        # apply (only to the very first recording).
        self._started_any = False

        # Effective audio state for the next recording. Seeded from
        # cli_args so ``--no-audio`` is honoured for the first recording,
        # but mutable via the menubar ``audio_toggle`` message so later
        # recordings in the same session pick up the new value without a
        # session restart.
        self._audio_effective: bool = bool(cli_args.get("audio", True))

        # Persistent menubar IPC — created once, reused for the whole
        # session. control_q carries controller → menubar messages (window
        # events, state transitions, pp status). menubar_event_q carries
        # menubar → controller messages (user clicks, app overrides).
        self._control_q: "multiprocessing.Queue" = multiprocessing.Queue()
        self._menubar_event_q: "multiprocessing.Queue" = multiprocessing.Queue()
        self._menubar_proc: "multiprocessing.Process | None" = None
        self._menubar_state_file: Path | None = None

        # Install signal handlers BEFORE spawning the menubar so a signal
        # arriving during subprocess creation is handled by us, not the
        # default terminator.
        self._install_signal_handlers()

        # Controller pidfile — points at our own PID so `screencap stop`
        # SIGTERMs us, not a worker.
        try:
            from screencap.pidfile import write_pidfile

            write_pidfile(Path("."), [])
        except Exception:
            pass
        atexit.register(self._atexit_cleanup)

        # Spawn menubar once up-front. It will initially render the IDLE
        # state; the first recording auto-starts in ``run()``.
        self._spawn_menubar_persistent()

    # ------------------------------------------------------------------ #
    # Signal handling
    # ------------------------------------------------------------------ #

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._sigint_handler)
        signal.signal(signal.SIGTERM, self._sigterm_handler)

    def _sigint_handler(self, signum: int, frame: Any) -> None:
        """Three-tap SIGINT: graceful → force → hard exit.

        Preserves the invariant from ``CLAUDE.md`` that Ctrl+C is always
        escapable regardless of what the controller is doing.

        Signal handlers must be async-signal-safe: we only set flags and
        send a SIGTERM to the current worker. All state-machine work
        happens on the next main-loop iteration.
        """
        self._ctrl_c_count += 1

        if self._ctrl_c_count == 1:
            try:
                sys.__stderr__.write(
                    "\n  \u25a0 Stopping recording and ending session"
                    " (Ctrl+C again to force quit)...\n",
                )
                sys.__stderr__.flush()
            except Exception:
                pass
            # Graceful: flag the main loop to shut down. The loop will
            # see this flag, stop the current recording (non-blocking),
            # wait for the RW cleanup + any pending PPW in bounded time,
            # then exit. Do NOT call _stop_current_worker_async here:
            # the main loop's Ctrl+C-agnostic stop path handles it.
            self._shutdown_requested = True
            return

        if self._ctrl_c_count == 2:
            try:
                sys.__stderr__.write(
                    "\n  \u26a1 Force quitting — terminating all processes...\n",
                )
                sys.__stderr__.flush()
            except Exception:
                pass
            self._force_kill_all_children()
            self._kill_menubar_proc()
            try:
                from screencap.pidfile import delete_pidfile

                delete_pidfile()
            except Exception:
                pass
            os._exit(1)

        # 3rd tap+: raw immediate exit.
        os._exit(1)

    def _sigterm_handler(self, signum: int, frame: Any) -> None:
        """SIGTERM = ``screencap stop`` = graceful shutdown equivalent to
        one SIGINT tap. Never force-kills. Only sets the flag — the main
        loop handles the state transition and worker termination."""
        self._shutdown_requested = True

    def _iter_owned_procs(self) -> "list[multiprocessing.Process]":
        """Collect every subprocess this controller owns.

        Returns the active recording worker, any recording workers
        still running their post-capture cleanup, the active
        post-process worker, and the queued post-process workers that
        have been materialised into ``Process`` objects already.
        """
        owned: list[multiprocessing.Process] = []
        if self._current_worker is not None and self._current_worker.proc is not None:
            owned.append(self._current_worker.proc)
        for finishing_rw, _ts in self._finishing_workers:
            if finishing_rw.proc is not None:
                owned.append(finishing_rw.proc)
        if self._active_postprocess is not None and self._active_postprocess.proc is not None:
            owned.append(self._active_postprocess.proc)
        for job in self._pending_jobs.values():
            if job.proc is not None:
                owned.append(job.proc)
        return owned

    def _force_kill_all_children(self) -> None:
        """Hard-kill every subprocess we own. Used by tap-2 SIGINT."""
        for proc in self._iter_owned_procs():
            pid = proc.pid
            if not pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # ------------------------------------------------------------------ #
    # Menubar lifecycle
    # ------------------------------------------------------------------ #

    def _spawn_menubar_persistent(self) -> None:
        """Spawn the persistent menu bar subprocess (once per session)."""
        try:
            from screencap.config import get_first_seen_prompt_enabled
            from screencap.menubar import _run_menubar

            # Legacy state file path — still used for STATE_DONE signalling
            # at shutdown. Placed in the screencap dotdir so it doesn't leak
            # into a specific capture.
            state_dir = Path.home() / ".screencap"
            state_dir.mkdir(parents=True, exist_ok=True)
            self._menubar_state_file = state_dir / ".menubar_state"
            try:
                self._menubar_state_file.unlink(missing_ok=True)
            except Exception:
                pass

            name_hint = self._cli_args.get("name") or "session"
            self._menubar_proc = multiprocessing.Process(
                target=_run_menubar,
                args=(
                    os.getpid(),
                    name_hint,
                    time.time(),
                    str(self._menubar_state_file),
                    None,   # window_feed_q (legacy, unused in session mode)
                    None,   # override_q (legacy, unused in session mode)
                    get_first_seen_prompt_enabled(),
                    None,   # disable_q (legacy, unused in session mode)
                ),
                kwargs={
                    "control_q": self._control_q,
                    "menubar_event_q": self._menubar_event_q,
                    "session_mode": True,
                    "audio_enabled": self._audio_effective,
                },
                daemon=True,
                name="menubar",
            )
            self._menubar_proc.start()
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]Warning:[/yellow] menubar failed to spawn: {exc}"
            )
            self._menubar_proc = None

    def _kill_menubar_proc(self) -> None:
        """Tear down the persistent menu bar subprocess.

        Delegates to :func:`screencap.recorder._kill_menubar` so the
        STATE_DONE handshake + SIGKILL sequence stays in one place.
        """
        from screencap.recorder import _kill_menubar

        _kill_menubar(self._menubar_proc, self._menubar_state_file)
        self._menubar_proc = None

    def _push_control(self, msg: dict) -> None:
        """Send a message to the menubar over the persistent control queue."""
        try:
            self._control_q.put_nowait(msg)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Capture-dir allocation
    # ------------------------------------------------------------------ #

    def _allocate_capture_dir(self, base_name: str) -> tuple[str, Path]:
        """Return a (name, capture_dir) pair guaranteed not to collide.

        Rapid back-to-back recordings can land in the same second; append
        ``-2``, ``-3``, … to the base name in that case. Raises
        :class:`RuntimeError` if more than 999 colliding names exist —
        that would require 1000 recordings in the same wall-clock
        second and indicates either a clock glitch or a caller bug.
        """
        from screencap.config import get_recordings_dir

        output_dir = self._cli_args.get("output")
        if output_dir:
            return base_name, Path(output_dir)

        base_dir = get_recordings_dir()
        candidate = base_dir / base_name
        if not candidate.exists():
            return base_name, candidate
        for i in range(2, 1000):
            new_name = f"{base_name}-{i}"
            candidate = base_dir / new_name
            if not candidate.exists():
                return new_name, candidate
        raise RuntimeError(
            f"could not allocate a free capture directory for {base_name!r} "
            f"after 1000 suffix attempts"
        )

    def _fresh_recording_name(self) -> str:
        """Generate a timestamp-based name for the next recording."""
        return f"rec-{datetime.now().strftime('%Y%m%dT%H%M%S')}"

    # ------------------------------------------------------------------ #
    # Recording worker lifecycle
    # ------------------------------------------------------------------ #

    def _on_start_click(self) -> None:
        """Handle a Start Recording click from the menubar."""
        if self._state != SessionState.IDLE:
            return
        self._state = SessionState.RECORDING

        # For the very first recording of the session, honor the
        # CLI-provided ``--name``. Subsequent recordings in the same
        # session always get a fresh timestamped name so back-to-back
        # captures don't collide on disk.
        if not self._started_any and self._cli_args.get("name"):
            base_name = self._cli_args["name"]
        else:
            base_name = self._fresh_recording_name()
        self._started_any = True

        name, capture_dir = self._allocate_capture_dir(base_name)

        queues = _RWQueues(
            window_feed_q=multiprocessing.Queue(),
            override_q=multiprocessing.Queue(),
            disable_q=multiprocessing.Queue(),
        )

        # Network handoff event — created here when --network is in play, so
        # the SessionController-side daemon thread (below) can wait on the
        # SAME mp.Event the worker signals after writing the handoff file.
        network_enabled = bool(self._cli_args.get("network", False))
        network_handoff_ready: multiprocessing.Event | None = None
        if network_enabled:
            network_handoff_ready = multiprocessing.Event()

        worker_args = {
            "name": name,
            "description": self._cli_args.get("description"),
            "audio": self._audio_effective,
            "output_dir": str(capture_dir),
            "wifi_metrics": self._cli_args.get("wifi_metrics"),
            "app_versions": self._cli_args.get("app_versions"),
            "force_clean": self._cli_args.get("force_clean", False),
            "capture_video": self._cli_args.get("capture_video"),
            "capture_images": self._cli_args.get("capture_images"),
            "capture_window_data": self._cli_args.get("capture_window_data"),
            "verbose": self._cli_args.get("verbose", False),
            "chunk_duration": self._cli_args.get("chunk_duration"),
            "live_upload": self._cli_args.get("live_upload", True),
            "force_mode": self._cli_args.get("force_mode"),
            "cloud_intent": self._cli_args.get("cloud_intent", False),
            "keep_local": self._cli_args.get("keep_local", True),
            "intent_source": self._cli_args.get("intent_source", "flag"),
            "segmentation_mode": self._cli_args.get("segmentation_mode", "llm"),
            "scrub_enabled": self._cli_args.get("scrub_enabled", True),
            "show_on_website": self._cli_args.get("show_on_website", True),
            "capture_dir_hint": str(capture_dir),
            "_window_feed_q": queues.window_feed_q,
            "_override_q": queues.override_q,
            "_disable_q": queues.disable_q,
            "network": network_enabled,
            "_network_handoff_ready": network_handoff_ready,
        }

        proc = multiprocessing.Process(
            target=run_recording_worker,
            args=(worker_args,),
            name=f"rec_worker_{name}",
        )
        proc.start()

        rw = _RecordingWorker(
            name=name,
            capture_dir=capture_dir,
            proc=proc,
            queues=queues,
            started_at=time.time(),
        )
        rw.forwarder_thread = self._start_window_forwarder(rw)
        self._current_worker = rw

        # Network handoff registration: when --network is enabled, the worker
        # writes <capture_dir>/.network_child.json after the proxy mp.Process
        # is alive; we wait on the mp.Event in a daemon thread so the menubar
        # main loop is not blocked by the 10s + DB-setup window.
        if network_enabled and network_handoff_ready is not None:
            handoff_thread = threading.Thread(
                target=self._wait_for_network_handoff,
                args=(network_handoff_ready, capture_dir, proc.pid),
                name=f"network_handoff_{name}",
                daemon=True,
            )
            handoff_thread.start()

        # Tell the menubar about the new recording.
        self._push_control({"type": "session_reset"})
        self._push_control({
            "type": "state",
            "state": SessionState.RECORDING.value,
            "name": name,
            "start_time": rw.started_at,
            "pending": len(self._pending_jobs)
            + (1 if self._active_postprocess else 0),
        })

    def _start_window_forwarder(self, rw: _RecordingWorker) -> threading.Thread:
        """Spawn a thread that forwards window events from a worker to the menubar.

        The thread exits when the worker process dies. This is the
        "mediator" from the design doc — but scoped to a single worker's
        lifetime rather than being a long-lived controller thread with a
        mutable reference.
        """

        def _forward() -> None:
            q = rw.queues.window_feed_q
            while True:
                try:
                    evt = q.get(timeout=0.5)
                except (_queue_mod.Empty, OSError):
                    if not rw.proc.is_alive():
                        return
                    continue
                except (EOFError, BrokenPipeError):
                    return
                if evt is None:
                    return
                try:
                    self._control_q.put_nowait({
                        "type": "window_event",
                        "data": evt,
                    })
                except Exception:
                    pass

        t = threading.Thread(target=_forward, name="rw_forward", daemon=True)
        t.start()
        return t

    def _reap_network_state(self, rw: "_RecordingWorker") -> None:
        """When a worker is reaped, ensure network state is clean.

        Called from ``_reap_finishing_workers`` for every exited worker.
        For workers that DID run engine teardown, ``.proxy_restored`` is
        present and this is a near-no-op (just deregisters the pidfile
        child and deletes the handoff). For workers that died unexpectedly
        (e.g. SIGKILL between handoff-write and engine teardown), this
        runs orphan-cleanup so the user's system proxy is not left
        pointing at a dead port.
        """
        from screencap import pidfile as _pidfile
        from screencap.network import lifecycle as _net_lifecycle

        capture_dir = rw.capture_dir
        proxy_restored = (capture_dir / ".proxy_restored").exists()
        handoff = _net_lifecycle.read_network_child_handoff(capture_dir)

        # If neither the marker nor the handoff exists, this was likely
        # a non-network recording — skip.
        if not proxy_restored and handoff is None:
            return

        if not proxy_restored:
            # Worker died without running engine teardown. Run orphan-cleanup.
            try:
                _net_lifecycle.restore_orphaned_proxy_state()
            except Exception:  # noqa: BLE001
                logger.exception("orphan proxy restore in reap path failed")

        # Deregister pidfile child + delete handoff (idempotent).
        try:
            _pidfile.remove_child("mitmproxy")
        except Exception:  # noqa: BLE001
            pass
        try:
            _net_lifecycle.delete_network_child_handoff(capture_dir)
        except Exception:  # noqa: BLE001
            pass

    def _wait_for_network_handoff(
        self,
        handoff_ready: multiprocessing.Event,
        capture_dir: Path,
        worker_pid: int,
    ) -> None:
        """Wait for the worker to publish .network_child.json, then register
        the proxy PID via pidfile.add_child so terminate_processes can reach
        it.

        The worker signals ``handoff_ready`` AFTER atomic-writing the handoff
        file; we wait up to 15s (covers 10s wait_for_ready + DB setup +
        slack). On timeout, fall back to reading the file directly; if it's
        absent or unparseable AND the worker is dead, run the orphan-cleanup
        restore. Daemon thread always exits within ~46s (15s wait + bounded
        validation + 30s subprocess timeout cap).
        """
        from screencap import pidfile as _pidfile
        from screencap.network import lifecycle as _net_lifecycle

        signaled = handoff_ready.wait(timeout=15.0)
        if signaled:
            handoff = _net_lifecycle.read_network_child_handoff(capture_dir)
            if handoff is None:
                logger.warning(
                    "network handoff signaled but file unreadable for %s",
                    capture_dir,
                )
                return
            proxy_pid = handoff["proxy_pid"]
            handoff_worker_pid = handoff["worker_pid"]
            proxy_create_time = _net_lifecycle.proc_create_time(proxy_pid)
            proxy_cmdline_tail = _net_lifecycle.cmdline_tail(proxy_pid)
            # F3-v9 PID-reuse defense before registering.
            if not _net_lifecycle.is_pid_alive_with_create_time(
                proxy_pid,
                expected_create_time=proxy_create_time,
                expected_cmdline_tail=proxy_cmdline_tail,
            ):
                logger.warning(
                    "network handoff: proxy PID %s failed liveness check",
                    proxy_pid,
                )
                return
            try:
                _pidfile.add_child(
                    "mitmproxy",
                    proxy_pid=proxy_pid,
                    worker_pid=handoff_worker_pid,
                    proxy_create_time=proxy_create_time,
                    proxy_cmdline_tail=proxy_cmdline_tail,
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to register proxy in recording.pid")
            return

        # Timeout — fall back to reading the file directly. If the worker
        # SIGKILLed between handoff-write and event-signal, this still works.
        handoff = _net_lifecycle.read_network_child_handoff(capture_dir)
        if handoff is None:
            # Worker may be dead and never wrote the file. Run orphan-cleanup
            # so the user's system proxy is not left pointing at a dead port.
            try:
                import psutil as _psutil

                worker_alive = _psutil.pid_exists(worker_pid)
            except Exception:  # noqa: BLE001
                worker_alive = False
            if not worker_alive:
                try:
                    _net_lifecycle.restore_orphaned_proxy_state()
                except Exception:  # noqa: BLE001
                    logger.exception("orphan proxy restore failed")

    def _on_stop_click(self) -> None:
        """Handle a Stop Recording click from the menubar.

        **Non-blocking.** Sends SIGTERM to the current recording worker,
        moves it to the ``_finishing_workers`` list, and transitions the
        session state to IDLE immediately so the menu bar re-enables the
        "Start Recording" button right away. The recording worker's slow
        cleanup (ChunkProcessor drain, DB checkpoint, sentinel upload)
        continues in the background; when the worker actually exits, the
        main loop's :meth:`_reap_finishing_workers` reaps it and
        enqueues its post-process job.
        """
        if self._state != SessionState.RECORDING:
            return
        self._state = SessionState.STOPPING

        self._push_control({"type": "state", "state": SessionState.STOPPING.value})

        rw = self._current_worker
        if rw is not None and rw.proc is not None:
            try:
                rw.proc.terminate()  # SIGTERM → graceful recorder.stop()
            except Exception:
                pass
            self._finishing_workers.append((rw, time.time()))
        self._current_worker = None

        # Flip to IDLE right away — the user must see the menubar return
        # to "▶ Start Recording" as soon as the capture is halted, even
        # though the worker subprocess is still finalizing in the
        # background.
        self._state = SessionState.IDLE
        self._push_control({
            "type": "state",
            "state": SessionState.IDLE.value,
            "pending": self._total_pending_count(),
        })

    def _total_pending_count(self) -> int:
        """Total number of in-flight post-capture jobs.

        Includes recording workers still running their post-capture
        cleanup, the active post-process worker (if any), and any
        post-process jobs queued behind it.
        """
        return (
            len(self._finishing_workers)
            + (1 if self._active_postprocess else 0)
            + len(self._pending_jobs)
        )

    def _reap_finishing_workers(self) -> None:
        """Reap recording workers that have completed their post-capture cleanup.

        Called once per main-loop iteration. For each worker that has
        exited, enqueue its post-process job and drop it from the
        finishing list. Workers that linger past a very generous 10-minute
        deadline (e.g. a wedged ChunkProcessor or stuck network upload)
        are force-killed so the pending list can't grow without bound.
        """
        if not self._finishing_workers:
            return
        still_finishing: list[tuple[_RecordingWorker, float]] = []
        reaped: list[_RecordingWorker] = []
        for rw, sigterm_ts in self._finishing_workers:
            if rw.proc is None or not rw.proc.is_alive():
                try:
                    if rw.proc is not None:
                        rw.proc.join(timeout=1.0)
                except Exception:
                    pass
                reaped.append(rw)
                continue
            if time.time() - sigterm_ts > 600:  # 10 min deadline
                console.print(
                    f"[yellow]Warning:[/yellow] recording worker "
                    f"{rw.name!r} exceeded 10 min shutdown deadline — "
                    "force-killing."
                )
                try:
                    rw.proc.kill()
                except Exception:
                    pass
                try:
                    rw.proc.join(timeout=5.0)
                except Exception:
                    pass
                reaped.append(rw)
                continue
            still_finishing.append((rw, sigterm_ts))
        self._finishing_workers = still_finishing

        for rw in reaped:
            # Network teardown backstop: if the worker exited without writing
            # .proxy_restored, run orphan-cleanup so the user's system proxy
            # is not left pointing at a dead port. Then deregister the
            # mitmproxy child from the pidfile + delete the handoff file.
            self._reap_network_state(rw)

            self._enqueue_postprocess_for_worker(rw)
            # Wait briefly for the forwarder thread to notice the dead
            # worker and exit on its own, then release the per-recording
            # queue semaphores so the resource tracker doesn't warn at
            # shutdown. A stalled forwarder is non-fatal — we use
            # cancel_join_thread() inside ``_close_queues_safely``.
            if rw.forwarder_thread is not None:
                try:
                    rw.forwarder_thread.join(timeout=1.0)
                except Exception:
                    pass
            _close_queues_safely(
                rw.queues.window_feed_q,
                rw.queues.override_q,
                rw.queues.disable_q,
            )
            self._push_control({
                "type": "pp_status",
                "name": rw.name,
                "pending": self._total_pending_count(),
            })

    def _enqueue_postprocess_for_worker(self, rw: _RecordingWorker) -> None:
        """Build a :class:`PostProcessJob` from a finished worker and queue it."""
        disk_full = bool(_read_recording_ready(rw.capture_dir).get("disk_full", False))

        job = PostProcessJob(
            name=rw.name,
            capture_dir=rw.capture_dir,
            args={
                "capture_dir": str(rw.capture_dir),
                "name": rw.name,
                "audio": self._cli_args.get("audio", True),
                "output": self._cli_args.get("output"),
                "auto_name_enabled": self._cli_args.get(
                    "auto_name_enabled", True,
                ),
                "local_only": self._cli_args.get("local_only", False),
                "disk_full": disk_full,
                "verbose": self._cli_args.get("verbose", False),
            },
        )
        self._pending_jobs[rw.name] = job

    # ------------------------------------------------------------------ #
    # Post-process scheduling
    # ------------------------------------------------------------------ #

    def _maybe_start_next_postprocess(self) -> None:
        """Start the next pending post-process job iff no job is running."""
        if self._active_postprocess is not None:
            return
        if not self._pending_jobs:
            return
        name, job = self._pending_jobs.popitem(last=False)
        proc = multiprocessing.Process(
            target=run_postprocess_worker,
            args=(job.args,),
            name=f"pp_worker_{name}",
        )
        proc.start()
        job.proc = proc
        job.started_at = time.time()
        self._active_postprocess = job
        self._push_control({
            "type": "pp_status",
            "name": name,
            "pending": self._total_pending_count(),
        })

    def _reap_completed_postprocess(self) -> None:
        """Reap the active post-process job if it has exited."""
        job = self._active_postprocess
        if job is None or job.proc is None:
            return
        if job.proc.is_alive():
            return
        job.proc.join(timeout=1.0)
        self._active_postprocess = None
        self._push_control({
            "type": "pp_done",
            "name": job.name,
            "pending": self._total_pending_count(),
        })

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #

    def _on_quit_click(self) -> None:
        """Handle a Quit click from the menubar.

        Just sets the shutdown flag. The main loop takes care of
        stopping the current recording (if any), reaping finishing
        workers, and draining pending PPWs inside :meth:`_do_shutdown`.
        """
        self._shutdown_requested = True

    def _do_shutdown(self) -> None:
        """Drain finishing workers + pending PPWs, then tear everything down."""
        deadline = time.time() + 300  # 5 min bounded wait

        # Step 1: wait for any recording workers that are still running
        # their post-capture cleanup to finish exiting. Each iteration
        # also pumps the PPW scheduler so previously-finished workers
        # get their PPW started early.
        while self._finishing_workers and time.time() < deadline:
            self._reap_finishing_workers()
            self._reap_completed_postprocess()
            self._maybe_start_next_postprocess()
            time.sleep(0.5)

        # Step 2: let the active PPW finish (if any) and drain the
        # pending queue sequentially.
        while (
            (self._active_postprocess is not None or self._pending_jobs)
            and time.time() < deadline
        ):
            self._reap_completed_postprocess()
            self._maybe_start_next_postprocess()
            time.sleep(0.5)

        # Force-kill any laggards that outlived the bounded wait, AND
        # release their per-recording queue semaphores on the way out.
        # Without this, the resource tracker prints a big "leaked
        # semaphore objects" warning as the interpreter shuts down.
        for rw, _ts in list(self._finishing_workers):
            if rw.proc is not None and rw.proc.is_alive():
                try:
                    rw.proc.kill()
                except Exception:
                    pass
            if rw.forwarder_thread is not None:
                try:
                    rw.forwarder_thread.join(timeout=1.0)
                except Exception:
                    pass
            _close_queues_safely(
                rw.queues.window_feed_q,
                rw.queues.override_q,
                rw.queues.disable_q,
            )
        self._finishing_workers = []

        if self._active_postprocess is not None:
            console.print(
                "[yellow]Warning:[/yellow] post-processing exceeded 5 min — "
                "force-killing background worker.",
            )
            try:
                self._active_postprocess.proc.kill()  # type: ignore[union-attr]
            except Exception:
                pass

        # Menu bar must be killed BEFORE we close the persistent queues —
        # the menubar subprocess is a consumer of ``_control_q``, and
        # closing that queue while the menubar is still reading from it
        # would raise spurious EOFError in the child.
        self._kill_menubar_proc()

        _close_queues_safely(self._control_q, self._menubar_event_q)

        try:
            from screencap.pidfile import delete_pidfile

            delete_pidfile()
        except Exception:
            pass

    def _atexit_cleanup(self) -> None:
        """Best-effort cleanup on interpreter exit."""
        try:
            for child in multiprocessing.active_children():
                try:
                    child.terminate()
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Menubar event dispatch
    # ------------------------------------------------------------------ #

    def _dispatch_menubar_msg(self, msg: dict) -> None:
        """Handle a single message off the menubar_event_q."""
        mtype = msg.get("type")
        if mtype == "start_click":
            self._on_start_click()
        elif mtype == "stop_click":
            self._on_stop_click()
        elif mtype == "quit_click":
            self._on_quit_click()
        elif mtype == "override":
            # Forward to the active recording worker's override_q.
            rw = self._current_worker
            if rw is not None:
                try:
                    rw.queues.override_q.put_nowait(msg.get("data", msg))
                except Exception:
                    pass
        elif mtype == "disable":
            rw = self._current_worker
            if rw is not None:
                try:
                    rw.queues.disable_q.put_nowait(msg.get("data", msg))
                except Exception:
                    pass
        elif mtype == "audio_toggle":
            # The menubar already wrote config.toml and updated its own
            # display; we mirror the new value here so the next
            # ``_on_start_click`` picks it up within this session.
            try:
                self._audio_effective = bool(msg.get("value", True))
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Main event loop
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Run the controller until shutdown.

        The main loop's responsibilities are:

        1. Honor a pending shutdown request — if we're still RECORDING,
           kick off a (non-blocking) stop; if we're already IDLE, break
           out and run ``_do_shutdown``.
        2. Dispatch one menu-bar event per iteration.
        3. Reap finishing recording workers and start / reap
           post-process workers (async housekeeping).
        4. Detect a worker that died on its own (disk-full, crash) and
           move it into the finishing list.
        """
        # Auto-start the first recording so ``screencap start`` preserves
        # its current UX: one command → recording begins immediately.
        self._on_start_click()

        while True:
            # ---- Shutdown-request dispatch ----
            # Done at the top of the loop so a SIGINT / SIGTERM /
            # quit_click received at any point is handled synchronously
            # in a well-defined place (not inside a signal handler).
            if self._shutdown_requested:
                if self._state == SessionState.RECORDING:
                    self._on_stop_click()  # non-blocking
                if self._state == SessionState.IDLE:
                    break

            # ---- Drain one menubar event ----
            try:
                msg = self._menubar_event_q.get(timeout=0.5)
            except (_queue_mod.Empty, OSError):
                msg = None
            except (EOFError, BrokenPipeError):
                msg = None

            if msg is not None:
                try:
                    self._dispatch_menubar_msg(msg)
                except Exception as exc:  # noqa: BLE001
                    console.print(
                        f"[yellow]Warning:[/yellow] controller dispatch"
                        f" error: {exc}",
                    )

            # ---- Housekeeping (skipped when idle) ----
            # When no recording workers are finishing up and no PPW is
            # active or queued, there is nothing to reap or start — skip
            # the three no-op calls to keep the idle loop cheap.
            if (
                self._finishing_workers
                or self._active_postprocess is not None
                or self._pending_jobs
            ):
                try:
                    self._reap_finishing_workers()
                    self._reap_completed_postprocess()
                    self._maybe_start_next_postprocess()
                except Exception:
                    pass

            # ---- Worker-died-on-its-own detection ----
            # disk_full or engine crash: the worker exits but no stop
            # click was issued, so state is still RECORDING. Move it
            # into the finishing list and flip to IDLE so the menubar
            # reflects reality.
            rw = self._current_worker
            if (
                self._state == SessionState.RECORDING
                and rw is not None
                and rw.proc is not None
                and not rw.proc.is_alive()
            ):
                self._finishing_workers.append((rw, time.time()))
                self._current_worker = None
                self._state = SessionState.IDLE
                self._push_control({
                    "type": "state",
                    "state": SessionState.IDLE.value,
                    "pending": self._total_pending_count(),
                })

        self._do_shutdown()
