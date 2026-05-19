"""``engine.ScreenRecorder`` seam.

Owns the recording lifecycle: ``.run()`` dispatches into
``_run_screen_recorder`` (also in this module) which orchestrates setup,
the live loop, and teardown through the six policy axes (signal, lock,
menubar, permission, disk, network). ``screencap/recorder.py`` is a CLI
adapter holding the banner, ``Live`` display, summary printing, the
privacy-settings deep-link helper, and the ``start_recording`` shim.

The full design lives in
``docs/decisions/0001-engine-screen-recorder-seam.md``. Everything in
this file should reflect that ADR; if they disagree, update one or both
deliberately.
"""

from __future__ import annotations

import atexit
import json
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol

from rich.live import Live
from rich.text import Text

from screencap.engine.config import RecordingConfig

if TYPE_CHECKING:
    from screencap.privacy.policy import PrivacyMode

IntentSource = Literal[
    "flag", "non_interactive_default", "prompt", "config_default", "menubar",
]
SegmentationMode = Literal["llm", "idle"]


class RecordingError(Exception):
    """Base for all seam-raised exceptions. Programming bugs propagate unchanged."""


class PreflightError(RecordingError):
    """Recording never started. CLI adapter translates to a non-zero exit."""


class OrphanProcessesFound(PreflightError):
    """``--force-clean`` is required to terminate orphaned recorder processes."""


class PermissionsMissing(PreflightError):
    """One or more TCC permissions (Screen Recording / Accessibility / Input) absent."""


class NetworkPreflightFailed(PreflightError):
    """V1.5 KEK/DEK setup or mitm proxy bring-up failed before recording started."""


class DiskTooLowAtStart(PreflightError):
    """Free disk space below the configured ``stop_mb`` threshold at startup."""


class RecordingInterrupted(RecordingError):
    """Stopped mid-recording. Subclasses set ``capture_dir`` + ``elapsed`` in ``__init__``."""


class PermissionRevoked(RecordingInterrupted):
    """A required TCC permission was revoked during recording."""

    def __init__(self, missing: str) -> None:
        super().__init__(missing)
        self.missing = missing


class Monitor(Protocol):
    """Shared shape for ``PermissionPolicy`` and ``DiskPolicy``.

    Both observe an external condition on a cadence, raise
    ``PreflightError`` at startup if absent, and ``RecordingInterrupted``
    if the condition disappears mid-recording.
    """

    def preflight(self) -> None: ...

    def poll(self, now: float) -> None: ...

    @property
    def next_poll_at(self) -> float: ...


SignalHandler = Callable[[int, Any], None]


class SignalPolicy(Protocol):
    """SIGINT / SIGTERM handler installation. ``ThreeTapSigint`` | ``SigtermOnly`` | ``NoopSignalPolicy``.

    The policy is install/uninstall plumbing only — handler bodies stay
    in ``_run_screen_recorder`` because they close over ``nonlocal``
    state (``_stop_event``, ``_ctrl_c_count``, ``_child_pids``,
    ``_menubar_proc``). The seam is "should the runtime register these
    handlers at all", not "what does the handler do" — the latter is
    the same for every caller.
    """

    def install(
        self,
        *,
        sigint_handler: SignalHandler,
        sigterm_handler: SignalHandler,
    ) -> None: ...

    def uninstall(self) -> None: ...


class ThreeTapSigint:
    """Standalone-CLI signal policy: register SIGINT + SIGTERM verbatim.

    The 3-tap escalation lives inside the SIGINT handler itself
    (1 = graceful, 2 = force, 3+ = ``os._exit(1)``). This policy just
    decides *whether* to install the runtime's handlers — it doesn't
    own the escalation semantics.

    ``uninstall`` mirrors the restoration that
    ``_run_screen_recorder``'s ``finally`` block performs today:
    ``SIGINT`` → ``default_int_handler``, ``SIGTERM`` → ``SIG_DFL``.
    """

    def install(
        self,
        *,
        sigint_handler: SignalHandler,
        sigterm_handler: SignalHandler,
    ) -> None:
        signal.signal(signal.SIGINT, sigint_handler)
        signal.signal(signal.SIGTERM, sigterm_handler)

    def uninstall(self) -> None:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


class NoopSignalPolicy:
    """Test-only signal policy: leave SIGINT/SIGTERM untouched.

    Used by in-process parity / network tests that must not register
    runtime signal handlers. Production session workers use
    ``SigtermOnly`` instead — they need the SIGTERM graceful-stop
    handler.
    """

    def install(
        self,
        *,
        sigint_handler: SignalHandler,
        sigterm_handler: SignalHandler,
    ) -> None:
        pass

    def uninstall(self) -> None:
        pass


