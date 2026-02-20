"""Wrap openadapt-capture Recorder for screencap."""

from __future__ import annotations

import atexit
import multiprocessing
import signal
import sys
import time
import warnings
from pathlib import Path

from rich.console import Console

from screencap.config import get_audio_default, get_recordings_dir, get_wifi_metrics

console = Console()


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _fmt_size(path: Path) -> str:
    if not path.exists():
        return "—"
    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def start_recording(
    name: str,
    description: str | None = None,
    audio: bool | None = None,
    output_dir: str | Path | None = None,
    wifi_metrics: bool | None = None,
    force_clean: bool = False,
) -> Path:
    """Start a screen capture recording. Blocks until Ctrl+C."""
    if audio is None:
        audio = get_audio_default()
    if wifi_metrics is None:
        wifi_metrics = get_wifi_metrics()

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

    # Check macOS Screen Recording permission before starting
    if sys.platform == "darwin":
        try:
            from openadapt_capture.platform.darwin import DarwinPlatform

            if not DarwinPlatform.is_screen_recording_enabled():
                console.print(
                    "[red]Error:[/red] Screen Recording permission not granted.\n"
                    "  Go to: System Settings > Privacy & Security > Screen Recording\n"
                    "  Enable your terminal app, then restart it."
                )
                raise SystemExit(1)
        except ImportError:
            pass

    desc = description or ""

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

        save_metrics(capture_dir, "start", wifi_metrics=wifi_metrics)
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not collect system metrics: {e}")

    def _cleanup_children():
        for child in multiprocessing.active_children():
            child.terminate()

    atexit.register(_cleanup_children)

    # Let KeyboardInterrupt propagate naturally into the Recorder.
    # record() internally catches KeyboardInterrupt at line 1707 and
    # sets terminate_processing, then joins all child processes.
    # We just need to handle the interrupt that bubbles up to us.
    try:
        with Recorder(
            str(capture_dir),
            task_description=desc,
            capture_video=True,
            capture_audio=audio,
        ) as recorder:
            recorder.wait_for_ready(timeout=30)
            status.stop()

            # Write PID file tracking all child processes
            child_pids = [
                {"pid": child.pid, "name": child.name}
                for child in multiprocessing.active_children()
            ]
            write_pidfile(capture_dir, child_pids)

            console.print(f'[bold red]Recording "[/bold red]{name}[bold red]"... Press Ctrl+C to stop.[/bold red]')

            # Install a handler only for second Ctrl+C (force-kill)
            _ctrl_c_count = 0

            def _force_exit(sig, frame):
                nonlocal _ctrl_c_count
                _ctrl_c_count += 1
                if _ctrl_c_count == 1:
                    console.print("\n[dim]Stopping... (press Ctrl+C again to force quit)[/dim]")
                    recorder.stop()
                else:
                    console.print("\n[dim]Force quitting — terminating child processes...[/dim]")
                    for child in multiprocessing.active_children():
                        child.terminate()
                    time.sleep(1)
                    for child in multiprocessing.active_children():
                        child.kill()
                    sys.exit(1)

            signal.signal(signal.SIGINT, _force_exit)

            try:
                while recorder.is_recording:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                console.print("\n[dim]Stopping recording...[/dim]")
                recorder.stop()

    except KeyboardInterrupt:
        console.print("\n[dim]Stopping recording...[/dim]")
    finally:
        status.stop()
        # Restore default handler
        signal.signal(signal.SIGINT, signal.default_int_handler)
        atexit.unregister(_cleanup_children)
        delete_pidfile()
        # Suppress noisy multiprocessing cleanup tracebacks
        warnings.filterwarnings("ignore", category=ResourceWarning)

    try:
        from screencap.metrics import save_metrics

        save_metrics(capture_dir, "end", wifi_metrics=wifi_metrics)
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not collect end metrics: {e}")

    elapsed = time.time() - t0

    # Auto-generate viewer.html
    try:
        from openadapt_capture import create_html

        create_html(str(capture_dir), output=str(capture_dir / "viewer.html"))
        console.print("[dim]Generated viewer.html[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not generate viewer.html: {e}")

    console.print(f"\n[bold green]Saved to {capture_dir}/[/bold green]")
    console.print(f"   Duration: {_fmt_duration(elapsed)} | Size: {_fmt_size(capture_dir)}")

    video = capture_dir / "video.mp4"
    audio_file = capture_dir / "audio.flac"
    parts = []
    if video.exists():
        parts.append(f"Video: {_fmt_size(video)}")
    if audio_file.exists():
        parts.append(f"Audio: {_fmt_size(audio_file)}")
    if parts:
        console.print(f"   {' | '.join(parts)}")

    return capture_dir
