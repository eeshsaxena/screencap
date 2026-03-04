"""ScreenCap CLI — Click-based entry point."""

from __future__ import annotations

import json
import sys

import click
from rich.console import Console
from rich.table import Table

from screencap import __version__

console = Console()

_RECORD_EXTRAS_MSG = (
    "[red]Error: This command requires recording dependencies.[/red]\n"
    "Install them with: [bold]pip install screencap\\[record][/bold]"
)


@click.group()
@click.version_option(version=__version__, prog_name="screencap")
@click.option("--no-update-check", is_flag=True, hidden=True,
              help="Skip auto-update check.")
@click.pass_context
def cli(ctx, no_update_check):
    """ScreenCap — macOS screen capture."""
    if not no_update_check:
        from screencap.updater import maybe_check_for_update
        maybe_check_for_update()


@cli.command()
@click.option("--name", "-n", default=None, help="Recording name (skips auto-naming).")
@click.option("--description", "-d", default=None, help="Task description.")
@click.option("--no-audio", is_flag=True, default=False, help="Disable audio capture.")
@click.option("--no-video", is_flag=True, default=False, help="Disable video capture.")
@click.option("--no-images", is_flag=True, default=False, help="Disable screenshot capture.")
@click.option("--no-window-data", is_flag=True, default=False, help="Disable window/accessibility data.")
@click.option("--no-browser-events", is_flag=True, default=False, help="Disable browser event capture.")
@click.option("--output", "-o", type=click.Path(), default=None, help="Custom output directory.")
@click.option("--no-wifi-metrics", is_flag=True, default=False, help="Disable WiFi metrics collection.")
@click.option("--no-app-versions", is_flag=True, default=False, help="Disable running app version capture.")
@click.option("--no-auto-name", is_flag=True, default=False, help="Skip LLM auto-naming after recording.")
@click.option("--local-only", is_flag=True, default=False, help="Restrict LLM naming to local providers (Ollama).")
@click.option("--force", is_flag=True, default=False, help="Auto-clean orphaned processes before starting.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show all info/debug output during recording.")
def start(
    name, description, no_audio, no_video, no_images, no_window_data,
    no_browser_events, output, no_wifi_metrics, no_app_versions,
    no_auto_name, local_only, force, verbose,
):
    """Record a screen capture session. Ctrl+C to stop."""
    from datetime import datetime

    from screencap.config import get_auto_name, get_auto_name_local_only

    # Determine if auto-naming is enabled
    user_provided_name = name is not None
    auto_name_enabled = get_auto_name() and not no_auto_name and not user_provided_name
    local_only = local_only or get_auto_name_local_only()

    if not name:
        if no_auto_name:
            # Restore old interactive prompt behavior
            if not sys.stdin.isatty():
                console.print("[red]Error: --name is required with --no-auto-name in non-interactive mode.[/red]")
                raise SystemExit(1)
            name = click.prompt("Recording name")
            description = description or click.prompt("Description (optional)", default="", show_default=False)
        else:
            # Generate timestamp-based temp name
            name = f"rec-{datetime.now().strftime('%Y%m%dT%H%M%S')}"

    audio = not no_audio
    wifi_metrics = not no_wifi_metrics
    app_versions = not no_app_versions

    # Capture flags: default to True for images and window data (opt-out)
    capture_video = False if no_video else None  # None = use upstream default (True)
    capture_images = False if no_images else True  # Default ON (overrides upstream False)
    capture_window_data = False if no_window_data else None  # None = upstream default (True)
    capture_browser_events = False if no_browser_events else None  # None = upstream default (False)

    try:
        from screencap.recorder import DiskFullError, print_summary, start_recording
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    disk_full = False
    try:
        capture_dir, elapsed = start_recording(
            name, description or None, audio, output,
            wifi_metrics=wifi_metrics, app_versions=app_versions, force_clean=force,
            capture_video=capture_video, capture_images=capture_images,
            capture_window_data=capture_window_data, capture_browser_events=capture_browser_events,
            verbose=verbose,
        )
    except DiskFullError as e:
        capture_dir, elapsed = e.capture_dir, e.elapsed
        disk_full = True
        console.print("[yellow]Skipping auto-naming/transcription: disk space is low.[/yellow]")
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    # --- Post-recording pipeline ---
    final_name = name
    final_dir = capture_dir

    if auto_name_enabled and not disk_full:
        # Auto-transcribe if audio was captured
        audio_path = capture_dir / "audio.flac"
        if audio and audio_path.exists() and audio_path.stat().st_size >= 1024:
            try:
                _auto_transcribe(capture_dir, audio_path)
            except KeyboardInterrupt:
                console.print("[yellow]Transcription cancelled.[/yellow]")

        # LLM auto-naming
        skip_rename = output is not None  # User chose a specific path
        try:
            from screencap.namer import auto_name as do_auto_name

            with console.status("[bold]Generating name...[/bold]"):
                final_dir = do_auto_name(
                    capture_dir,
                    local_only=local_only,
                    skip_rename=skip_rename,
                )
            final_name = final_dir.name
        except KeyboardInterrupt:
            console.print("[yellow]Naming cancelled — keeping timestamp name[/yellow]")

    print_summary(final_name, final_dir, elapsed)


