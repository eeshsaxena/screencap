"""Wrap openadapt-capture Recorder for screencap."""

from __future__ import annotations

import atexit
import multiprocessing
import os
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
from screencap.config import get_app_versions, get_audio_default, get_recordings_dir, get_wifi_metrics

console = Console()


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

def _build_live_display(name: str, elapsed: float, pulse_on: bool) -> Group:
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

    content = Text()
    content.append_text(line1)
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
        (f"screencap scrub {name}", "Remove PII"),
        (f"screencap export {name}", "Export as JSONL"),
        (f"screencap upload {name}", "Upload to cloud"),
        ("screencap list", "All recordings"),
    ]
    for cmd, desc in commands:
        console.print(f"    [bold #22d3ee]{cmd:<38}[/bold #22d3ee] [dim]{desc}[/dim]")

    console.print()


# ---------------------------------------------------------------------------
# Log suppression
# ---------------------------------------------------------------------------

def _suppress_output() -> None:
    """Suppress noisy output from the recording pipeline.

    Sets OA_LOG_LEVEL=ERROR env var so spawned child processes (which
    re-import the module) pick up the higher threshold.  Also overrides
    loguru directly in the current process (in case the module was already
    imported before this function was called).  Disables tqdm as well.

    We use ERROR (not WARNING) because the vendored code emits benign
    WARNING messages during shutdown (screenshot failures, audio timeout)
    that are expected and shouldn't clutter the user's terminal.
    """
    os.environ["OA_LOG_LEVEL"] = "ERROR"
    os.environ["TQDM_DISABLE"] = "1"
    # Override loguru directly in the main process — the env var only
    # takes effect on fresh imports (child processes).  If openadapt_capture
    # was already imported, loguru is already configured at INFO.
    try:
        from loguru import logger as _oa_logger
        _oa_logger.remove()
        _oa_logger.add(sys.stderr, level="ERROR")
    except ImportError:
        pass


def _restore_output() -> None:
    """Restore loguru and tqdm to defaults."""
    os.environ.pop("OA_LOG_LEVEL", None)
    os.environ.pop("TQDM_DISABLE", None)
    # Re-configure loguru in this process back to INFO
    try:
        from loguru import logger as _oa_logger
        _oa_logger.remove()
        _oa_logger.add(sys.stderr, level="INFO")
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# macOS permission helpers
# ---------------------------------------------------------------------------

def _open_privacy_settings(pane: str) -> None:
    """Open System Settings to a specific Privacy & Security pane.

    pane: one of 'Privacy_ScreenCapture', 'Privacy_Accessibility', 'Privacy_ListenEvent'
    """
    subprocess.run(
        ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
        check=False,
    )


