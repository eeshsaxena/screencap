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
# Menu bar helpers — implementations live in ``engine.menubar_policy``;
# re-exported here so existing test patches via
# ``mock.patch("screencap.recorder._spawn_menubar")`` and
# ``...._kill_menubar`` continue to resolve.
# ---------------------------------------------------------------------------

from screencap.engine.menubar_policy import _kill_menubar, _spawn_menubar  # noqa: E402, F401


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
    _lock_policy: "LockPolicy",
    _permission_policy: "PermissionPolicy | None" = None,
    _disk_policy: "DiskPolicy | None" = None,
    _network_policy: "NetworkPolicy | None" = None,
    network_handoff_ready=None,
) -> tuple[Path, float, multiprocessing.Process | None, Path | None]:
    """Start a screen capture recording. Blocks until Ctrl+C.

    Thin CLI adapter: builds the seam bundles and delegates the entire
    recording lifecycle to ``engine.ScreenRecorder.run()``. The 4-tuple
    return is preserved for backward compat with existing callers and
    tests; the trailing two elements (``menubar_proc`` and
    ``menubar_state_file``) are surfaced from the active menubar policy
    so callers that still need to manage the subprocess directly (e.g.,
    ``DiskFullError``) can reach it without touching the seam.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.disk_policy import MonitorAndStop
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
    signal = _signal_policy if _signal_policy is not None else ThreeTapSigint()
    # ``_lock_policy`` is REQUIRED: callers must consciously choose their
    # process-exclusion policy. ``InheritLock`` (a no-op claim/register/
    # release that only writes identity files) is correct ONLY inside a
    # daemon-spawned worker, where the supervisor already owns the
    # process-exclusive pidfile (``daemon/supervisor.py`` →
    # ``pidfile.claim_lock``). A direct caller that passes ``InheritLock``
    # gets no exclusion; two concurrent ``start_recording`` calls for the
    # same name still race ``recording.db`` and friends. Requiring the
    # argument lifts that daemon-only invariant from prose to a
    # ``TypeError`` at the call boundary (SCR-66).
    lock = _lock_policy
    permission = _permission_policy if _permission_policy is not None else MacOSTCC()
    disk = _disk_policy if _disk_policy is not None else MonitorAndStop()
    net = _network_policy if _network_policy is not None else (_MitmProxyV15() if network else _NetworkNull())
    policies = RecordingPolicies(
        signal=signal,
        lock=lock,
        menubar=menubar,
        permission=permission,
        disk=disk,
        network=net,
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

# ---------------------------------------------------------------------------
# Engine recording lifecycle — re-export for the seam parity test.
#
# The body of the recording lifecycle now lives in
# ``screencap.engine.screen_recorder._run_screen_recorder``. The re-export
# here keeps two existing call sites working without modification:
#
#   * ``test_recorder.TestForceExitContracts`` AST-introspects
#     ``screencap.recorder._run_screen_recorder`` to assert the
#     ``_force_exit`` / ``_sigterm_handler`` inner functions guard
#     ``recorder.stop()`` with ``recorder is not None`` and call
#     ``lock_policy.release()``. ``inspect.getsource`` resolves to the
#     engine source through this binding.
#   * ``ScreenRecorder.run()`` calls the engine implementation directly,
#     so this binding is not on the runtime hot path.
# ---------------------------------------------------------------------------

from screencap.engine.screen_recorder import _run_screen_recorder  # noqa: E402, F401
