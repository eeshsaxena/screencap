"""Wrap screencap engine Recorder for screencap."""

# ruff: noqa: I001
# Load-bearing import order: ``screencap._startup`` must come before
# ``multiprocessing`` so its PYTHONWARNINGS filter is set before the
# resource_tracker subprocess spawns.

from __future__ import annotations

from screencap import _startup  # noqa: F401

import atexit
import json
import multiprocessing
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from screencap import __version__
from screencap.config import (
    get_app_versions,
    get_audio_default,
    get_disk_stop_mb,  # noqa: F401  -- re-exported for tests that mock it here
    get_disk_warn_mb,  # noqa: F401  -- re-exported for tests that mock it here
    get_recordings_dir,
    get_wifi_metrics,
)

console = Console()

_DISK_CHECK_INTERVAL = 30  # seconds between disk space checks


class DiskFullError(Exception):
    """Raised when recording auto-stops due to low disk space."""

    def __init__(
        self,
        capture_dir: Path,
        elapsed: float,
        menubar_proc: multiprocessing.Process | None = None,
        menubar_state_file: Path | None = None,
    ):
        self.capture_dir = capture_dir
        self.elapsed = elapsed
        self.menubar_proc = menubar_proc
        self.menubar_state_file = menubar_state_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _fmt_duration_clock(seconds: float) -> str:
    """Format as HH:MM:SS for the live display."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_size(path: Path) -> str:
    if not path.exists():
        return "—"
    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return _fmt_bytes(size)


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def _print_banner() -> None:
    """Print the ASCII art startup banner."""
    term_width = shutil.get_terminal_size((80, 24)).columns

    if term_width < 78:
        # Narrow terminal fallback
        console.print(f"\n[bold #60a5fa]◉ ScreenCap[/bold #60a5fa] [dim #a78bfa]v{__version__}[/dim #a78bfa]\n")
        return

    try:
        import pyfiglet
        banner_text = pyfiglet.figlet_format("SCREENCAP", font="ansi_shadow")
    except Exception:
        console.print(f"\n[bold #60a5fa]◉ ScreenCap[/bold #60a5fa] [dim #a78bfa]v{__version__}[/dim #a78bfa]\n")
        return

    banner = Text(banner_text.rstrip(), style="bold #60a5fa")
    console.print()
    console.print(banner)
    version_line = f"v{__version__}"
    console.print(f"[#818cf8]{version_line:^{term_width}}[/#818cf8]")
    console.print()


# ---------------------------------------------------------------------------
# Live recording display
# ---------------------------------------------------------------------------

def _build_live_display(name: str, elapsed: float, pulse_on: bool, disk_warning: str = "", chunk_status: str = "", health_warning: str = "") -> Group:
    """Build the Rich renderable for the live recording indicator."""
    dot_style = "bold #f472b6" if pulse_on else "dim #f472b6"
    timer = _fmt_duration_clock(elapsed)

    line1 = Text()
    line1.append(" ")
    line1.append("●", style=dot_style)
    line1.append(" REC  ", style="bold #f472b6")
    line1.append(name, style="bold #f0f4ff")
    # Right-align the timer
    padding = max(1, 46 - len(name) - 12)
    line1.append(" " * padding)
    line1.append(timer, style="bold #f0f4ff")

    line2 = Text()
    line2.append(" Ctrl+C", style="#818cf8")
    line2.append(" stop", style="dim")
    line2.append("  ·  ", style="dim")
    line2.append("Ctrl+C ×2", style="#818cf8")
    line2.append(" force quit", style="dim")
    line2.append("  ·  ", style="dim")
    line2.append("● Menu bar", style="#f472b6")
    line2.append(" also available", style="dim")

    content = Text()
    content.append_text(line1)
    if disk_warning:
        content.append("\n\n")
        content.append(f"   {disk_warning}")
    if chunk_status:
        content.append("\n")
        content.append(f"   {chunk_status}", style="dim")
    if health_warning:
        content.append("\n")
        content.append(f"   {health_warning}", style="bold #f59e0b")
    content.append("\n\n")
    content.append_text(line2)

    panel = Panel(
        content,
        box=box.ROUNDED,
        border_style="#f472b6",
        padding=(1, 2),
    )
    # Wrap with a blank line on top as a buffer.  Rich's Live cleanup
    # after Ctrl+C consistently misses the topmost rendered line (the
    # terminal echoes ^C\n which shifts the cursor by 1, causing an
    # off-by-one in Rich's line-erase count).  By making the topmost
    # line blank, the missed line is invisible — not a red border.
    return Group(Text(""), panel)


# ---------------------------------------------------------------------------
# Summary / end screen
# ---------------------------------------------------------------------------

def print_summary(name: str, capture_dir: Path, elapsed: float) -> None:
    """Print the Vercel-style post-recording summary."""
    console.print()
    console.print("  [bold #22d3ee]✅ Recording complete[/bold #22d3ee]")
    console.print()

    # Stats with thick left border
    stats: list[tuple[str, str]] = [
        ("Name", name),
        ("Duration", _fmt_duration(elapsed)),
        ("Size", _fmt_size(capture_dir)),
    ]

    video = capture_dir / "video.mp4"
    audio_file = capture_dir / "audio.flac"
    if video.exists():
        stats.append(("Video", _fmt_bytes(video.stat().st_size)))
    if audio_file.exists():
        stats.append(("Audio", _fmt_bytes(audio_file.stat().st_size)))

    # Use ~ shorthand for home directory
    location = str(capture_dir)
    home = str(Path.home())
    if location.startswith(home):
        location = "~" + location[len(home):]
    stats.append(("Location", location))

    for label, value in stats:
        console.print(f"  [dim #818cf8]┃[/dim #818cf8]  [#60a5fa]{label:<10}[/#60a5fa] {value}")

    console.print()
    console.print(f"  [#818cf8]{'━' * 52}[/#818cf8]")
    console.print()
    console.print("  [bold #f0f4ff]Next steps:[/bold #f0f4ff]")
    console.print()

    commands = [
        (f"screencap view {name}", "Open in browser"),
        (f"screencap export {name}", "Export as JSONL"),
        (f"screencap upload {name}", "Upload to cloud"),
        ("screencap list", "All recordings"),
    ]
    for cmd, desc in commands:
        console.print(f"    [bold #22d3ee]{cmd:<38}[/bold #22d3ee] [dim]{desc}[/dim]")

    console.print()


FOLLOWUP_FORCE_STOPPED = "force_stopped"
FOLLOWUP_NONE_UPLOADED = "none_uploaded"
FOLLOWUP_UPLOAD_DISABLED = "upload_disabled"
FOLLOWUP_PARTIAL = "partial"

_FOLLOWUP_MESSAGES = {
    FOLLOWUP_FORCE_STOPPED: lambda d, cmd: (
        "Some chunks may not have been uploaded (processing timed out).\n"
        f"  Run [bold]{cmd}[/bold] to upload remaining data."
    ),
    FOLLOWUP_NONE_UPLOADED: lambda d, cmd: (
        "No chunks were uploaded.\n"
        f"  Run [bold]{cmd}[/bold] to upload the recording."
    ),
    FOLLOWUP_UPLOAD_DISABLED: lambda d, cmd: (
        f"Uploads disabled: {d.get('upload_warning')}\n"
        f"  Run [bold]{cmd}[/bold] after fixing the issue."
    ),
    FOLLOWUP_PARTIAL: lambda d, cmd: (
        f"{d.get('n_uploaded', 0)} of {d.get('n_total', 0)} chunks uploaded.\n"
        f"  Run [bold]{cmd}[/bold] to upload the rest."
    ),
}


def print_upload_followup(recording_name: str, capture_dir: Path) -> None:
    """Print the deferred upload follow-up warning, if any.

    ``start_recording`` writes ``.upload_followup.json`` when the live-upload
    path did not fully succeed. The CLI / session controller invokes this
    helper *after* any post-recording rename so the suggested
    ``screencap upload <name>`` command matches the final on-disk directory.
    No-op if the follow-up file is missing.
    """
    followup_path = capture_dir / ".upload_followup.json"
    try:
        data = json.loads(followup_path.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        followup_path.unlink(missing_ok=True)
        return

    msg = _FOLLOWUP_MESSAGES.get(data.get("kind"))
    if msg is not None:
        console.print(f"[yellow]{msg(data, f'screencap upload {recording_name}')}[/yellow]")

    followup_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Log suppression
# ---------------------------------------------------------------------------

def _suppress_output() -> None:
    """Suppress noisy output from the recording pipeline.

    Sets SC_LOG_LEVEL=ERROR env var so spawned child processes (which
    re-import the module) pick up the higher threshold.  Also overrides
    loguru directly in the current process (in case the module was already
    imported before this function was called).  Disables tqdm as well.

    We use ERROR (not WARNING) because the vendored code emits benign
    WARNING messages during shutdown (screenshot failures, audio timeout)
    that are expected and shouldn't clutter the user's terminal.
    """
    os.environ["SC_LOG_LEVEL"] = "ERROR"
    os.environ["TQDM_DISABLE"] = "1"
    # Override loguru directly in the main process — the env var only
    # takes effect on fresh imports (child processes).  If screencap.engine
    # was already imported, loguru is already configured at INFO.
    try:
        from loguru import logger as _sc_logger
        _sc_logger.remove()
        _sc_logger.add(sys.stderr, level="ERROR")
    except ImportError:
        pass


def _restore_output() -> None:
    """Restore loguru and tqdm to defaults."""
    os.environ.pop("SC_LOG_LEVEL", None)
    os.environ.pop("TQDM_DISABLE", None)
    # Re-configure loguru in this process back to INFO
    try:
        from loguru import logger as _sc_logger
        _sc_logger.remove()
        _sc_logger.add(sys.stderr, level="INFO")
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# macOS permission helpers
# ---------------------------------------------------------------------------

def _open_privacy_settings(pane: str) -> None:
    """Open System Settings to a specific Privacy & Security pane.

    pane: one of 'Privacy_ScreenCapture', 'Privacy_Accessibility', 'Privacy_ListenEvent'

    URL form is version-detected:

    * macOS 13+ uses the ``com.apple.settings.PrivacySecurity.extension`` URL
      (System Settings introduced in Ventura). The legacy form lands on a
      generic page on macOS 26+.
    * macOS 11/12 still ships System Preferences and only honors the legacy
      ``com.apple.preference.security`` URL.

    The SwiftUI shell follows the same Apple URL contract independently in
    ``PermissionController.openSystemSettings``; both code paths are
    independent implementations of the documented Apple URL form, not
    locked together by tooling.
    """
    macos_major = 0
    try:
        version_str = platform.mac_ver()[0]
        if version_str:
            macos_major = int(version_str.split(".", 1)[0])
    except (ValueError, IndexError):
        pass

    if macos_major >= 13:
        url = f"x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?{pane}"
    else:
        url = f"x-apple.systempreferences:com.apple.preference.security?{pane}"
    subprocess.run(["open", url], check=False)


# Python snippets to check each permission in a fresh subprocess.
# macOS caches permission state within a process, so in-process checks
# won't detect grants made after startup.  Spawning a subprocess gives
# us the real, current OS state.
_PERMISSION_CHECK_CODE: dict[str, str] = {
    "Accessibility": (
        "from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt; "
        "print(bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False})))"
    ),
    "Input Monitoring": (
        "import Quartz; print(bool(Quartz.CGPreflightListenEventAccess()))"
    ),
    # Screen Recording entry added (todo 002) so the mid-recording watcher
    # can use the fresh-subprocess path. macOS caches TCC state per-process,
    # so an in-process CGPreflightScreenCaptureAccess() call inside the
    # recorder's main loop returns the cached value at recorder start —
    # never the live state — defeating the whole point of the watcher.
    "Screen Recording": (
        "import Quartz; print(bool(Quartz.CGPreflightScreenCaptureAccess()))"
    ),
}


def _check_permission_fresh(name: str) -> bool | None:
    """Check a permission in a fresh subprocess to bypass OS-level caching.

    Tri-state return:
      - ``True``  — probe succeeded, permission granted
      - ``False`` — probe succeeded, permission denied
      - ``None``  — probe FAILED (subprocess timeout, OSError, missing
                    PERMISSION_CHECK_CODE entry, or unparseable stdout).
                    Caller must treat this as "couldn't determine" and
                    NOT as "denied" — otherwise a transient Quartz/PyObjC
                    hiccup during a Sequoia overlay would kill the
                    in-progress recording.
    """
    code = _PERMISSION_CHECK_CODE.get(name)
    if not code:
        return None
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    out = result.stdout.strip()
    if out == "True":
        return True
    if out == "False":
        return False
    return None


def _check_macos_permissions() -> None:
    """Check macOS permissions and guide the user through granting them.

    Walks through each missing permission one at a time:
    1. Non-restart permissions first (Accessibility, Input Monitoring) —
       triggers the native prompt, opens System Settings, polls until granted.
    2. Screen Recording last — requires a terminal restart, so we exit.

    Polling uses a fresh subprocess for each check because macOS caches
    permission state within a process lifetime.
    """
    if sys.platform != "darwin":
        return

    try:
        from screencap.engine.platform.darwin import DarwinPlatform
    except ImportError:
        return

    # (name, check_fn, request_fn, pane, needs_restart)
    all_permissions = [
        ("Accessibility", DarwinPlatform.is_accessibility_enabled,
         DarwinPlatform.request_accessibility_access, "Privacy_Accessibility", False),
        ("Input Monitoring", DarwinPlatform.is_input_monitoring_enabled,
         DarwinPlatform.request_input_monitoring_access, "Privacy_ListenEvent", False),
        ("Screen Recording", DarwinPlatform.is_screen_recording_enabled,
         DarwinPlatform.request_screen_recording_access, "Privacy_ScreenCapture", True),
    ]

    missing = [(name, check, request, pane, restart)
               for name, check, request, pane, restart in all_permissions
               if not check()]

    if not missing:
        return

    names = ", ".join(m[0] for m in missing)
    total = len(missing)
    console.print(f"\n  [bold]Missing permissions:[/bold] {names}\n")

    for i, (name, check_fn, request_fn, pane, needs_restart) in enumerate(missing, 1):
        console.print(f"  [{i}/{total}] [bold]{name}[/bold]")

        request_fn()
        _open_privacy_settings(pane)
        console.print("        System Settings has been opened — enable your terminal app.")

        if needs_restart:
            console.print("\n  [yellow]Note:[/yellow] Screen Recording requires a terminal restart.")
            console.print("  After enabling, quit and reopen your terminal, then re-run:")
            console.print("    screencap start")
            raise SystemExit(1)

        # Non-restart permission — poll with subprocess checks until granted.
        # _check_permission_fresh is tri-state (True / False / None); only
        # ``is True`` counts as "granted". None (probe failed) keeps polling.
        with console.status(f"[bold]  Waiting for {name}...[/bold]"):
            for _ in range(120):
                time.sleep(1)
                if _check_permission_fresh(name) is True:
                    break

        result = _check_permission_fresh(name)
        if result is True:
            console.print(f"  [green]✓[/green] {name} granted!\n")
        else:
            if result is None:
                console.print(
                    f"\n  [yellow]Warning:[/yellow] Could not verify "
                    f"{name} (subprocess probe failed)."
                )
            else:
                console.print(f"\n  [red]Error:[/red] {name} was not granted in time.")
            console.print("  Grant the permission and re-run: screencap start")
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Unit 8: mid-recording permission revocation watcher
# ---------------------------------------------------------------------------


def _check_permissions_now() -> tuple[bool, str | None]:
    """Probe the three TCC permissions; return (all_ok, missing_name).

    Designed to run on the recorder's main loop every ~5s. Returns
    (False, "screen_recording" | "accessibility" | "input_monitoring") on
    the first detected revocation. Microphone is intentionally NOT polled
    here — audio loss should not abort a video-only capture.

    Uses fresh subprocesses (todo 002) instead of in-process PyObjC calls
    because macOS caches TCC state per-process — an in-process call from
    the recorder's main loop returns the cached value at recorder startup,
    not the live state. The 50-100ms-per-call subprocess cost happens once
    every 5s and is bounded against recording's existing CPU footprint.

    Probe failures are FAIL-OPEN: ``_check_permission_fresh`` returns
    tri-state and we only treat an explicit ``False`` as a revocation. A
    subprocess timeout, OSError, or unparseable stdout returns ``None`` —
    we skip that permission for this tick and retry on the next. Without
    this distinction (the previous bool-only path) a transient Quartz /
    PyObjC hiccup during a Sequoia overlay would kill the in-progress
    recording.
    """
    if sys.platform != "darwin":
        return True, None

    # Order matters — Screen Recording is the most user-impactful loss
    # (capture goes black), so report it first when multiple are revoked.
    for tcc_name, missing_label in (
        ("Screen Recording", "screen_recording"),
        ("Accessibility", "accessibility"),
        ("Input Monitoring", "input_monitoring"),
    ):
        try:
            granted = _check_permission_fresh(tcc_name)
        except Exception:
            # Defensive belt-and-suspenders: _check_permission_fresh already
            # returns None on subprocess errors, but a future change could
            # cause it to raise. Treat any raise as "couldn't determine"
            # rather than risk killing the recording.
            granted = None
        if granted is None:
            # Probe failed — couldn't determine state. Fail-open for this
            # tick; if the permission really is revoked, the next tick will
            # see a clean False and report it.
            continue
        if granted is False:
            return False, missing_label
    return True, None


# ---------------------------------------------------------------------------
# Menu bar helpers
# ---------------------------------------------------------------------------


def _spawn_menubar(
    recording_name: str,
    start_time: float,
    state_file: Path,
    window_feed_q: multiprocessing.Queue | None = None,
    override_q: multiprocessing.Queue | None = None,
    prompt_enabled: bool = True,
    disable_q: multiprocessing.Queue | None = None,
    *,
    audio_enabled: bool = True,
) -> multiprocessing.Process | None:
    """Spawn the menu bar status item as a daemon subprocess.

    Returns the Process object on success, or None if spawn fails.
    The process is daemonic so it is killed when the parent exits.

    Args:
        prompt_enabled: When True, the menubar shows a non-activating
            NSPanel the first time a never-seen ``(app, domain)`` pair
            becomes the frontmost window during the recording.
        disable_q: Optional queue the menubar writes to when the user
            toggles a target to ``exclude``. The recorder's scrub worker
            consumes this queue to retroactively delete already-captured
            rows for that target.
        audio_enabled: Initial state of the "Audio (next recording)"
            toggle shown in the menu.  Legacy / non-session path only —
            in session mode the :class:`SessionController` passes the
            value directly to ``_run_menubar``.
    """
    from screencap.menubar import _run_menubar

    proc = multiprocessing.Process(
        target=_run_menubar,
        args=(os.getpid(), recording_name, start_time, str(state_file),
              window_feed_q, override_q, prompt_enabled, disable_q),
        kwargs={"audio_enabled": audio_enabled},
        daemon=True,
        name="menubar",
    )
    proc.start()
    return proc


def _kill_menubar(
    proc: multiprocessing.Process | None,
    state_file: Path | None = None,
) -> None:
    """Terminate the menu bar subprocess.  Safe to call multiple times."""
    if proc is None:
        return
    # Signal via state file first (allows clean AppKit shutdown)
    if state_file is not None:
        try:
            from screencap.menubar import STATE_DONE
            state_file.write_text(STATE_DONE)
        except Exception:
            pass
    # SIGKILL immediately — the menu bar is a UI helper, no data to flush.
    pid = getattr(proc, "pid", None)
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------------------
# Main recording function
# ---------------------------------------------------------------------------

def start_recording(
    name: str,
    description: str | None = None,
    audio: bool | None = None,
    output_dir: str | Path | None = None,
    wifi_metrics: bool | None = None,
    app_versions: bool | None = None,
    force_clean: bool = False,
    capture_video: bool | None = None,
    capture_images: bool | None = None,
    capture_window_data: bool | None = None,
    verbose: bool = False,
    chunk_duration: float | None = None,
    live_upload: bool = True,
    force_mode: "PrivacyMode | None" = None,
    cloud_intent: bool = False,
    keep_local: bool = True,
    intent_source: str = "flag",
    segmentation_mode: str = "llm",
    scrub_enabled: bool = True,
    show_on_website: bool = True,
    network: bool = False,
    *,
    # Session-controller worker-mode injection points. Set only by
    # screencap.session.run_recording_worker.
    _channels: "IpcChannels | None" = None,
    _menubar_policy: "MenubarPolicy | None" = None,
    _signal_policy: "SignalPolicy | None" = None,
    _lock_policy: "LockPolicy | None" = None,
    network_handoff_ready=None,
) -> tuple[Path, float, multiprocessing.Process | None, Path | None]:
    """Start a screen capture recording. Blocks until Ctrl+C.

    SCR-38 thin shim: builds the seam bundles and delegates the body to
    ``engine.ScreenRecorder.run()``. The 4-tuple return is preserved for
    backward compat with existing callers and tests; the trailing two
    elements (``menubar_proc``, ``menubar_state_file``) move out in
    SCR-41 once ``MenubarPolicy`` owns them.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.disk_policy import MonitorAndStop
    from screencap.engine.lock_policy import ClaimLock
    from screencap.engine.menubar_policy import SpawnNewMenubar
    from screencap.engine.network_policy import MitmProxyV15 as _MitmProxyV15
    from screencap.engine.network_policy import Null as _NetworkNull
    from screencap.engine.permission_policy import MacOSTCC
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingPolicies,
        RecordingRequest,
        ScreenRecorder,
        ThreeTapSigint,
    )

    request = RecordingRequest(
        name=name,
        config=RecordingConfig(),
        description=description,
        cloud_intent=cloud_intent,
        keep_local=keep_local,
        intent_source=intent_source,
        segmentation_mode=segmentation_mode,
        scrub_enabled=scrub_enabled,
        show_on_website=show_on_website,
    )
    channels = _channels if _channels is not None else IpcChannels.create()
    menubar = _menubar_policy if _menubar_policy is not None else SpawnNewMenubar()
    policies = RecordingPolicies(
        signal=_signal_policy if _signal_policy is not None else ThreeTapSigint(),
        lock=_lock_policy if _lock_policy is not None else ClaimLock(),
        menubar=menubar,
        permission=MacOSTCC(),
        disk=MonitorAndStop(),
        network=_MitmProxyV15() if network else _NetworkNull(),
    )
    legacy = LegacyOptions(
        audio=audio,
        output_dir=output_dir,
        wifi_metrics=wifi_metrics,
        app_versions=app_versions,
        force_clean=force_clean,
        capture_video=capture_video,
        capture_images=capture_images,
        capture_window_data=capture_window_data,
        verbose=verbose,
        chunk_duration=chunk_duration,
        live_upload=live_upload,
        force_mode=force_mode,
        network_handoff_ready=network_handoff_ready,
    )

    rec = ScreenRecorder(
        request=request, channels=channels, policies=policies, legacy=legacy,
    )
    result = rec.run()
    return (
        result.capture_dir,
        result.elapsed,
        menubar.proc,
        menubar.state_file,
    )