class SigtermOnly:
    """Session-worker signal policy: install SIGTERM, leave SIGINT alone.

    Workers spawned by ``SessionController`` already SIG_IGN SIGINT at
    entry (the controller owns Ctrl+C and forwards it as SIGTERM via
    ``Process.terminate()``). The runtime still needs the SIGTERM
    handler installed so ``recorder.stop()`` runs the chunk/scrub drain
    + sentinel write before the worker exits.
    """

    def install(
        self,
        *,
        sigint_handler: SignalHandler,
        sigterm_handler: SignalHandler,
    ) -> None:
        signal.signal(signal.SIGTERM, sigterm_handler)

    def uninstall(self) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


class LockPolicy(Protocol):
    """Pidfile lifecycle + per-recording identity files. ``InheritLock``.

    Bundle owned by SCR-40. Post-Phase-2 the daemon supervisor owns the
    process-exclusive pidfile claim itself, so the engine subprocess
    runs with ``InheritLock`` — ``claim`` / ``register_children`` /
    ``release`` are no-ops; ``write_identity`` still emits
    ``.recording_id`` + ``.recording_intent`` for downstream catalog /
    upload / scrubber / recovery consumers. The concrete protocol
    methods live alongside the implementation in
    ``screencap.engine.lock_policy``.
    """


class MenubarPolicy(Protocol):
    """Menubar subprocess + queue lifecycle. ``SpawnNewMenubar`` | ``Noop``."""


class PermissionPolicy(Monitor, Protocol):
    """TCC permission preflight + revocation polling. ``MacOSTCC`` | ``Noop``."""


class DiskPolicy(Monitor, Protocol):
    """Disk-space preflight + adaptive polling. ``MonitorAndStop`` | ``Noop``."""


class NetworkPolicy(Protocol):
    """V1.5 KEK/DEK + mitm preflight + proxy lifecycle. ``MitmProxyV15`` | ``Noop``."""


@dataclass(frozen=True, slots=True)
class RecordingRequest:
    """What kind of recording is this."""

    name: str
    config: RecordingConfig
    description: str | None = None
    cloud_intent: bool = False
    keep_local: bool = True
    intent_source: IntentSource = "flag"
    segmentation_mode: SegmentationMode = "llm"
    scrub_enabled: bool = True
    show_on_website: bool = True


@dataclass(frozen=True, slots=True)
class IpcChannels:
    """How this recording talks to its environment."""

    window_feed: mp.Queue
    override: mp.Queue
    disable: mp.Queue

    @classmethod
    def create(cls) -> IpcChannels:
        return cls(mp.Queue(), mp.Queue(), mp.Queue())


@dataclass(frozen=True, slots=True)
class RecordingPolicies:
    """How this recording behaves.

    No defaults — production must be explicit. Defaulting any field to
    ``Noop`` would silently disable lock/signal/privacy. Tests get an
    ``all_noop_policies()`` helper outside the production package.
    """

    signal: SignalPolicy
    lock: LockPolicy
    menubar: MenubarPolicy
    permission: PermissionPolicy
    disk: DiskPolicy
    network: NetworkPolicy


@dataclass(frozen=True, slots=True)
class RecordingResult:
    """Returned by ``ScreenRecorder.run()`` on clean stop."""

    capture_dir: Path
    elapsed: float


@dataclass(frozen=True, slots=True)
class LegacyOptions:
    """Hold-pen for ``start_recording`` kwargs not yet promoted into a policy.

    Fields here have not found a home in ``RecordingRequest`` /
    ``RecordingPolicies`` yet; the body reads them verbatim. Adding a
    field here is a temporary expedient — preferred direction is to
    promote it into a typed bundle.
    """

    audio: bool | None = None
    output_dir: str | Path | None = None
    wifi_metrics: bool | None = None
    app_versions: bool | None = None
    force_clean: bool = False
    capture_video: bool | None = None
    capture_images: bool | None = None
    capture_window_data: bool | None = None
    verbose: bool = False
    chunk_duration: float | None = None
    live_upload: bool = True
    force_mode: PrivacyMode | None = None
    network_handoff_ready: Any | None = None


class ScreenRecorder:
    """Recording seam.

    Intended use::

        rec = ScreenRecorder(request=..., channels=..., policies=...)
        result = rec.run()  # blocks until stop; setup + teardown live in run()

    Setup and teardown are centralized inside ``.run()`` (the body in
    ``_run_screen_recorder``); the class itself is just a typed bundle
    holder. See the ADR for the full lifecycle contract.

    ``legacy=`` is a hold-pen for kwargs that have not yet been
    promoted into a policy axis.
    """

    def __init__(
        self,
        *,
        request: RecordingRequest,
        channels: IpcChannels,
        policies: RecordingPolicies,
        legacy: LegacyOptions | None = None,
    ) -> None:
        self._request = request
        self._channels = channels
        self._policies = policies
        self._legacy = legacy if legacy is not None else LegacyOptions()

    def run(self) -> RecordingResult:
        return _run_screen_recorder(self)