def _auto_transcribe(capture_dir, audio_path):
    """Auto-transcribe audio using the fastest available backend."""
    import os

    transcript_path = capture_dir / "transcript.txt"
    transcript_json_path = capture_dir / "transcript.json"

    # Skip if already transcribed
    if transcript_path.exists():
        return

    # Try faster-whisper first
    try:
        import faster_whisper  # noqa: F401
        from sc_engine.cli import _transcribe_faster_whisper

        with console.status("[bold]Transcribing audio...[/bold]"):
            # Suppress print() calls from vendored code
            _orig = sys.stdout
            sys.stdout = open(os.devnull, "w")
            try:
                _transcribe_faster_whisper(audio_path, transcript_path, transcript_json_path, "base")
            finally:
                sys.stdout.close()
                sys.stdout = _orig
        return
    except ImportError:
        pass

    # Try openai-whisper
    try:
        from sc_engine.cli import _transcribe_local

        with console.status("[bold]Transcribing audio...[/bold]"):
            _orig = sys.stdout
            sys.stdout = open(os.devnull, "w")
            try:
                _transcribe_local(audio_path, transcript_path, transcript_json_path, "base")
            finally:
                sys.stdout.close()
                sys.stdout = _orig
        return
    except ImportError:
        pass

    # Try OpenAI API
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            with console.status("[bold]Transcribing audio via API...[/bold]"):
                _transcribe_api_inline(api_key, audio_path, transcript_path, transcript_json_path)
            return
        except Exception:
            pass

    # No transcription backend available — not an error


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option(
    "--sort",
    type=click.Choice(["name", "date", "duration"], case_sensitive=False),
    default="date",
    help="Sort column.",
)
def list_cmd(as_json, sort):
    """List all recordings."""
    from screencap.catalog import list_recordings

    recordings = list_recordings()

    if not recordings:
        console.print("[dim]No recordings found.[/dim]")
        return

    # Sort
    sort_keys = {
        "name": lambda r: r.name,
        "date": lambda r: r.date,
        "duration": lambda r: r.duration,
    }
    recordings.sort(key=sort_keys[sort])

    if as_json:
        click.echo(
            json.dumps(
                [r._asdict() for r in recordings],
                indent=2,
            )
        )
        return

    table = Table(show_header=True, header_style="bold #60a5fa")
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Date")
    table.add_column("Duration")
    table.add_column("Size")
    table.add_column("Audio")
    table.add_column("Transcribed")
    table.add_column("Uploaded")

    for i, r in enumerate(recordings, 1):
        name_display = r.name
        if r.drops:
            name_display += " [yellow]\u26a0[/yellow]"
        table.add_row(
            str(i),
            name_display,
            r.date,
            r.duration,
            r.size_mb,
            "[green]\u2713[/green]" if r.has_audio else "[dim]\u2717[/dim]",
            "[green]\u2713[/green]" if r.transcribed else "[dim]\u2717[/dim]",
            "[green]\u2713[/green]" if r.uploaded else "[dim]\u2717[/dim]",
        )

    console.print(table)