def _check_macos_permissions() -> None:
    """Check macOS permissions and guide the user through granting them.

    Checks Screen Recording, Accessibility, and Input Monitoring.
    For any missing permission, triggers the native OS prompt dialog
    and opens System Settings to the relevant pane.
    """
    if sys.platform != "darwin":
        return

    try:
        from openadapt_capture.platform.darwin import DarwinPlatform
    except ImportError:
        return

    missing: list[tuple[str, str, bool]] = []

    if not DarwinPlatform.is_screen_recording_enabled():
        DarwinPlatform.request_screen_recording_access()
        missing.append(("Screen Recording", "Privacy_ScreenCapture", True))

    if not DarwinPlatform.is_accessibility_enabled():
        DarwinPlatform.request_accessibility_access()
        missing.append(("Accessibility", "Privacy_Accessibility", False))

    if not DarwinPlatform.is_input_monitoring_enabled():
        DarwinPlatform.request_input_monitoring_access()
        missing.append(("Input Monitoring", "Privacy_ListenEvent", False))

    if not missing:
        return

    # Open System Settings to the first missing permission's pane
    _open_privacy_settings(missing[0][1])

    # Build the error message
    names = ", ".join(m[0] for m in missing)
    needs_restart = any(m[2] for m in missing)

    console.print(f"\n[red]Error:[/red] Missing permissions: {names}.")
    console.print("  System Settings has been opened for you.")
    console.print(f"  Enable [bold]{missing[0][0]}[/bold] for your terminal app.")

    if len(missing) > 1:
        others = ", ".join(m[0] for m in missing[1:])
        console.print(f"  Also enable: {others}")

    if needs_restart:
        console.print("\n  [yellow]Note:[/yellow] Screen Recording requires a terminal restart.")
        console.print("  After enabling, quit and reopen your terminal, then re-run:")
    else:
        console.print("\n  After enabling, re-run:")

    console.print("    screencap start")

    raise SystemExit(1)


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
    capture_browser_events: bool | None = None,
    verbose: bool = False,
) -> tuple[Path, float]:
    """Start a screen capture recording. Blocks until Ctrl+C."""
    if audio is None:
        audio = get_audio_default()
    if wifi_metrics is None:
        wifi_metrics = get_wifi_metrics()
    if app_versions is None:
        app_versions = get_app_versions()

    # Check for orphaned processes from a previous recording
    from screencap.pidfile import (
        delete_pidfile,
        find_orphaned_processes,
        terminate_processes,
        write_pidfile,
    )

    orphans = find_orphaned_processes()
    if orphans:
        if force_clean:
            console.print(f"[yellow]Cleaning up {len(orphans)} orphaned process(es) from a previous recording...[/yellow]")
            terminate_processes(orphans, force=True)
            delete_pidfile()
        else:
            console.print(
                f"[yellow]Warning:[/yellow] Found {len(orphans)} orphaned process(es) from a previous recording.\n"
                "  Run 'screencap stop' to clean them up, or pass --force to auto-clean."
            )
            raise SystemExit(1)

    if output_dir:
        capture_dir = Path(output_dir)
    else:
        capture_dir = get_recordings_dir() / name

    if capture_dir.exists() and any(capture_dir.iterdir()):
        console.print(
            f"[red]Error:[/red] Directory already exists and is not empty: {capture_dir}"
        )
        raise SystemExit(1)

    capture_dir.mkdir(parents=True, exist_ok=True)

    # Suppress loguru/tqdm noise unless --verbose.
    # Must happen BEFORE any openadapt_capture import (including the
    # screen-recording permission check below) so the env var is set
    # when the module-level loguru config runs for the first time.
    if not verbose:
        _suppress_output()

    # Check macOS permissions (Screen Recording, Accessibility, Input Monitoring)
    _check_macos_permissions()

    desc = description or ""

    # --- Banner ---
    _print_banner()

    if verbose:
        console.print(f"[dim]Audio: {'on' if audio else 'off'}[/dim]")

    t0 = time.time()
    status = console.status("[bold]Initializing capture...[/bold]")
    status.start()

    # Heavy import — deferred here to keep `screencap --help` fast.
    from openadapt_capture import Recorder

    if Recorder is None:
        status.stop()
        console.print(
            "[red]Error:[/red] Recorder not available. "
            "Check that all dependencies are installed (pynput, mss, etc.)"
        )
        raise SystemExit(1)

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
    _stop_reason = ""  # "graceful", "force", or "interrupt"
    _stop_event = threading.Event()
    _saved_stdout = None  # Will hold real stdout when we redirect to devnull
    _saved_stderr = None  # Will hold real stderr when we redirect to devnull

    try:
        # Build Recorder kwargs, only passing non-None values
        recorder_kwargs: dict = {
            "task_description": desc,
            "capture_audio": audio,
        }
        if capture_video is not None:
            recorder_kwargs["capture_video"] = capture_video
        else:
            recorder_kwargs["capture_video"] = True
        if capture_images is not None:
            recorder_kwargs["capture_images"] = capture_images
        if capture_window_data is not None:
            recorder_kwargs["capture_window_data"] = capture_window_data
        if capture_browser_events is not None:
            recorder_kwargs["capture_browser_events"] = capture_browser_events

        with Recorder(
            str(capture_dir),
            **recorder_kwargs,
        ) as recorder:
            recorder.wait_for_ready(timeout=30)
            status.stop()

            # Write PID file tracking all child processes
            child_pids = [
                {"pid": child.pid, "name": child.name}
                for child in multiprocessing.active_children()
            ]
            write_pidfile(capture_dir, child_pids)

            # --- SIGINT handler (flag-based, no console.print inside) ---
            _ctrl_c_count = 0

            def _force_exit(sig, frame):
                nonlocal _ctrl_c_count, _stop_reason
                _ctrl_c_count += 1
                if _ctrl_c_count == 1:
                    _stop_reason = "graceful"
                    _stop_event.set()
                    recorder.stop()
                else:
                    _stop_reason = "force"
                    _stop_event.set()
                    for child in multiprocessing.active_children():
                        child.terminate()
                    time.sleep(1)
                    for child in multiprocessing.active_children():
                        child.kill()
                    sys.exit(1)

            signal.signal(signal.SIGINT, _force_exit)

            # --- Live recording display ---
            # We use transient=False and handle cleanup ourselves:
            # on stop we replace the panel with the stop message via
            # live.update().  Rich's normal render cycle overwrites
            # every panel line (including borders) with the new content.
            # transient=True has an off-by-one bug with Panel borders
            # on signal interrupt, leaving the top border as a remnant.
            with Live(
                _build_live_display(name, 0.0, True),
                console=console,
                refresh_per_second=2,
            ) as live:
                try:
                    while recorder.is_recording and not _stop_event.is_set():
                        elapsed = time.time() - t0
                        pulse_on = int(elapsed) % 2 == 0
                        live.update(_build_live_display(name, elapsed, pulse_on))
                        _stop_event.wait(0.5)
                finally:
                    # Replace the panel with the stop message.  Rich
                    # knows the panel height and will overwrite every
                    # line, including borders.  The message stays on
                    # screen because transient=False (default).
                    if _stop_reason == "graceful":
                        live.update(Text("  ■ Stopping recording...", style="#a78bfa"))
                    elif _stop_reason == "force":
                        live.update(Text("  ⚡ Force quitting — terminating processes...", style="#f472b6"))
                    else:
                        live.update(Text(""))

            # Suppress ALL output before Recorder.__exit__ runs:
            # 1. Redirect stdout/stderr for print() calls (Recording Profile)
            # 2. Remove loguru handlers — loguru caches the original stderr
            #    file object, so sys.stderr = devnull doesn't stop it.
            if not verbose:
                try:
                    from loguru import logger as _oa_logger
                    _oa_logger.remove()
                except Exception:
                    pass
                _saved_stdout = sys.stdout
                _saved_stderr = sys.stderr
                _devnull = open(os.devnull, "w")
                sys.stdout = _devnull
                sys.stderr = _devnull

    except KeyboardInterrupt:
        _stop_reason = "interrupt"
        console.print("  [dim]■ Stopping recording...[/dim]")
        # Suppress profile block on interrupt path
        if not verbose:
            try:
                from loguru import logger as _oa_logger
                _oa_logger.remove()
            except Exception:
                pass
            _saved_stdout = sys.stdout
            _saved_stderr = sys.stderr
            _devnull = open(os.devnull, "w")
            sys.stdout = _devnull
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
        # Restore default handler
        signal.signal(signal.SIGINT, signal.default_int_handler)
        atexit.unregister(_cleanup_children)
        delete_pidfile()
        # Suppress noisy multiprocessing cleanup tracebacks
        warnings.filterwarnings("ignore", category=ResourceWarning)

    try:
        from screencap.metrics import save_metrics

        save_metrics(capture_dir, "end", wifi_metrics=wifi_metrics, app_versions=app_versions)
    except Exception as e:
        if verbose:
            console.print(f"[yellow]Warning:[/yellow] Could not collect end metrics: {e}")

    elapsed = time.time() - t0

    # Auto-generate viewer.html — disabled (too slow, does N full video scans).
    # Run `screencap view <name>` to generate on demand instead.

    # Restore output if we suppressed it
    if not verbose:
        _restore_output()

    return capture_dir, elapsed