def _run_screen_recorder(rec: "ScreenRecorder") -> "RecordingResult":
    """Body of the recording lifecycle, driven by ``ScreenRecorder.run()``.

    SCR-45 inlines the body that previously lived in ``screencap.recorder``
    so the wrapper shrinks to a CLI adapter. CLI-side helpers (``console``,
    ``_print_banner``, ``_suppress_output``, ``_build_live_display``, the
    ``get_*`` config getters, ``DiskFullError``) are looked up via
    ``screencap.recorder`` to preserve test patches that target that
    namespace (``mock.patch("screencap.recorder.console")`` and similar).
    """
    # Deferred imports keep ``screencap --help`` fast and let tests patch
    # the recorder module's attributes (``console``, config getters, etc.)
    # — name lookup happens at call time inside this function.
    from screencap.recorder import (
        DiskFullError,
        _build_live_display,
        _print_banner,
        _restore_output,
        _suppress_output,
        console,
        get_app_versions,
        get_audio_default,
        get_recordings_dir,
        get_wifi_metrics,
    )

    request = rec._request
    legacy = rec._legacy
    signal_policy = rec._policies.signal
    lock_policy = rec._policies.lock
    menubar_policy = rec._policies.menubar
    permission_policy = rec._policies.permission
    disk_policy = rec._policies.disk
    network_policy = rec._policies.network
    channels = rec._channels
    name = request.name
    description = request.description
    audio = legacy.audio
    output_dir = legacy.output_dir
    wifi_metrics = legacy.wifi_metrics
    app_versions = legacy.app_versions
    force_clean = legacy.force_clean
    capture_video = legacy.capture_video
    capture_images = legacy.capture_images
    capture_window_data = legacy.capture_window_data
    verbose = legacy.verbose
    chunk_duration = legacy.chunk_duration
    cloud_intent = request.cloud_intent
    show_on_website = request.show_on_website
    network_handoff_ready = legacy.network_handoff_ready

    if audio is None:
        audio = get_audio_default()
    if wifi_metrics is None:
        wifi_metrics = get_wifi_metrics()
    if app_versions is None:
        app_versions = get_app_versions()

    # Resolve chunk duration from CLI flag or config
    if chunk_duration is None:
        from screencap.config import get_chunk_duration
        chunk_duration = get_chunk_duration()
    chunking_enabled = chunk_duration > 0

    if output_dir:
        capture_dir = Path(output_dir)
    else:
        capture_dir = get_recordings_dir() / name

    # bind() needs the resolved capture_dir; capture_dir is computed from
    # request + legacy options, so policy construction can't pre-bind.
    disk_policy.bind(capture_dir)

    # Post-Phase-2: the daemon supervisor already owns the pidfile claim
    # (see ``daemon/supervisor.py``), so ``InheritLock.claim`` is a no-op.
    # The call is preserved for the seam — a future ``LockPolicy`` variant
    # from the engine-topology spike may need to do real work here.
    lock_policy.claim(capture_dir, force_clean=force_clean)

    if capture_dir.exists() and any(capture_dir.iterdir()):
        console.print(
            f"[red]Error:[/red] Directory already exists and is not empty: {capture_dir}"
        )
        raise SystemExit(1)

    # _DiskSpaceCritical is reserved for the live-loop branch below;
    # preflight raises DiskTooLowAtStart instead.
    from screencap.engine.disk_policy import DiskSpaceCritical as _DiskSpaceCritical
    try:
        disk_policy.preflight()
    except DiskTooLowAtStart as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    capture_dir.mkdir(parents=True, exist_ok=True)

    # Suppress loguru/tqdm noise unless --verbose.
    # Must happen BEFORE any screencap.engine import (including the
    # screen-recording permission check below) so the env var is set
    # when the module-level loguru config runs for the first time.
    if not verbose:
        _suppress_output()

    permission_policy.preflight()

    desc = description or ""

    _print_banner()

    if verbose:
        console.print(f"[dim]Audio: {'on' if audio else 'off'}[/dim]")

    # ``t0`` is the reference for the live timer / summary duration.
    # We seed it here so it's defined for early-exit paths, but it's
    # reset to the actual engine-ready moment inside the ``with
    # Recorder(...)`` block below — see the comment there for why.
    t0 = time.time()
    status = console.status("[bold]Initializing capture...[/bold]")
    status.start()

    # Heavy import — deferred here to keep `screencap --help` fast.
    # Function-local try/except preserves the headless-friendly fallback
    # after the engine package's eager `Recorder` re-export was removed.
    try:
        from screencap.engine.recorder import Recorder
    except ImportError:
        Recorder = None

    if Recorder is None:
        status.stop()
        console.print(
            "[red]Error:[/red] Recorder not available. "
            "Check that all dependencies are installed (pynput, mss, etc.)"
        )
        raise SystemExit(1)

    # SpawnNewMenubar owns new queues (standalone CLI); Noop reuses the
    # controller's queues (session worker).
    _menubar_disable_q = channels.disable

    # Capture-time privacy enforcement: cloud_intent forces PUBLIC mode,
    # the window-data gate makes the filter unconstructable without window
    # events. The wrapper owns the console UX around config-load failure
    # and the cloud privacy notice; helper.build_recorder_privacy_filter
    # owns construction.
    from screencap.engine.collaborators import RecordingCollaborators

    _collaborators = RecordingCollaborators(
        request=request, legacy=legacy, channels=channels,
    )
    screen_filter = None
    privacy_config = None
    _override_file = capture_dir / ".menubar_overrides.json"
    try:
        from screencap.engine.config import config as _engine_config

        _effective_window_data = (
            capture_window_data
            if capture_window_data is not None
            else _engine_config.RECORD_WINDOW_DATA
        )
        screen_filter, privacy_config, _override_file = (
            _collaborators.build_recorder_privacy_filter(
                capture_dir=capture_dir,
                capture_window_data=_effective_window_data,
            )
        )
        if not _effective_window_data:
            console.print(
                "[yellow]Warning:[/yellow] Capture-time privacy enforcement "
                "requires window data. Disabled because window data capture is off."
            )
        elif verbose and privacy_config is not None:
            console.print(f"[dim]Privacy mode: {privacy_config.mode.value}[/dim]")
    except Exception as e:
        # Cloud-bound recordings cannot proceed without the recorder filter.
        # ``build_recorder_privacy_filter`` publishes the resolved config on
        # the helper before constructing the filter, so a filter-construction
        # exception still leaves ``_collaborators.privacy_config`` populated
        # (the user explicitly chose a privacy posture — honour it).
        # ``cloud_intent`` is the unconditional floor.
        privacy_config = privacy_config or _collaborators.privacy_config
        if cloud_intent or privacy_config is not None:
            console.print(
                f"[red]Error:[/red] Capture-time privacy enforcement failed: {e!r}\n"
                "Recording cannot proceed without privacy protection. "
                "Check dependencies and configuration."
            )
            raise SystemExit(1)
        console.print(
            f"[yellow]Warning:[/yellow] Capture-time privacy enforcement disabled: {e!r}"
        )

    # --- Cloud recording privacy warning ---
    if cloud_intent and screen_filter is not None:
        console.print()
        console.print("[bold yellow]⚠ Cloud Recording Privacy Notice[/bold yellow]")
        console.print("This recording will be uploaded. Privacy protections active:")
        console.print("  • Sensitive apps (email, chat, banking, passwords) are automatically blocked")
        console.print("  • Code editors and admin consoles are captured; keystroke text is scrubbed for PII before upload")
        console.print("  • Audio continues recording during all intervals, including blocked apps")
        console.print("  [dim]Avoid displaying passwords, API keys, or personal information on screen.[/dim]")
        console.print()

    if cloud_intent:
        if show_on_website:
            console.print("[dim]This recording will be visible on the website (use --unlisted to hide)[/dim]")
        else:
            console.print("[dim]This recording will not be visible on the website (change in screencap settings)[/dim]")

    try:
        from screencap.metrics import save_metrics

        save_metrics(capture_dir, "start", wifi_metrics=wifi_metrics, app_versions=app_versions)
    except Exception as e:
        if verbose:
            console.print(f"[yellow]Warning:[/yellow] Could not collect system metrics: {e}")

    def _cleanup_children():
        for child in mp.active_children():
            child.terminate()

    atexit.register(_cleanup_children)

    # Track how stop happened for messaging after Live exits
    _stop_reason = ""  # "graceful", "force", "disk_full", "sigterm", or "interrupt"
    _stop_event = threading.Event()
    _sentinel_uploaded = False
    _recording_name = name  # default; may be overridden by .recording_id later
    _saved_stdout = None  # Will hold real stdout when we redirect to devnull
    _saved_stderr = None  # Will hold real stderr when we redirect to devnull

    # Disk check state — disk_policy tracks cadence and warning internally.
    disk_warning = ""
    _disk_free_at_stop = 0.0

    # Pre-initialize for signal handler closures (handlers installed before
    # Recorder.__enter__ so signals work during the entire setup window).
    recorder = None
    _child_pids = []
    _ctrl_c_count = 0

    # MitmProxyV15.setup acquires the network lock + preflights mitm +
    # generates KEK/DEK; teardown() releases the lock. Null is a no-op.
    try:
        _network_material = network_policy.setup(capture_dir, privacy_config)
    except NetworkPreflightFailed as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc

    try:
        # Build Recorder kwargs, only passing non-None values
        recorder_kwargs: dict = {
            "task_description": desc,
            "capture_audio": audio,
        }
        if _network_material.active:
            recorder_kwargs["network"] = True
            recorder_kwargs["network_handoff_ready"] = network_handoff_ready
            recorder_kwargs["network_config"] = _network_material.network_config
            recorder_kwargs["privacy_config"] = privacy_config
            recorder_kwargs["network_proxy_port"] = _network_material.proxy_port
            # V1.5 body-encryption material. dek plaintext crosses the
            # spawn boundary into the proxy mp.Process via pickle (the
            # threat model accepts in-memory exposure within the recorder
            # process tree). dek_wrapped / dek_nonce are persisted to
            # network_event_meta inside _setup_network_capture so export-
            # time decryption can resolve the DEK without re-reading KEK.
            recorder_kwargs["dek"] = _network_material.dek
            recorder_kwargs["dek_wrapped"] = _network_material.dek_wrapped
            recorder_kwargs["dek_nonce"] = _network_material.dek_nonce
        if capture_video is not None:
            recorder_kwargs["capture_video"] = capture_video
        else:
            recorder_kwargs["capture_video"] = True
        if capture_images is not None:
            recorder_kwargs["capture_images"] = capture_images
        if capture_window_data is not None:
            recorder_kwargs["capture_window_data"] = capture_window_data
        if chunking_enabled:
            recorder_kwargs["video_chunk_duration"] = chunk_duration

        # Per-recording identity files are policy-owned. ``InheritLock``
        # writes them because identity is independent of who owns the
        # process lock — the daemon-spawned engine subprocess still needs
        # ``.recording_id`` / ``.recording_intent`` for downstream catalog,
        # upload, scrubber, and recovery consumers.
        _privacy_mode_str = (
            privacy_config.mode.value if privacy_config else "internal"
        )
        try:
            lock_policy.write_identity(
                capture_dir, request=request, privacy_mode=_privacy_mode_str,
            )
        except OSError as _intent_err:
            if cloud_intent:
                console.print(
                    f"[red]Error:[/red] Failed to write recording identity: {_intent_err}\n"
                    "Cloud recordings require intent tracking. Cannot proceed."
                )
                raise SystemExit(1)
            elif verbose:
                console.print(
                    f"[yellow]Warning:[/yellow] Could not write recording identity: {_intent_err}"
                )

        chunk_processor = None

        # --- SIGINT handler (flag-based, no console.print inside) ---
        # Installed BEFORE Recorder.__enter__() so signals are handled during
        # the entire setup window (wait_for_ready, chunk processor init, etc.).
        # Guards protect against variables not yet bound (recorder, _child_pids).
        def _force_exit(sig, frame):
            nonlocal _ctrl_c_count, _stop_reason
            _ctrl_c_count += 1

            if _ctrl_c_count == 1:
                _stop_reason = "graceful"
                _stop_event.set()
                if recorder is not None:
                    recorder.stop()
                return

            if _ctrl_c_count == 2:
                # Write hint to stderr (stdout may be suppressed)
                try:
                    sys.__stderr__.write(
                        "\n  \033[1;35m⚡ Force quitting\033[0m — "
                        "terminating all processes...\n"
                        "  \033[2mStill stuck? Run: "
                        "\033[0;1mscreencap stop --force\033[0m\n\n"
                    )
                    sys.__stderr__.flush()
                except Exception:
                    pass

            # 3rd+ Ctrl+C: immediate exit — raw SIGKILL, no escalation
            if _ctrl_c_count > 2:
                menubar_policy.kill()
                os._exit(1)

            # 2nd Ctrl+C: force-quit path
            _stop_reason = "force"
            _stop_event.set()

            # Kill children using stored PIDs (signal-safe).
            # Fall back to active_children() if PIDs not yet captured.
            # Snapshot to avoid mutation during iteration (health
            # check removes dead PIDs from the main thread).
            _pids_snapshot = (
                list(_child_pids) if _child_pids
                else [c.pid for c in mp.active_children()]
            )
            for pid in _pids_snapshot:
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(1)
            for pid in _pids_snapshot:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

            # Essential cleanup that os._exit would skip
            try:
                lock_policy.release()
            except Exception:
                pass
            if _saved_stdout is not None:
                sys.stdout = _saved_stdout
            if _saved_stderr is not None:
                sys.stderr = _saved_stderr

            # Write sentinel locally for recovery via `screencap upload`
            # (no upload — os._exit is imminent)
            try:
                import uuid

                _sentinel = {
                    "version": 1,
                    "recording_name": _recording_name,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "stop_reason": "force",
                    "chunks_expected": len(list(capture_dir.glob("chunk_*_manifest.json"))),
                    "sentinel_id": str(uuid.uuid4()),
                    "show_on_website": show_on_website,
                }
                (capture_dir / "recording_complete.json").write_text(
                    json.dumps(_sentinel, indent=2)
                )
            except Exception:
                pass

            menubar_policy.kill()
            os._exit(1)

        def _sigterm_handler(sig, frame):
            nonlocal _stop_reason
            _stop_reason = "sigterm"
            _stop_event.set()
            # ``recorder is None`` means a signal arrived before Recorder
            # finished __enter__; the body's finally block still runs.
            if recorder is not None:
                recorder.stop()

        # Installed BEFORE Recorder.__enter__() so SIGINT during the
        # entire setup window is honoured — Tier-3 enforcement in
        # tests/test_signal_during_setup.py. Standalone CLI passes
        # ThreeTapSigint; session workers pass SigtermOnly (controller
        # owns Ctrl+C, forwards as SIGTERM).
        signal_policy.install(
            sigint_handler=_force_exit, sigterm_handler=_sigterm_handler,
        )

        # Temporarily redirect stderr to /dev/null while creating the
        # Recorder.  The multiprocessing resource_tracker is lazily spawned
        # on the first multiprocessing primitive (Event/Value/Queue) and
        # inherits sys.stderr at that moment.  By pointing stderr at
        # /dev/null, the tracker's output (KeyError tracebacks at shutdown)
        # goes nowhere.  We restore stderr immediately after so real errors
        # are still visible.
        _real_stderr_fd = os.dup(2)
        try:
            _devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(_devnull_fd, 2)
            os.close(_devnull_fd)
        except OSError:
            _real_stderr_fd = None

        with Recorder(
            str(capture_dir),
            **recorder_kwargs,
            screen_filter=screen_filter,
        ) as recorder:
            # Restore real stderr now that the resource tracker has spawned
            # with /dev/null as its stderr.
            if _real_stderr_fd is not None:
                try:
                    os.dup2(_real_stderr_fd, 2)
                    os.close(_real_stderr_fd)
                except OSError:
                    pass
                _real_stderr_fd = None

            recorder.wait_for_ready(timeout=30)
            status.stop()

            # Reset ``t0`` to the moment the engine is confirmed ready.
            # Everything above this point — metrics scan (wifi + app
            # versions can be 30-60s on busy Macs), engine spawn,
            # writer-process startup, ``wait_for_ready`` — is setup
            # overhead that the user does NOT perceive as "recording
            # time". Counting it inflates the reported Duration by
            # many tens of seconds and makes the summary disagree with
            # ``ffprobe chunk_0000.mp4``. Starting the clock here
            # yields an elapsed value that matches the video file to
            # within ~100 ms.
            t0 = time.time()

            # ChunkProcessor + ScrubWorker share the helper's flush_lock
            # so concurrent flush handshakes don't race on the engine's
            # flush_ack_counter.
            _collaborators.start(
                recorder=recorder,
                capture_dir=capture_dir,
                screen_filter=screen_filter,
                privacy_config=privacy_config,
                chunking_enabled=chunking_enabled,
                console=console,
            )
            chunk_processor = _collaborators.chunk_processor
            _scrub_worker = _collaborators.scrub_worker

            # ``InheritLock.register_children`` is a no-op — the daemon
            # supervisor owns the pidfile lifecycle. Kept for the seam.
            lock_policy.register_children(capture_dir, [])

            # Store raw PIDs for signal-safe force-exit (avoids
            # multiprocessing._children_lock which can deadlock in a handler).
            _child_pids = [child.pid for child in mp.active_children()]

            from screencap.config import get_first_seen_prompt_enabled
            menubar_policy.spawn(
                name, t0, capture_dir, channels,
                audio_enabled=audio,
                prompt_enabled=get_first_seen_prompt_enabled(),
            )

            # transient=False + manual live.update() on stop. Rich's normal
            # render cycle overwrites every panel line (including borders).
            # transient=True has an off-by-one bug with Panel borders on
            # signal interrupt, leaving the top border as a remnant.
            #
            # ``elapsed`` is updated inside the Live loop and frozen in the
            # finally block so the returned value reflects the user-visible
            # recording duration (the number shown in the live status bar)
            # and NOT the wall clock that includes the multi-minute
            # post-capture cleanup (ChunkProcessor drain, DB upload,
            # sentinel upload).
            elapsed = 0.0
            with Live(
                _build_live_display(name, 0.0, True),
                console=console,
                refresh_per_second=2,
            ) as live:
                try:
                    while recorder.is_recording and not _stop_event.is_set():
                        elapsed = time.time() - t0
                        pulse_on = int(elapsed) % 2 == 0

                        # PermissionPolicy.poll: 5 s adaptive cadence; raises
                        # PermissionRevoked when a TCC permission goes away
                        # mid-recording. The ``permission_lost`` stderr event
                        # is the SwiftUI shell's only signal — silent
                        # regression here would break the macOS shell UX.
                        try:
                            permission_policy.poll(elapsed)
                        except PermissionRevoked as _exc:
                            if not _stop_event.is_set():
                                _stop_reason = f"permission_revoked_{_exc.missing}"
                                try:
                                    from screencap._stderr_events import (
                                        EVENT_PERMISSION_LOST,
                                    )
                                    from screencap._stderr_events import (
                                        emit_event as _emit_event,
                                    )
                                    _emit_event(
                                        EVENT_PERMISSION_LOST,
                                        permission=_exc.missing,
                                        elapsed=elapsed,
                                    )
                                except Exception:
                                    pass
                                _stop_event.set()
                                recorder.stop()

                        try:
                            disk_policy.poll(elapsed)
                        except _DiskSpaceCritical as _exc:
                            if not _stop_event.is_set():
                                _stop_reason = "disk_full"
                                _disk_free_at_stop = _exc.free_mb
                                _stop_event.set()
                                recorder.stop()
                        disk_warning = disk_policy.warning

                        _chunk_status = chunk_processor.status if chunk_processor else ""
                        _health_warning = recorder.health_warning
                        # Prune dead PIDs to prevent PID recycling bug in force-quit
                        for crash in recorder.child_crashes:
                            dead_pid = crash.get("pid")
                            if dead_pid and dead_pid in _child_pids:
                                _child_pids.remove(dead_pid)
                        live.update(_build_live_display(name, elapsed, pulse_on, disk_warning, _chunk_status, _health_warning))
                        _stop_event.wait(0.5)
                finally:
                    # Detect if recording stopped due to a critical child crash
                    if not _stop_reason and recorder.health_warning:
                        _stop_reason = "child_crash"

                    # Replace the panel with the stop message.  Rich
                    # knows the panel height and will overwrite every
                    # line, including borders.  The message stays on
                    # screen because transient=False (default).
                    if _stop_reason == "child_crash":
                        live.update(Text(
                            f"  ⚠ Recording stopped: {recorder.health_warning}",
                            style="#f59e0b",
                        ))
                    elif _stop_reason == "disk_full":
                        live.update(Text(
                            f"  ■ Recording auto-stopped: disk space critically low "
                            f"({_disk_free_at_stop:.0f} MB remaining)",
                            style="#f59e0b",
                        ))
                    elif _stop_reason == "graceful":
                        _stop_msg = Text()
                        _stop_msg.append("  ■ Stopping recording... ", style="#a78bfa")
                        _stop_msg.append("post-processing may take a moment", style="dim")
                        _stop_msg.append("\n    ", style="dim")
                        _stop_msg.append("From another terminal: ", style="dim")
                        _stop_msg.append("screencap stop", style="bold")
                        _stop_msg.append("  or  ", style="dim")
                        _stop_msg.append("screencap stop --force", style="bold")
                        live.update(_stop_msg)
                    elif _stop_reason == "sigterm":
                        _stop_msg = Text()
                        _stop_msg.append("  ■ Stopping recording... ", style="#a78bfa")
                        _stop_msg.append("post-processing may take a moment", style="dim")
                        live.update(_stop_msg)
                    elif _stop_reason == "force":
                        live.update(Text("  ⚡ Force quitting — terminating processes...", style="#f472b6"))
                    else:
                        live.update(Text(""))

                    # Drain pending menu bar overrides before final flush
                    if screen_filter is not None:
                        try:
                            screen_filter.poll_overrides()
                        except Exception:
                            pass

                    menubar_policy.notify_processing()

            # Drain the engine pipeline FIRST so the record/fanout/status
            # threads finish forwarding every chunk message — including
            # the ``final_chunk`` rotation pushed during record-thread
            # shutdown — onto ``_chunk_process_q`` before we enqueue a
            # poison pill behind it. ``finalize_pipeline`` joins the
            # threads but preserves the queues so chunk_processor /
            # scrub_worker can drain. ``Recorder.__exit__`` calls it
            # again idempotently and then closes the queues.
            recorder.finalize_pipeline()

            # Finalize runs INSIDE the ``with``-block so chunk_processor
            # and scrub_worker drain BEFORE Recorder.__exit__ closes the
            # engine queues. Load-bearing ordering: no outsider holds
            # engine-queue references past __exit__.
            _recording_name = (
                (capture_dir / ".recording_id").read_text().strip()
                if (capture_dir / ".recording_id").exists() else name
            )
            try:
                _collaborators.finalize_catchall_scrub(capture_dir=capture_dir)
                _collaborators.stop_scrub_worker(timeout=30.0)
                _collaborators.stop_chunk_processor(
                    deadline_seconds=300, console=console,
                )
            finally:
                _collaborators.close_engine_queues(
                    menubar_owns_channels=menubar_policy.owns_channels,
                )
            _finalize_result = _collaborators.finalize_uploads(
                capture_dir=capture_dir,
                stop_reason=_stop_reason,
                recording_name=_recording_name,
                console=console,
            )
            _sentinel_uploaded = _finalize_result["sentinel_uploaded"]

            # Suppress stdout before Recorder.__exit__ runs (profile block),
            # but redirect stderr to a log file so subprocess errors are captured.
            if not verbose:
                try:
                    from loguru import logger as _sc_logger
                    _sc_logger.remove()
                except Exception:
                    pass
                _saved_stdout = sys.stdout
                _saved_stderr = sys.stderr
                _devnull = open(os.devnull, "w")
                sys.stdout = _devnull
                try:
                    _engine_log = open(capture_dir / "engine_exit.log", "w")
                    sys.stderr = _engine_log
                except Exception:
                    sys.stderr = _devnull

    except KeyboardInterrupt:
        _stop_reason = "interrupt"
        console.print("  [dim]■ Stopping recording...[/dim]")
        # Suppress stdout, redirect stderr to log file on interrupt path
        if not verbose:
            try:
                from loguru import logger as _sc_logger
                _sc_logger.remove()
            except Exception:
                pass
            _saved_stdout = sys.stdout
            _saved_stderr = sys.stderr
            _devnull = open(os.devnull, "w")
            sys.stdout = _devnull
            try:
                _engine_log = open(capture_dir / "engine_exit.log", "w")
                sys.stderr = _engine_log
            except Exception:
                sys.stderr = _devnull
    finally:
        # Restore stdout/stderr if we redirected them
        if _saved_stdout is not None:
            try:
                sys.stdout.close()
            except Exception:
                pass
            sys.stdout = _saved_stdout
        if _saved_stderr is not None:
            try:
                sys.stderr.close()
            except Exception:
                pass
            sys.stderr = _saved_stderr

        status.stop()
        # Restore default handlers via the policy (mirrors install/uninstall).
        signal_policy.uninstall()
        atexit.unregister(_cleanup_children)
        lock_policy.release()

        # MitmProxyV15.teardown releases the flock acquired in setup();
        # Null is a no-op.
        network_policy.teardown()

        # Restore stderr fd if it wasn't restored earlier (e.g. exception
        # during Recorder.__enter__).
        if _real_stderr_fd is not None:
            try:
                os.dup2(_real_stderr_fd, 2)
                os.close(_real_stderr_fd)
            except OSError:
                pass

        # Best-effort LOCAL sentinel write for unhandled exceptions (Step 3d)
        # Do NOT upload here — chunk_processor hasn't stopped yet, so chunks
        # may still be uploading.  Uploading sentinel now would trigger the
        # stitcher before manifests land in GCS (race condition).
        # The local file enables recovery via ``screencap upload``.
        if cloud_intent and chunk_processor is not None and not _sentinel_uploaded:
            try:
                from screencap.chunk_processor import _build_sentinel_data
                _n_chunks = len(list(capture_dir.glob("chunk_*_manifest.json")))
                _sentinel_data = _build_sentinel_data(
                    _recording_name, stop_reason="exception", chunks_expected=_n_chunks,
                    show_on_website=show_on_website,
                )
                _sentinel_path = capture_dir / "recording_complete.json"
                _sentinel_path.write_text(json.dumps(_sentinel_data, indent=2))
            except Exception:
                pass

        # Suppress noisy multiprocessing cleanup tracebacks
        warnings.filterwarnings("ignore", category=ResourceWarning)

    try:
        from screencap.metrics import save_metrics

        stop_reason_val = _stop_reason if _stop_reason == "disk_full" else None
        save_metrics(
            capture_dir, "end",
            wifi_metrics=wifi_metrics, app_versions=app_versions,
            stop_reason=stop_reason_val,
        )
    except Exception as e:
        if verbose:
            console.print(f"[yellow]Warning:[/yellow] Could not collect end metrics: {e}")


    # NOTE: intentionally NOT recomputing ``elapsed = time.time() - t0``
    # here. The live loop above already froze ``elapsed`` at the moment
    # the user stopped the recording — that's the number shown in the
    # status bar. Reassigning now would inflate the summary's Duration
    # field by the wall-clock cost of the post-capture cleanup
    # (ChunkProcessor drain, DB upload, sentinel upload), which can be
    # minutes for chunked cloud recordings.

    # Restore output if we suppressed it
    if not verbose:
        _restore_output()

    # Sidecar metadata for the worker → .recording_ready merge (todo 002, 009).
    # Writes force_stopped + terminated_reason so SessionController can
    # propagate the right SystemExit code to the SwiftUI shell.
    try:
        _force_stopped = bool(
            getattr(chunk_processor, "was_force_stopped", False)
            or _stop_reason in ("force", "child_crash")
        )
        _term_reason = None
        if _stop_reason == "disk_full":
            _term_reason = "disk_full"
        elif _stop_reason and _stop_reason.startswith("permission_revoked_"):
            _term_reason = "permission_lost"
        elif _stop_reason in ("force", "child_crash"):
            _term_reason = "force_killed"
        (capture_dir / ".recording_stop_meta.json").write_text(json.dumps({
            "force_stopped": _force_stopped,
            "terminated_reason": _term_reason,
            "stop_reason_raw": _stop_reason or None,
        }))
    except OSError as exc:
        # Failure here breaks the documented exit-code contract (todo 005
        # / R6): SessionController.run() reads the absent sidecar, leaves
        # _terminated_reason=None, and exits 0 even on permission_lost /
        # disk_full. Surface as a structured event so SwiftUI can correlate
        # the unexpected exit_code=0 with a real terminal-reason failure.
        try:
            from screencap._stderr_events import (
                EVENT_TERMINATED_REASON_PERSIST_FAILED,
            )
            from screencap._stderr_events import (
                emit_event as _emit_event,
            )
            _emit_event(
                EVENT_TERMINATED_REASON_PERSIST_FAILED,
                error=str(exc),
                capture_dir=str(capture_dir),
                terminated_reason=_term_reason,
            )
        except Exception:
            pass

    if _stop_reason == "disk_full":
        raise DiskFullError(
            capture_dir, elapsed, menubar_policy.proc, menubar_policy.state_file,
        )

    return RecordingResult(capture_dir=capture_dir, elapsed=elapsed)