@cli.command()
@click.argument("name")
def view(name):
    """Open recording viewer in browser."""
    from screencap.viewer import open_viewer

    try:
        open_viewer(name)
        console.print(f"[dim]Opening {name}/viewer.html ...[/dim]")
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        sys.exit(1)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def info(name, as_json):
    """Show details and system metrics for a recording."""
    from screencap.catalog import find_db, read_drops, _read_recording_meta
    from screencap.config import get_recordings_dir

    try:
        from screencap.metrics import METRICS_FILENAME
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    recording_dir = get_recordings_dir() / name
    if not recording_dir.exists():
        console.print(f"[red]Error:[/red] Recording not found: {name}")
        sys.exit(1)

    # Read DB metadata
    db_path = find_db(recording_dir)
    rec_meta = {}
    if db_path:
        from datetime import datetime

        started, duration = _read_recording_meta(db_path)
        if started:
            rec_meta["date"] = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S")
        if duration:
            m, s = divmod(int(duration), 60)
            h, m = divmod(m, 60)
            rec_meta["duration"] = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    # Read drop counts
    drops = read_drops(recording_dir)

    # Read metrics
    metrics_path = recording_dir / METRICS_FILENAME
    metrics = None
    if metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if as_json:
        click.echo(json.dumps({"recording": rec_meta, "metrics": metrics, "drops": drops}, indent=2))
        return

    # Human-readable output
    from rich.panel import Panel

    console.print(Panel(f"[bold]{name}[/bold]", title="Recording"))

    if rec_meta:
        for key, val in rec_meta.items():
            console.print(f"  [#60a5fa]{key}:[/#60a5fa] {val}")
    else:
        console.print("  [dim]No recording metadata available.[/dim]")

    if isinstance(drops, dict) and any(v > 0 for v in drops.values()):
        console.print(f"\n  [yellow]Events dropped during recording:[/yellow]")
        for event_type, count in drops.items():
            if count > 0:
                console.print(f"    [yellow]{event_type}:[/yellow] {count}")

    if metrics is None:
        console.print("\n[dim]No system metrics available (recorded before metrics feature).[/dim]")
        return

    static = metrics.get("static", {})
    if static:
        console.print(Panel("[bold]System Info[/bold]"))
        for key, val in static.items():
            if key == "displays":
                for i, d in enumerate(val):
                    console.print(f"  [#60a5fa]display {i}:[/#60a5fa] {d.get('width')}x{d.get('height')}")
            elif key == "locale" and isinstance(val, dict):
                console.print(f"  [#60a5fa]locale:[/#60a5fa]")
                for lk, lv in val.items():
                    if isinstance(lv, list):
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {', '.join(str(x) for x in lv)}")
                    elif isinstance(lv, dict):
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {lv}")
                    else:
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {lv}")
            elif key == "running_applications" and isinstance(val, list):
                console.print(f"  [#60a5fa]running apps:[/#60a5fa]")
                for app in val:
                    v = f" v{app['version']}" if app.get("version") else ""
                    console.print(f"    {app['name']} ({app['bundle_id']}){v}")
            elif key == "wifi" and isinstance(val, dict):
                console.print(f"  [#60a5fa]wifi:[/#60a5fa]")
                for wk, wv in val.items():
                    console.print(f"    [#60a5fa]{wk}:[/#60a5fa] {wv}")
            else:
                console.print(f"  [#60a5fa]{key}:[/#60a5fa] {val}")

    for phase in ("start", "end"):
        snapshot = metrics.get(phase)
        if snapshot:
            console.print(Panel(f"[bold]{phase.title()} Snapshot[/bold]"))
            for key, val in snapshot.items():
                if key == "wifi" and isinstance(val, dict):
                    console.print(f"  [#60a5fa]wifi:[/#60a5fa]")
                    for wk, wv in val.items():
                        console.print(f"    [#60a5fa]{wk}:[/#60a5fa] {wv}")
                else:
                    console.print(f"  [#60a5fa]{key}:[/#60a5fa] {val}")
        elif phase == "end":
            console.print(f"\n  [dim]No end snapshot (recording may have been interrupted).[/dim]")