def _run_screen_recorder(rec: "ScreenRecorder") -> "RecordingResult":
    """Body of the recording lifecycle, driven by ``ScreenRecorder.run()``.

    SCR-38 ports the body of ``start_recording`` here verbatim. The seam
    constructs ``rec`` with the request / channels / policies / legacy
    bundles; this function reads them into locals and proceeds exactly
    as the wrapper used to. The ``_external_*`` / ``_skip_*`` flags live
    on ``LegacyOptions`` and migrate into ``RecordingPolicies`` axis-by-
    axis in SCR-39 through SCR-43.
    """
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

    # SCR-42: bind disk policy to the resolved capture_dir before preflight.
    disk_policy.bind(capture_dir)

    # SCR-40: orphan preflight + process-exclusive lock claim are bundled
    # into ``LockPolicy.claim``. Standalone CLI passes ``ClaimLock``;
    # session workers pass ``InheritLock`` (controller already claimed at
    # ``__init__``). Lock-contention exit-code-2 + stderr event also live
    # inside ``ClaimLock``.
    lock_policy.claim(capture_dir, force_clean=force_clean)

    if capture_dir.exists() and any(capture_dir.iterdir()):
        console.print(
            f"[red]Error:[/red] Directory already exists and is not empty: {capture_dir}"
        )
        raise SystemExit(1)

    # SCR-42: disk preflight delegated to DiskPolicy.
    from screencap.engine.disk_policy import DiskSpaceCritical as _DiskSpaceCritical
    from screencap.engine.screen_recorder import DiskTooLowAtStart as _DiskTooLowAtStart
    try:
        disk_policy.preflight()
    except _DiskTooLowAtStart as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    capture_dir.mkdir(parents=True, exist_ok=True)

    # Suppress loguru/tqdm noise unless --verbose.
    # Must happen BEFORE any screencap.engine import (including the
    # screen-recording permission check below) so the env var is set
    # when the module-level loguru config runs for the first time.
    if not verbose:
        _suppress_output()

    # SCR-42: permission preflight delegated to PermissionPolicy.
    permission_policy.preflight()

    desc = description or ""

    # --- Banner ---
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

    # --- Menu bar IPC queues ---
    # SCR-41: queues are first-class args via IpcChannels; no fallback
    # creation here. SpawnNewMenubar owns new queues (standalone CLI);
    # Noop reuses the controller's queues (session worker).
    _menubar_disable_q = channels.disable

    # --- Privacy: capture-time enforcement (SCR-44) ---
    # Construction (cloud_intent → PUBLIC floor, window-data gate, override
    # file path) is owned by the engine helper. The wrapper still owns the
    # console UX around configuration failure and the cloud privacy notice.
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
        if privacy_config is not None:
            console.print(
                f"[red]Error:[/red] Capture-time privacy enforcement failed: {e}\n"
                "Recording cannot proceed without privacy protection. "
                "Check dependencies and configuration."
            )
            raise SystemExit(1)
        console.print(
            f"[yellow]Warning:[/yellow] Capture-time privacy enforcement disabled: {e}"
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
        for child in multiprocessing.active_children():
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

    # ----- Network proxy capture (V1.5) — policy-based preflight -----------
    # SCR-43: lock acquisition, preflight_or_raise, KEK/DEK generation, and
    # empty-allowlist warning are delegated to NetworkPolicy. MitmProxyV15
    # holds the lock until teardown(); Null is a no-op.
    from screencap.engine.screen_recorder import NetworkPreflightFailed as _NetworkPreflightFailed
    try:
        _network_material = network_policy.setup(capture_dir, privacy_config)
    except _NetworkPreflightFailed as exc:
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

        # SCR-40: identity files are written by ``LockPolicy.write_identity``
        # for both ``ClaimLock`` and ``InheritLock`` (per-recording identity
        # is independent of who owns the process lock).
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
                else [c.pid for c in multiprocessing.active_children()]
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
                _sentinel = {
                    "version": 1,
                    "recording_name": _recording_name,
                    "completed_at": _dt.now(_tz.utc).isoformat(),
                    "stop_reason": "force",
                    "chunks_expected": len(list(capture_dir.glob("chunk_*_manifest.json"))),
                    "sentinel_id": str(__import__('uuid').uuid4()),
                    "show_on_website": show_on_website,
                }
                (capture_dir / "recording_complete.json").write_text(
                    _json.dumps(_sentinel, indent=2)
                )
            except Exception:
                pass

            menubar_policy.kill()
            os._exit(1)

        # --- SIGTERM handler (for `screencap stop`) ---
        def _sigterm_handler(sig, frame):
            nonlocal _stop_reason
            _stop_reason = "sigterm"
            _stop_event.set()
            if recorder is not None:
                recorder.stop()

        # SCR-39: SignalPolicy decides whether to register handlers.
        # Standalone CLI passes ThreeTapSigint; session workers pass
        # NoopSignalPolicy (controller owns Ctrl+C). Installed BEFORE
        # Recorder.__enter__() so SIGINT during the entire setup window
        # is honoured — Tier-3 enforcement in
        # tests/test_signal_during_setup.py.
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

            # SCR-44: ChunkProcessor + ScrubWorker construction + start are
            # owned by the engine helper. Both consumers share the helper's
            # internal flush_lock so concurrent flush handshakes don't race
            # on the engine's flush_ack_counter.
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

            # SCR-40: pidfile snapshot of children is delegated to
            # ``LockPolicy.register_children``. ``ClaimLock`` writes the
            # pidfile; ``InheritLock`` is a no-op (controller owns it).
            child_pids = [
                {"pid": child.pid, "name": child.name}
                for child in multiprocessing.active_children()
            ]
            lock_policy.register_children(capture_dir, child_pids)

            # Store raw PIDs for signal-safe force-exit (avoids
            # multiprocessing._children_lock which can deadlock in a handler).
            _child_pids = [child.pid for child in multiprocessing.active_children()]

            # Spawn menu bar status item (non-blocking, best-effort).
            # SCR-41: SpawnNewMenubar spawns; Noop skips (worker mode).
            from screencap.config import get_first_seen_prompt_enabled
            menubar_policy.spawn(
                name, t0, capture_dir, channels,
                audio_enabled=audio,
                prompt_enabled=get_first_seen_prompt_enabled(),
            )

            # --- Live recording display ---
            # We use transient=False and handle cleanup ourselves:
            # on stop we replace the panel with the stop message via
            # live.update().  Rich's normal render cycle overwrites
            # every panel line (including borders) with the new content.
            # transient=True has an off-by-one bug with Panel borders
            # on signal interrupt, leaving the top border as a remnant.
            # ``elapsed`` is updated inside the Live loop and then frozen
            # in the loop's ``finally`` block so the returned value
            # reflects the user-visible recording duration (the number
            # shown in the live status bar) and NOT the total wall clock
            # that includes the multi-minute post-capture cleanup
            # (ChunkProcessor drain, DB upload, sentinel upload).
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

                        # SCR-42: mid-recording permission revocation watcher
                        # delegated to PermissionPolicy (Unit 8a contract).
                        from screencap.engine.screen_recorder import PermissionRevoked as _PermissionRevoked
                        try:
                            permission_policy.poll(elapsed)
                        except _PermissionRevoked as _exc:
                            if not _stop_event.is_set():
                                _stop_reason = f"permission_revoked_{_exc.missing}"
                                try:
                                    from screencap._stderr_events import (
                                        EVENT_PERMISSION_LOST,
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

                        # SCR-42: periodic disk space check delegated to DiskPolicy.
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
                            f"  \u26a0 Recording stopped: {recorder.health_warning}",
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

            # SCR-44: end-of-recording finalize runs INSIDE the engine
            # ``with``-block so chunk_processor and scrub_worker drain
            # before ``Recorder.__exit__`` closes the engine queues. This
            # is the load-bearing ordering that lets us delete
            # ``_NoCloseProxy``: no outsider holds engine-queue references
            # past ``__exit__``, so close-coordination is purely internal.
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

        # SCR-43: lock release delegated to NetworkPolicy.teardown().
        # MitmProxyV15 releases the flock it acquired in setup(); Null is a no-op.
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
                _sentinel_path.write_text(_json.dumps(_sentinel_data, indent=2))
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

    from screencap.engine.screen_recorder import RecordingResult

    return RecordingResult(capture_dir=capture_dir, elapsed=elapsed)
