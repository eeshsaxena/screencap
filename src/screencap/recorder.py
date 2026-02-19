"""Wrap openadapt-capture Recorder for screencap."""

from __future__ import annotations

import os
import signal
import time
import warnings
from pathlib import Path

from rich.console import Console

from screencap.config import get_audio_default, get_recordings_dir

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
) -> Path:
    """Start a screen capture recording. Blocks until Ctrl+C."""
    from openadapt_capture import Recorder

    if Recorder is None:
        console.print(
            "[red]Error:[/red] Recorder not available. "
            "Check that all dependencies are installed (pynput, mss, etc.)"
        )
        raise SystemExit(1)

    if audio is None:
        audio = get_audio_default()

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
    desc = description or ""

    console.print(f'[bold red]Recording "[/bold red]{name}[bold red]"... Press Ctrl+C to stop.[/bold red]')
    console.print(f"[dim]Audio: {'on' if audio else 'off'}[/dim]")

    t0 = time.time()

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

            # Install a handler only for second Ctrl+C (force-kill)
            _ctrl_c_count = 0

            def _force_exit(sig, frame):
                nonlocal _ctrl_c_count
                _ctrl_c_count += 1
                if _ctrl_c_count == 1:
                    console.print("\n[dim]Stopping... (press Ctrl+C again to force quit)[/dim]")
                    recorder.stop()
                else:
                    console.print("\n[dim]Force quitting.[/dim]")
                    os._exit(1)

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
        # Restore default handler
        signal.signal(signal.SIGINT, signal.default_int_handler)
        # Suppress noisy multiprocessing cleanup tracebacks
        warnings.filterwarnings("ignore", category=ResourceWarning)

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