def _export_one(recording_dir, output_path, exclude_moves, err_console):
    """Export a single recording. Returns event count, or -1 on error."""
    from screencap.exporter import build_export_metadata, export_recording

    meta = build_export_metadata(exclude_moves)
    count = export_recording(recording_dir, output_path, exclude_moves, metadata=meta)
    if count == -1:
        err_console.print(
            f"[red]Error:[/red] No recording.db found in {recording_dir.name}. "
            "Legacy capture.db format is not supported for export."
        )
    return count


def _find_exportable_dirs(base_dir):
    """Find recording directories eligible for export under *base_dir*."""
    if not base_dir.is_dir():
        return []
    return sorted(
        d for d in base_dir.iterdir()
        if d.is_dir()
        and (d / "recording.db").exists()
    )


@cli.command()
@click.argument("name", required=False, default=None)
@click.option("--all", "all_recordings", is_flag=True, help="Export all recordings.")
@click.option("--downloads", is_flag=True, help="Include downloaded recordings (with --all) or search downloads dir.")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Output file path. Default: events.jsonl in the recording directory.")
@click.option("--stdout", "use_stdout", is_flag=True, default=False,
              help="Write to stdout instead of a file.")
@click.option("--exclude-moves", is_flag=True, default=False,
              help="Exclude mouse move events from output.")
def export(name, all_recordings, downloads, output, use_stdout, exclude_moves):
    """Export recording events as JSONL for training.

    WARNING: Export includes all captured keystrokes (passwords, API keys,
    private messages). Review recordings for sensitive data before sharing.
    """
    from screencap.config import get_recordings_dir, resolve_recording_dir

    err_console = Console(stderr=True)

    batch_mode = all_recordings or (downloads and not name)

    if not name and not batch_mode:
        err_console.print("[red]Error:[/red] Provide a recording name or use --all / --downloads.")
        sys.exit(1)

    if batch_mode and (use_stdout or output):
        err_console.print("[red]Error:[/red] --all/--downloads cannot be used with --stdout or -o.")
        sys.exit(1)

    try:
        from sc_engine import Capture  # noqa: F401
    except ImportError:
        err_console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    err_console.print(
        "[yellow]Warning:[/yellow] Export includes all captured keystrokes. "
        "Review recordings for sensitive data before sharing.",
        highlight=False,
    )

    if batch_mode:
        from screencap.config import get_downloads_dir

        dirs = []
        if all_recordings:
            dirs.extend(_find_exportable_dirs(get_recordings_dir()))
        if downloads:
            dirs.extend(_find_exportable_dirs(get_downloads_dir()))

        if not dirs:
            err_console.print("[dim]No recordings found.[/dim]")
            return

        total = len(dirs)
        exported = 0
        failed = 0
        for i, rec_dir in enumerate(dirs, 1):
            err_console.print(f"\n[bold][{i}/{total}][/bold] {rec_dir.name}")
            out = str(rec_dir / "events.jsonl")
            count = _export_one(rec_dir, out, exclude_moves, err_console)
            if count >= 0:
                err_console.print(f"Exported {count} events to [bold]{out}[/bold]")
                exported += 1
            else:
                failed += 1

        err_console.print(f"\n[bold]Done.[/bold] {exported} exported, {failed} failed.")
        if failed:
            sys.exit(1)
        return

    # Single recording — check recordings dir first, then downloads
    try:
        recording_dir = resolve_recording_dir(name)
    except ValueError:
        err_console.print("[red]Error:[/red] Invalid recording name.")
        sys.exit(1)

    if not recording_dir.exists() and downloads:
        from screencap.config import get_downloads_dir
        recording_dir = get_downloads_dir() / name

    if not recording_dir.exists():
        err_console.print(f"[red]Error:[/red] Recording not found: {name}")
        sys.exit(1)

    # Resolve output destination
    if use_stdout:
        output_path = None
    elif output:
        output_path = output
    else:
        output_path = str(recording_dir / "events.jsonl")

    count = _export_one(recording_dir, output_path, exclude_moves, err_console)
    if count < 0:
        sys.exit(1)
    if output_path:
        err_console.print(f"Exported {count} events to [bold]{output_path}[/bold]")


