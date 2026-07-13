"""Recording Worker entry point for daemon-spawned capture subprocesses.

:func:`run_recording_worker` is the in-process body of the hidden
``screencap _engine-worker`` CLI command, which the daemon supervisor
spawns as a subprocess for each recording. It preflights the Screen
Recording permission, invokes :func:`screencap.recorder.start_recording`
with worker-mode policies, and reports the outcome through filesystem
sidecars (``.recording_ready`` on clean exit, ``.recording_error.log``
on exception) so the wrapping ``_engine-worker`` command can emit the
correct ``recording_finalized`` event without sharing Python state
across the process boundary.
"""

# ruff: noqa: I001
#
# The import order in this file is load-bearing: ``screencap._startup``
# must be imported BEFORE ``multiprocessing`` so its PYTHONWARNINGS
# filter is installed before the resource_tracker subprocess is
# lazily spawned. Auto-sorting the imports would break this invariant.

from __future__ import annotations

from screencap import _startup  # noqa: F401

import json
import os
import signal
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Recording Worker subprocess entry point
# ---------------------------------------------------------------------------


def run_recording_worker(args: dict) -> None:
    """Recording Worker subprocess entry point.

    Detaches from the parent's process group so the tty's SIGINT does
    not reach us, then invokes
    :func:`screencap.recorder.start_recording` with worker-mode flags set.

    Writes ``.recording_ready`` (on clean exit) or ``.recording_error.log``
    (on exception) into the capture_dir so the caller can observe the
    outcome without having to share Python state across the process
    boundary.
    """
    # Detach from the parent's process group so Ctrl+C on the tty is
    # not delivered to this worker. Best-effort; on platforms where this
    # fails we fall back to the signal.signal(SIGINT, SIG_IGN) below.
    try:
        os.setpgrp()
    except OSError:
        pass

    # The daemon owns stop semantics (SIGTERM) — ignore SIGINT here.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # SIGTERM is handled by start_recording's own _sigterm_handler, which
    # calls recorder.stop() for a graceful unwind.

    from screencap.engine.disk_policy import Noop as DiskNoop
    from screencap.engine.lock_policy import InheritLock
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.network_policy import Null as NetworkNull
    from screencap.engine.permission_policy import FreshScreenWatch
    from screencap.engine.screen_recorder import IpcChannels, SigtermOnly
    from screencap.recorder import DiskFullError, start_recording

    # Fail-fast Screen Recording preflight (daemon-spawn path only).
    # The engine policy is `FreshScreenWatch`, whose `preflight` is a no-op
    # (it governs only the mid-recording window, SCR-106), so without THIS
    # startup check the recording loop would enter `screen_event_reader`
    # (20 fps) and trigger a fresh TCC prompt on every `screencapture` /
    # Quartz call when the daemon binary's code identity is not authorized.
    #
    # In-process `Quartz.CGPreflightScreenCaptureAccess` is correct here
    # despite the per-process TCC cache: this worker is freshly spawned
    # and has not yet made any TCC-touching call, so the first preflight
    # reads live state. The previous subprocess-based `_check_permission_fresh`
    # path returned `None` in the bundled daemon because the bundled
    # CLI's Click entry point rejects `sys.executable -c "<code>"` with
    # a UsageError — making the gate a no-op in the frozen-binary path
    # and reproducing the infinite-prompt symptom (SCR-69 smoke test).
    # A PyObjC import / call failure is fail-open: better to let the
    # engine's existing handling deal with it than kill a recording on
    # a transient Quartz hiccup.
    if sys.platform == "darwin":
        from screencap._stderr_events import EVENT_PERMISSION_LOST, emit_event

        granted: bool | None
        try:
            import Quartz
            granted = bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            granted = None

        if granted is False:
            emit_event(EVENT_PERMISSION_LOST, permission="screen_recording", elapsed=0.0)
            raise SystemExit(3)

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
            ambient=args.get("ambient", False),
            # Worker-mode injection points ------------------------------------
            _channels=IpcChannels(
                window_feed=args["_window_feed_q"],
                override=args["_override_q"],
                disable=args["_disable_q"],
            ),
            _menubar_policy=MenubarNoop(),
            _signal_policy=SigtermOnly(),
            _lock_policy=InheritLock(),
            _permission_policy=FreshScreenWatch(),
            _disk_policy=DiskNoop(),
            _network_policy=NetworkNull(),
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

    # Completion marker — the `_engine-worker` wrapper reads this to know
    # the capture finished cleanly and to populate `recording_finalized`.
    try:
        if capture_dir is not None:
            # Merge the recorder's stop-meta sidecar (force_stopped +
            # terminated_reason) into the manifest so the `_engine-worker`
            # wrapper's `recording_finalized` event carries the correct
            # fields (todo 002, 009).
            stop_meta: dict = {}
            try:
                meta_path = capture_dir / ".recording_stop_meta.json"
                if meta_path.exists():
                    stop_meta = json.loads(meta_path.read_text() or "{}")
            except Exception:
                pass
            ready_payload = {
                "elapsed": elapsed,
                "completed_at": time.time(),
                "disk_full": disk_full,
                "force_stopped": bool(stop_meta.get("force_stopped", False)),
                "terminated_reason": stop_meta.get("terminated_reason"),
            }
            (capture_dir / ".recording_ready").write_text(
                json.dumps(ready_payload, indent=2),
            )
    except Exception:
        pass