@cli.command()
@click.option("--force", is_flag=True, help="Skip SIGTERM and go straight to SIGKILL.")
def stop(force):
    """Stop orphaned recording processes."""
    try:
        from screencap.pidfile import (
            delete_pidfile,
            find_orphaned_processes,
            terminate_processes,
        )
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    orphans = find_orphaned_processes()
    if not orphans:
        console.print("[dim]No orphaned recording processes found.[/dim]")
        return

    console.print(f"Found {len(orphans)} orphaned recording process(es).")
    terminated = terminate_processes(orphans, force=force)

    for entry in terminated:
        console.print(f"  Terminated {entry.get('name', 'unknown')} (PID {entry['pid']})... done")

    if terminated:
        console.print(f"Cleaned up {len(terminated)} process(es).")
    else:
        console.print("[yellow]Could not terminate any processes.[/yellow]")

    delete_pidfile()


@cli.command()
def update():
    """Check for and install updates."""
    from screencap.updater import (
        get_latest_version,
        is_update_available,
        perform_update,
    )

    if not getattr(sys, "frozen", False):
        console.print("[yellow]Self-update is only available for standalone binary installs.[/yellow]")
        console.print("Use [bold]pip install --upgrade screencap[/bold] instead.")
        return

    console.print(f"Current version: {__version__}")
    latest = get_latest_version()

    if latest is None:
        console.print("[red]Could not reach update server.[/red]")
        return

    if not is_update_available(latest):
        console.print("[green]Already up to date.[/green]")
        return

    console.print(f"New version available: {latest}")
    if perform_update(latest):
        console.print("Restart screencap to use the new version.")


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

_WHISPER_MODELS = {
    "1": ("tiny", "~39 MB", "Fast, lower accuracy"),
    "2": ("base", "~140 MB", "Good balance (recommended)"),
    "3": ("small", "~466 MB", "Better accuracy"),
    "4": ("medium", "~1.5 GB", "High accuracy"),
    "5": ("large", "~2.9 GB", "Best accuracy"),
}


def _resolve_backend_interactive() -> tuple[str, str | None]:
    """Discover API key / prompt user and return (backend, api_key_or_model).

    Returns:
        ("api", api_key) — use OpenAI API with this key
        ("local", model_name) — use local whisper
    """
    import os

    # 1. Check for existing API key
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        try:
            from sc_engine.config import settings
            api_key = settings.openai_api_key
        except Exception:
            pass

    if api_key:
        console.print("[green]Found OpenAI API key[/green]")
        if click.confirm(
            "Use OpenAI API for transcription? (faster, costs ~$0.006/min)",
            default=True,
        ):
            return "api", api_key
        # user declined — fall through to local
        return _select_local_model()

    # 2. No key found — offer choices
    console.print("[dim]No OpenAI API key found.[/dim]")
    choice = click.prompt(
        "Choose an option\n"
        "  1. Enter OpenAI API key\n"
        "  2. Transcribe locally with Whisper\n"
        "Choice",
        type=click.IntRange(1, 2),
        default=2,
    )

    if choice == 1:
        key = click.prompt("OpenAI API key", hide_input=True)
        return "api", key

    return _select_local_model()


_MODEL_RANK = ["large", "medium", "small", "base", "tiny"]


def _detect_cached_models() -> list[str]:
    """Return whisper model names already downloaded, best-quality first."""
    from pathlib import Path
    import os

    found = set()

    # faster-whisper: ~/.cache/huggingface/hub/models--Systran--faster-whisper-{name}/
    hf_cache = Path(os.environ.get("HF_HUB_CACHE", "")) if os.environ.get("HF_HUB_CACHE") else (
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )
    if hf_cache.is_dir():
        for entry in hf_cache.iterdir():
            if entry.is_dir() and entry.name.startswith("models--Systran--faster-whisper-"):
                model = entry.name.split("faster-whisper-", 1)[1]
                if model in _MODEL_RANK:
                    found.add(model)

    # openai-whisper: ~/.cache/whisper/{name}.pt
    whisper_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whisper"
    if whisper_cache.is_dir():
        for entry in whisper_cache.iterdir():
            if entry.suffix == ".pt":
                model = entry.stem.split(".")[0]  # handles "base.en.pt" → "base"
                if model in _MODEL_RANK:
                    found.add(model)

    return [m for m in _MODEL_RANK if m in found]


def _ensure_whisper_backend() -> None:
    """Make sure at least one local whisper backend is installed."""
    try:
        import faster_whisper  # noqa: F401
        return
    except ImportError:
        pass
    try:
        import whisper  # noqa: F401
        return
    except ImportError:
        pass

    if getattr(sys, 'frozen', False):
        console.print("[red]Whisper dependencies missing from binary. Reinstall screencap.[/red]")
    else:
        console.print("[red]Local transcription requires whisper dependencies.[/red]")
        console.print("Run: pip install faster-whisper")
    raise SystemExit(1)


def _select_local_model(model: str | None = None) -> tuple[str, str]:
    """Pick a local whisper model. Auto-detects cached models to skip prompts.

    Args:
        model: Explicit model name (from --model flag). Skips all detection/prompts.

    Returns ("local", model_name).
    """
    _ensure_whisper_backend()

    # Explicit --model flag: use it directly
    if model:
        return "local", model

    # Auto-detect cached models
    cached = _detect_cached_models()
    if cached:
        best = cached[0]
        console.print(f"[green]Using cached Whisper model:[/green] {best}")
        if len(cached) > 1:
            others = ", ".join(cached[1:])
            console.print(f"[dim]Also available locally: {others}. Use --model to switch.[/dim]")
        return "local", best

    # Nothing cached — show selection menu
    console.print("\nAvailable models:")
    for num, (name, size, desc) in _WHISPER_MODELS.items():
        console.print(f"  {num}. {name:8s} ({size:8s}) — {desc}")

    choice = click.prompt(
        "Select model",
        type=click.IntRange(1, 5),
        default=2,
    )
    model_name = _WHISPER_MODELS[str(choice)][0]
    return "local", model_name


def _transcribe_api_inline(api_key, audio_path, transcript_path, transcript_json_path):
    """Transcribe using OpenAI Whisper API with an explicit api_key.

    Avoids delegating to sc_engine (whose pydantic-settings singleton
    ignores os.environ changes after import).
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    with open(audio_path, "rb") as audio_file:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    transcript = result.text.strip()

    segments = []
    for segment in getattr(result, "segments", []) or []:
        segments.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })

    # Save files directly (no print() calls — caller controls output)
    transcript_path.write_text(transcript, encoding="utf-8")
    transcript_json_path.write_text(
        json.dumps({"text": transcript, "segments": segments}, indent=2),
        encoding="utf-8",
    )


def _run_api_transcription(api_key, audio_path, transcript_path, transcript_json_path):
    """Try API transcription, retry with new key on auth failure, or fall back to local."""
    while True:
        try:
            with console.status("[bold]Transcribing with OpenAI API ...[/bold]"):
                _transcribe_api_inline(
                    api_key, audio_path, transcript_path, transcript_json_path
                )
            return  # success
        except Exception as e:
            is_auth_error = "401" in str(e) or "invalid_api_key" in str(e)
            if not is_auth_error:
                console.print(f"[red]Transcription failed:[/red] {e}")
                sys.exit(1)

            console.print(f"[red]Invalid API key.[/red]")
            choice = click.prompt(
                "What would you like to do?\n"
                "  1. Enter a different API key\n"
                "  2. Transcribe locally with Whisper instead\n"
                "  3. Cancel\n"
                "Choice",
                type=click.IntRange(1, 3),
                default=1,
            )

            if choice == 1:
                api_key = click.prompt("OpenAI API key", hide_input=True).strip()
                continue
            elif choice == 2:
                _, model = _select_local_model()
                try:
                    try:
                        import faster_whisper  # noqa: F401
                        from sc_engine.cli import _transcribe_faster_whisper
                        local_fn = _transcribe_faster_whisper
                    except ImportError:
                        from sc_engine.cli import _transcribe_local
                        local_fn = _transcribe_local

                    console.print(f"[dim]Using Whisper ({model} model)...[/dim]")
                    local_fn(audio_path, transcript_path, transcript_json_path, model)
                except Exception as exc:
                    console.print(f"[red]Transcription failed:[/red] {exc}")
                    sys.exit(1)
                return
            else:
                console.print("[dim]Transcription cancelled.[/dim]")
                sys.exit(0)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--all", "all_recordings", is_flag=True, help="Upload all recordings.")
@click.option("--dry-run", is_flag=True, help="Show files and sizes without uploading.")
@click.option("--force", is_flag=True, help="Re-upload even if already uploaded.")
@click.option("--jobs", "-j", type=click.IntRange(min=1), default=4,
              help="Parallel file transfers per recording (default: 4).")
def upload(names, all_recordings, dry_run, force, jobs):
    """Upload recordings to cloud storage."""
    from screencap.upload import resolve_recording_dirs, upload_recording, _fmt_size

    try:
        dirs = resolve_recording_dirs(names, all_recordings=all_recordings)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    total_count = len(dirs)
    all_uploaded = 0
    all_skipped = 0
    all_failed = 0
    all_bytes = 0

    # Warn if any recordings will need export
    if not dry_run:
        needs_export = any(
            not (d / "events.jsonl").exists() or force for d in dirs
        )
        if needs_export:
            console.print(
                "[yellow]Warning:[/yellow] Auto-export includes all captured keystrokes. "
                "Review recordings for sensitive data before sharing.",
                highlight=False,
            )

    for i, d in enumerate(dirs, 1):
        if total_count > 1:
            console.print(f"\n[bold][{i}/{total_count}][/bold] {d.name}")

        # Auto-export events.jsonl if missing (or --force)
        if not dry_run:
            jsonl_path = d / "events.jsonl"
            if not jsonl_path.exists() or force:
                with console.status("[dim]Exporting events...[/dim]"):
                    try:
                        from screencap.exporter import export_recording, build_export_metadata
                        meta = build_export_metadata(exclude_moves=False)
                        count = export_recording(d, str(jsonl_path), exclude_moves=False, metadata=meta)
                        if count >= 0:
                            console.print(f"  [dim]Exported {count} events to events.jsonl[/dim]")
                        else:
                            console.print(f"  [yellow]Warning:[/yellow] Export failed (legacy DB?), uploading without events.jsonl")
                    except Exception as e:
                        console.print(f"  [yellow]Warning:[/yellow] Export failed ({e}), uploading without events.jsonl")

        try:
            result = upload_recording(d, dry_run=dry_run, force=force, jobs=jobs)
            all_uploaded += len(result.uploaded)
            all_skipped += len(result.skipped)
            all_failed += len(result.failed)
            all_bytes += result.total_bytes

            if not dry_run and not result.failed:
                parts = []
                if result.uploaded:
                    parts.append(f"{len(result.uploaded)} new")
                if result.skipped:
                    parts.append(f"{len(result.skipped)} skipped")
                summary = ", ".join(parts) if parts else "0 files"
                if result.gcs_prefix:
                    console.print(
                        f"\n[green]Uploaded {d.name}[/green] -> {result.gcs_prefix} ({summary})"
                    )
                else:
                    console.print(f"\n[green]Uploaded {d.name}[/green] ({summary})")
        except FileNotFoundError as e:
            console.print(f"[red]Error:[/red] {e}")
            all_failed += 1
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    if total_count > 1 and not dry_run:
        console.print(
            f"\n[bold]Done.[/bold] {all_uploaded} uploaded, "
            f"{all_skipped} skipped, {all_failed} failed "
            f"({_fmt_size(all_bytes)} total)"
        )


@cli.command()
@click.option("--dest", default=None, help="Destination directory (default: ~/.screencap/downloads/).")
@click.option("--dry-run", is_flag=True, help="Show what would be downloaded without downloading.")
@click.option("--force", is_flag=True, help="Re-download all recordings, ignoring markers.")
@click.option("--jobs", "-j", type=click.IntRange(min=1), default=4,
              help="Parallel file transfers per recording (default: 4).")
def download(dest, dry_run, force, jobs):
    """Download recordings from cloud storage."""
    from screencap.download import (
        _fmt_size,
        _resolve_dest_dir,
        download_recording,
        list_remote_recordings,
    )

    try:
        dest_dir = _resolve_dest_dir(dest)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    try:
        remote = list_remote_recordings()
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not remote:
        console.print("No recordings available for download.")
        return

    console.print(
        f"Found [bold]{len(remote)}[/bold] recording(s) on server."
    )

    all_downloaded = 0
    all_skipped = 0
    all_failed = 0
    all_bytes = 0

    for i, rec in enumerate(remote, 1):
        if len(remote) > 1:
            console.print(
                f"\n[bold][{i}/{len(remote)}][/bold] {rec.name} "
                f"({_fmt_size(rec.total_size)}, {rec.file_count} files)"
            )
        try:
            result = download_recording(
                rec.name, dest_dir, dry_run=dry_run, force=force, jobs=jobs,
            )
            all_downloaded += len(result.downloaded)
            all_skipped += len(result.skipped)
            all_failed += len(result.failed)
            all_bytes += result.total_bytes

            if not dry_run and not result.failed and result.downloaded:
                console.print(
                    f"  [green]Downloaded {rec.name}[/green] "
                    f"({len(result.downloaded)} files, {_fmt_size(result.total_bytes)})"
                )
        except FileNotFoundError as e:
            console.print(f"  [red]Error:[/red] {e}")
            all_failed += 1
        except RuntimeError as e:
            console.print(f"  [red]Error:[/red] {e}")
            all_failed += 1

    if not dry_run:
        console.print(
            f"\n[bold]Done.[/bold] {all_downloaded} downloaded, "
            f"{all_skipped} skipped, {all_failed} failed "
            f"({_fmt_size(all_bytes)} total)"
        )


@cli.command()
@click.argument("name")
@click.option(
    "--model", "-m",
    type=click.Choice(["tiny", "base", "small", "medium", "large"], case_sensitive=False),
    default=None,
    help="Whisper model to use (skips prompts, forces local).",
)
def transcribe(name, model):
    """Transcribe audio from a recording using Whisper."""
    from screencap.config import get_recordings_dir

    recording_dir = get_recordings_dir() / name
    audio_path = recording_dir / "audio.flac"

    if not audio_path.exists():
        console.print(
            f"[red]Error:[/red] No audio found for recording '{name}'. "
            "Was it recorded with audio enabled?"
        )
        sys.exit(1)

    # Fix #8: reject empty/corrupt audio
    if audio_path.stat().st_size < 1024:
        console.print(
            "[red]Error:[/red] Audio file is empty or too small to transcribe."
        )
        sys.exit(1)

    transcript_path = recording_dir / "transcript.txt"
    transcript_json_path = recording_dir / "transcript.json"

    # Fix #7: warn before overwriting
    if transcript_path.exists() or transcript_json_path.exists():
        if not click.confirm("Transcript already exists. Overwrite?", default=False):
            console.print("[dim]Transcription cancelled.[/dim]")
            return

    # --model flag → skip all interactive prompts, go straight to local
    if model:
        backend, backend_param = _select_local_model(model)
    else:
        backend, backend_param = _resolve_backend_interactive()

    if backend == "api":
        _run_api_transcription(
            backend_param, audio_path, transcript_path, transcript_json_path
        )
    else:
        try:
            # Fix #3: resolve import FIRST, then call
            try:
                import faster_whisper  # noqa: F401
                from sc_engine.cli import _transcribe_faster_whisper
                local_fn = _transcribe_faster_whisper
            except ImportError:
                from sc_engine.cli import _transcribe_local
                local_fn = _transcribe_local

            # Fix #4: no console.status() — sc_engine prints its own progress
            console.print(f"[dim]Using Whisper ({backend_param} model)...[/dim]")
            local_fn(audio_path, transcript_path, transcript_json_path, backend_param)
        except Exception as e:
            console.print(f"[red]Transcription failed:[/red] {e}")
            sys.exit(1)

    # Fix #2: hard-check that output was actually created
    if not transcript_path.exists():
        console.print(
            "[red]Error:[/red] Transcription completed but no transcript file was created. "
            "Check the logs above for errors."
        )
        sys.exit(1)

    # Show results
    console.print(f"\n[green]Transcript saved:[/green] {transcript_path}")
    console.print(f"[green]Timestamps saved:[/green] {transcript_json_path}")
    text = transcript_path.read_text().strip()
    if text:
        preview = text[:500] + ("..." if len(text) > 500 else "")
        console.print(f"\n[bold]Preview:[/bold]\n{preview}")


if __name__ == "__main__":
    cli()
