"""ScreenCap CLI — Click-based entry point."""

from __future__ import annotations

import json
import sys

import click
from dotenv import load_dotenv

load_dotenv()  # auto-load .env if present
from rich.console import Console
from rich.table import Table

from screencap import __version__

console = Console()


def _stdin_is_tty() -> bool:
    """Check if stdin is a real TTY (not piped or redirected)."""
    return sys.stdin.isatty()


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


def _report_unclassified_apps(capture_dir) -> None:
    """Report apps seen during recording that are not in privacy config."""
    from screencap.catalog import get_seen_bundle_ids
    from screencap.config import get_privacy_config
    from screencap.privacy.context import BUNDLE_ID_MAP

    seen_bids = get_seen_bundle_ids([capture_dir])
    if not seen_bids:
        return

    try:
        privacy_config = get_privacy_config()
    except Exception:
        return

    known_bids = (
        set(BUNDLE_ID_MAP.keys())
        | set(privacy_config.exclude_apps)
        | set(privacy_config.allow_apps)
        | set(privacy_config.app_classes.keys())
    )

    unclassified = sorted(seen_bids - known_bids)
    if not unclassified:
        return

    console.print(
        f"\n[yellow]Note:[/yellow] {len(unclassified)} app(s) seen during recording "
        f"are not classified:"
    )
    for bid in unclassified[:10]:
        console.print(f"  {bid}")
    if len(unclassified) > 10:
        console.print(f"  ... and {len(unclassified) - 10} more")
    console.print("  Run [bold]screencap setup --scan[/bold] to classify them.")


def _download_nlp_models() -> None:
    """Download GLiNER + spaCy models. Thin wrapper for testability."""
    from screencap.setup_wizard import _download_nlp_models as _do_download
    _do_download()


def _maybe_download_nlp_models() -> None:
    """Prompt to download GLiNER + spaCy models if not already cached."""
    from screencap.privacy import are_nlp_models_cached

    if are_nlp_models_cached():
        return  # fully cached

    console.print(
        "\n[bold]Privacy models not yet downloaded.[/bold] "
        "These are needed for scrubbing and cloud upload."
    )
    if click.confirm("Download now?", default=True):
        _download_nlp_models()
    else:
        console.print(
            "[dim]Skipped. Models will download on first scrub or upload.[/dim]"
        )


def _maybe_prompt_privacy_setup(*, cloud_intent: bool = False) -> None:
    """Prompt for privacy setup on first run if [privacy] section is missing."""
    import sys as _sys  # use real sys, not the module-level reference

    if not _sys.stdin.isatty():
        return  # non-interactive: skip silently, use defaults

    from screencap.config import _CONFIG_PATH, _load_toml

    if not _CONFIG_PATH.exists():
        # No config file at all — still prompt
        pass
    else:
        cfg = _load_toml()
        privacy_section = cfg.get("privacy")
        if privacy_section is not None:
            # Has a [privacy] section — check if NLP models need downloading.
            # Skip for cloud-intent: the cloud gate handles model download.
            if not cloud_intent:
                _maybe_download_nlp_models()
            return

    console.print(
        "\n[bold]Privacy setup not configured.[/bold] "
        "Run the setup wizard to classify apps for privacy protection."
    )
    if click.confirm("Run setup now?", default=True):
        from screencap.setup_wizard import run_setup_wizard
        run_setup_wizard()
    else:
        # Write setup_skipped flag to prevent re-prompting
        import tomlkit
        from screencap.config import invalidate_config_cache
        from screencap.setup_wizard import _load_config_toml, _save_config_atomic

        doc = _load_config_toml(_CONFIG_PATH)
        if "privacy" not in doc:
            doc.add("privacy", tomlkit.table())
        doc["privacy"]["setup_skipped"] = True
        _save_config_atomic(_CONFIG_PATH, doc)
        invalidate_config_cache()
        console.print(
            "[dim]Skipped. Recordings will stay local with default privacy settings. "
            "Run 'screencap setup' anytime.[/dim]"
        )


@cli.command()
@click.option("--name", "-n", default=None, help="Recording name (skips auto-naming).")
@click.option("--description", "-d", default=None, help="Task description.")
@click.option("--no-audio", is_flag=True, default=False, help="Disable audio capture.")
@click.option("--no-video", is_flag=True, default=False, help="Disable video capture.")
@click.option("--no-images", is_flag=True, default=False, help="Disable screenshot capture.")
@click.option("--no-window-data", is_flag=True, default=False, help="Disable window/accessibility data.")
@click.option("--output", "-o", type=click.Path(), default=None, help="Custom output directory.")
@click.option("--no-wifi-metrics", is_flag=True, default=False, help="Disable WiFi metrics collection.")
@click.option("--no-app-versions", is_flag=True, default=False, help="Disable running app version capture.")
@click.option("--no-auto-name", is_flag=True, default=False, help="Skip LLM auto-naming after recording.")
@click.option("--local-only", is_flag=True, default=False, help="Restrict LLM naming to local providers (Ollama).")
@click.option("--force", is_flag=True, default=False, help="Auto-clean orphaned processes before starting.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show all info/debug output during recording.")
@click.option("--chunk-duration", type=float, default=None,
              help="Auto-cut recording at this interval (seconds). Default: 900 (15 min). Set 0 to disable chunking.")
@click.option("--no-live-upload", is_flag=True, default=False,
              help="Disable background upload of chunks during recording.")
@click.option("--cloud", "destination", flag_value="cloud", default=None,
              help="Record for cloud upload (forces public privacy mode).")
@click.option("--local", "destination", flag_value="local",
              help="Record for local use only (uses configured privacy mode).")
@click.option("--segmentation-mode", type=click.Choice(["llm", "idle"], case_sensitive=False),
              default=None, help="Task segmentation: 'llm' (server-side) or 'idle' (gap detection).")
def start(
    name, description, no_audio, no_video, no_images, no_window_data,
    output, no_wifi_metrics, no_app_versions,
    no_auto_name, local_only, force, verbose, chunk_duration, no_live_upload,
    destination, segmentation_mode,
):
    """Record a screen capture session. Ctrl+C to stop."""
    from datetime import datetime

    from screencap.config import get_auto_name, get_auto_name_local_only, get_segmentation_mode

    # Resolve segmentation mode: CLI flag > config.toml > default
    seg_mode = segmentation_mode or get_segmentation_mode()

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
    # First-run privacy setup detection
    _maybe_prompt_privacy_setup(cloud_intent=destination == "cloud")

    # --- Resolve recording destination (cloud/local) ---
    from screencap.config import get_upload_default

    intent_source = "flag"
    if destination is None:
        upload_default = get_upload_default()
        if not sys.stdin.isatty():
            # Non-interactive: always default to local regardless of config
            destination = "local"
            intent_source = "non_interactive_default"
        elif upload_default == "ask":
            console.print(
                "\n[bold]Recording destination:[/bold]"
            )
            console.print(
                "  Cloud recordings use public privacy mode -- email, chat, calendar,"
            )
            console.print(
                "  and banking apps are blocked or masked. Data may be used in public datasets."
            )
            console.print(
                "\n  Local recordings use your configured privacy mode and stay on this machine.\n"
            )
            destination = click.prompt(
                "Cloud or local?",
                type=click.Choice(["cloud", "local"], case_sensitive=False),
                default="local",
            )
            intent_source = "prompt"
        else:
            destination = upload_default
            intent_source = "config_default"
    # else: destination was set by --cloud or --local flag, intent_source stays "flag"

    is_cloud = destination == "cloud"
    force_mode = None
    if is_cloud:
        from screencap.privacy.policy import PrivacyMode
        force_mode = PrivacyMode.PUBLIC

    # --- Cloud NLP model gate ---
    if is_cloud:
        from screencap.privacy import are_nlp_models_cached
        if not are_nlp_models_cached():
            if not _stdin_is_tty():
                console.print(
                    "[red]Error:[/] NLP models required for cloud recording "
                    "are not installed. Run [bold]screencap setup --scan[/bold] first."
                )
                raise SystemExit(1)
            if click.confirm(
                "NLP models needed for cloud PII scrubbing are not installed. "
                "Download now?",
                default=True,
            ):
                try:
                    _download_nlp_models()
                except (OSError, RuntimeError, ImportError) as exc:
                    console.print(f"[yellow]Warning:[/] Download failed: {exc}")
            if not are_nlp_models_cached():
                if click.confirm(
                    "Record locally instead? You can 'screencap upload' later "
                    "once models are installed.",
                    default=True,
                ):
                    is_cloud = False
                    force_mode = None  # revert to configured mode
                else:
                    console.print("[red]Aborted.[/]")
                    raise SystemExit(1)

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
            capture_window_data=capture_window_data,
            verbose=verbose,
            chunk_duration=chunk_duration,
            live_upload=not no_live_upload,
            force_mode=force_mode,
            cloud_intent=is_cloud,
            intent_source=intent_source,
            segmentation_mode=seg_mode,
        )
    except DiskFullError as e:
        capture_dir, elapsed = e.capture_dir, e.elapsed
        disk_full = True
        console.print("[yellow]Skipping auto-naming/transcription: disk space is low.[/yellow]")
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    # --- Post-recording pipeline ---
    # Order matters: export must run before auto-name, which may rename the directory.
    # 1. Auto-export events.jsonl (unconditional)
    # 2. Auto-transcribe audio (if auto_name_enabled and audio exists)
    # 3. Auto-name via LLM (if auto_name_enabled; may rename capture_dir -> final_dir)
    # 4. Print summary
    final_name = name
    final_dir = capture_dir

    # Auto-export events.jsonl for downstream scrubbing
    try:
        _auto_export(capture_dir)
    except KeyboardInterrupt:
        console.print("[yellow]Export cancelled.[/yellow]")

    if auto_name_enabled and not disk_full:
        # Auto-transcribe if audio was captured
        # In chunked mode, per-chunk transcription is handled by ChunkProcessor
        has_chunk_transcripts = any(capture_dir.glob("transcript_*.txt"))
        audio_path = capture_dir / "audio.flac"
        if audio and not has_chunk_transcripts and audio_path.exists() and audio_path.stat().st_size >= 1024:
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

    # Post-recording new-app report
    try:
        _report_unclassified_apps(final_dir)
    except Exception:
        pass  # non-blocking

    # Hard-exit to avoid multiprocessing feeder-thread atexit hangs.
    # All recording data is flushed to disk by this point.
    # Kill the resource tracker first so it can't warn about leaked semaphores.
    import os as _os
    try:
        from multiprocessing.resource_tracker import _resource_tracker
        if _resource_tracker._pid is not None:
            _os.kill(_resource_tracker._pid, 9)
            try:
                _os.waitpid(_resource_tracker._pid, _os.WNOHANG)
            except ChildProcessError:
                pass
    except Exception:
        pass
    _os._exit(0)


def _auto_export(capture_dir: Path) -> None:
    """Auto-export events.jsonl for downstream scrubbing."""
    jsonl_path = capture_dir / "events.jsonl"
    try:
        from screencap.exporter import build_export_metadata, export_recording

        with console.status("[dim]Exporting events...[/dim]"):
            meta = build_export_metadata(exclude_moves=False)
            count = export_recording(
                capture_dir, str(jsonl_path), exclude_moves=False, metadata=meta,
            )
        console.print(f"  [dim]Exported {count} events to events.jsonl[/dim]")
        if count == 0:
            console.print("[yellow]Warning:[/yellow] Recording contains no events.")
    except Exception as e:
        console.print(
            f"[yellow]Warning:[/yellow] Could not auto-export events.jsonl ({e}). "
            f"Run 'screencap export {capture_dir.name}' manually."
        )


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
@click.option("--remote", is_flag=True, help="List remote processed sessions.")
@click.option("--tag", "filter_tag", default=None, help="Filter remote sessions by tag.")
@click.option("--category", "filter_category", default=None,
              type=click.Choice(["development", "communication", "research",
                                 "admin", "creative", "other"], case_sensitive=False),
              help="Filter remote sessions by category.")
def list_cmd(as_json, sort, remote, filter_tag, filter_category):
    """List all recordings."""
    if not remote and (filter_tag or filter_category):
        console.print("[yellow]--tag and --category require --remote[/yellow]")
        return

    if remote:
        from screencap.download import list_remote_sessions

        try:
            sessions = list_remote_sessions(tag=filter_tag, category=filter_category)
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

        if not sessions:
            console.print("[dim]No remote sessions found.[/dim]")
            return

        if as_json:
            import dataclasses
            click.echo(json.dumps(
                [dataclasses.asdict(s) for s in sessions], indent=2,
            ))
            return

        def _duration_human(secs: float) -> str:
            h, rem = divmod(int(secs), 3600)
            m, s = divmod(rem, 60)
            parts = []
            if h:
                parts.append(f"{h}h")
            if m:
                parts.append(f"{m}m")
            parts.append(f"{s}s")
            return " ".join(parts)

        table = Table(show_header=True, header_style="bold #60a5fa")
        table.add_column("#", justify="right")
        table.add_column("Name")
        table.add_column("Date")
        table.add_column("Duration")
        table.add_column("Focus")
        table.add_column("Tags")
        table.add_column("Tasks", justify="right")

        for i, s in enumerate(sessions, 1):
            date_display = s.processed_at[:10] if s.processed_at else "-"
            tags_display = ", ".join(s.tags[:5]) if s.tags else "-"
            if len(s.tags) > 5:
                tags_display += ", ..."
            table.add_row(
                str(i),
                s.name,
                date_display,
                _duration_human(s.total_duration_s),
                s.primary_focus,
                tags_display,
                str(s.total_tasks),
            )

        console.print(table)
        return

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
    table.add_column("Intent")
    table.add_column("Audio")
    table.add_column("Transcribed")
    table.add_column("Uploaded")

    for i, r in enumerate(recordings, 1):
        name_display = r.name
        if r.drops:
            name_display += " [yellow]\u26a0[/yellow]"
        intent_display = r.intent or "-"
        table.add_row(
            str(i),
            name_display,
            r.date,
            r.duration,
            r.size_mb,
            intent_display,
            "[green]\u2713[/green]" if r.has_audio else "[dim]\u2717[/dim]",
            "[green]\u2713[/green]" if r.transcribed else "[dim]\u2717[/dim]",
            "[green]\u2713[/green]" if r.uploaded else "[dim]\u2717[/dim]",
        )

    console.print(table)


@cli.command()
@click.argument("name")
@click.option("--regenerate", is_flag=True, help="Delete cached viewer.html and regenerate.")
@click.option("--max-events", type=int, default=500, help="Max events in viewer (0 for all).")
def view(name, regenerate, max_events):
    """Open recording viewer in browser."""
    from screencap.viewer import open_viewer

    try:
        open_viewer(name, regenerate=regenerate, max_events=max_events)
        console.print(f"[dim]Opening {name}/viewer.html ...[/dim]")
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        sys.exit(1)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
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

    # Read recording intent
    from screencap.catalog import read_intent
    intent_data = None
    intent_path = recording_dir / ".recording_intent"
    if intent_path.exists():
        try:
            intent_data = json.loads(intent_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

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
        click.echo(json.dumps({"recording": rec_meta, "metrics": metrics, "drops": drops, "intent": intent_data}, indent=2))
        return

    # Human-readable output
    from rich.panel import Panel

    console.print(Panel(f"[bold]{name}[/bold]", title="Recording"))

    if intent_data:
        console.print(f"  [#60a5fa]destination:[/#60a5fa] {intent_data.get('destination', '?')}")
        console.print(f"  [#60a5fa]privacy mode:[/#60a5fa] {intent_data.get('privacy_mode', '?')}")
        console.print(f"  [#60a5fa]intent source:[/#60a5fa] {intent_data.get('source', '?')}")

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
    from screencap.exporter import ExportError, build_export_metadata, export_recording

    meta = build_export_metadata(exclude_moves)
    try:
        return export_recording(recording_dir, output_path, exclude_moves, metadata=meta)
    except ExportError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        return -1


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
                if count == 0:
                    err_console.print(f"[yellow]Warning:[/yellow] Recording '{rec_dir.name}' contains no events.")
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
    if count == 0:
        err_console.print("[yellow]Warning:[/yellow] Recording contains no events.")
    if output_path:
        err_console.print(f"Exported {count} events to [bold]{output_path}[/bold]")


@cli.command()
@click.option("--force", is_flag=True, help="Skip SIGTERM and go straight to SIGKILL.")
def stop(force):
    """Stop recording processes."""
    import os as _os
    import signal as _signal
    import time as _time

    try:
        from screencap.pidfile import (
            _is_screencap_process,
            _pid_exists,
            delete_pidfile,
            find_orphaned_processes,
            read_pidfile,
            terminate_processes,
        )
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    # Try graceful shutdown via SIGTERM to parent process first
    if not force:
        data = read_pidfile()
        if data and data.get("parent_pid"):
            parent_pid = data["parent_pid"]
            if _pid_exists(parent_pid) and _is_screencap_process(parent_pid):
                console.print(f"Sending stop signal to recording (PID {parent_pid})...")
                try:
                    _os.kill(parent_pid, _signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                else:
                    # Wait for graceful shutdown (up to 30s)
                    for _ in range(60):
                        if not _pid_exists(parent_pid):
                            break
                        _time.sleep(0.5)
                    if not _pid_exists(parent_pid):
                        console.print("Recording stopped gracefully.")
                        delete_pidfile()
                        return
                    console.print("[yellow]Graceful stop timed out — falling back to force kill.[/yellow]")

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


def _recover_chunk_metadata(
    recording_dir: Path, console: "Console", *, force: bool = False,
) -> None:
    """Generate per-chunk manifests + events JSONL when chunks exist but metadata doesn't.

    This is a recovery path for when ChunkProcessor failed during recording
    but chunk video files were created. Uses recording.db to derive chunk
    time ranges and generate the metadata files the Cloud Run processor needs.
    """
    import sqlite3

    chunk_videos = sorted(recording_dir.glob("chunk_*.mp4"))
    if not chunk_videos:
        return  # not a chunked recording

    db_path = recording_dir / "recording.db"
    if not db_path.exists():
        return

    # Check which chunks are missing manifests and/or events
    missing_manifests = []
    missing_events = []
    for vf in chunk_videos:
        # Extract index from filename: chunk_0000.mp4 → 0
        idx_str = vf.stem.split("_")[1]
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if not (recording_dir / f"chunk_{idx:04d}_manifest.json").exists() or force:
            missing_manifests.append(idx)
        if not (recording_dir / f"events_{idx:04d}.jsonl").exists() or force:
            missing_events.append(idx)

    if not missing_manifests and not missing_events:
        return

    # Derive chunk time ranges from recording.db
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row

        # Get recording start time (video_start_time is when first frame was captured)
        rec = conn.execute("SELECT timestamp FROM recording LIMIT 1").fetchone()
        if not rec:
            conn.close()
            return
        rec_start = rec["timestamp"]

        # Get the first and last action event timestamps
        first_evt = conn.execute("SELECT MIN(timestamp) as ts FROM action_event").fetchone()
        last_evt = conn.execute("SELECT MAX(timestamp) as ts FROM action_event").fetchone()
        if not first_evt or first_evt["ts"] is None:
            conn.close()
            return

        first_ts = first_evt["ts"]
        last_ts = last_evt["ts"]
        n_chunks = len(chunk_videos)

        # Determine chunk duration from config
        from screencap.config import get_chunk_duration
        chunk_dur = get_chunk_duration()
        if chunk_dur <= 0:
            # Estimate from recording span and chunk count
            chunk_dur = (last_ts - first_ts) / max(n_chunks, 1)

        # Compute chunk boundaries: chunk N covers [start + N*dur, start + (N+1)*dur)
        # Use recording start (or first event) as the base
        base_ts = min(rec_start, first_ts)
        chunk_ranges = []
        for idx in range(n_chunks):
            c_start = base_ts + idx * chunk_dur
            c_end = base_ts + (idx + 1) * chunk_dur
            if idx == n_chunks - 1:
                c_end = max(c_end, last_ts + 1.0)  # last chunk extends to cover all events
            chunk_ranges.append((idx, c_start, c_end))

        conn.close()
    except Exception as e:
        console.print(f"  [yellow]Warning:[/yellow] Could not derive chunk ranges: {e}")
        return

    # Generate missing manifests
    if missing_manifests:
        with console.status("[dim]Generating chunk manifests...[/dim]"):
            from screencap.config import get_segmentation_mode
            from screencap.task_manifest import generate_manifest

            generated = 0
            seg_mode = get_segmentation_mode()
            for idx, c_start, c_end in chunk_ranges:
                if idx in missing_manifests:
                    try:
                        generate_manifest(recording_dir, idx, c_start, c_end, segmentation_mode=seg_mode)
                        generated += 1
                    except Exception as e:
                        console.print(f"  [yellow]Warning:[/yellow] Manifest generation failed for chunk {idx}: {e}")
            if generated:
                console.print(f"  [dim]Generated {generated} chunk manifest(s)[/dim]")

    # Generate missing per-chunk events
    if missing_events:
        with console.status("[dim]Exporting per-chunk events...[/dim]"):
            import json as _json

            exported = 0
            try:
                conn = sqlite3.connect(str(db_path))
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA query_only=ON")
                conn.row_factory = sqlite3.Row

                for idx, c_start, c_end in chunk_ranges:
                    if idx in missing_events:
                        try:
                            rows = conn.execute(
                                "SELECT * FROM action_event WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                                (c_start, c_end),
                            ).fetchall()
                            jsonl_path = recording_dir / f"events_{idx:04d}.jsonl"
                            with open(jsonl_path, "w") as f:
                                for row in rows:
                                    f.write(_json.dumps(dict(row)) + "\n")
                            exported += 1
                        except Exception as e:
                            console.print(f"  [yellow]Warning:[/yellow] Event export failed for chunk {idx}: {e}")
                conn.close()
            except Exception as e:
                console.print(f"  [yellow]Warning:[/yellow] Could not export chunk events: {e}")
            if exported:
                console.print(f"  [dim]Exported events for {exported} chunk(s)[/dim]")


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--all", "all_recordings", is_flag=True, help="Upload all recordings.")
@click.option("--dry-run", is_flag=True, help="Show files and sizes without uploading.")
@click.option("--force", is_flag=True, help="Re-upload even if already uploaded.")
@click.option("--jobs", "-j", type=click.IntRange(min=1), default=4,
              help="Parallel file transfers per recording (default: 4).")
@click.option("--no-delete", is_flag=True, default=False,
              help="Keep local recording files after upload instead of auto-deleting.")
def upload(names, all_recordings, dry_run, force, jobs, no_delete):
    """Upload recordings to cloud storage."""
    from screencap.upload import resolve_recording_dirs, upload_recording, _fmt_size

    try:
        dirs = resolve_recording_dirs(names, all_recordings=all_recordings)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # --- Intent warnings ---
    from screencap.catalog import read_intent

    for d in dirs:
        intent = read_intent(d)
        if intent == "local" and not dry_run:
            console.print(
                f"  [yellow]Note:[/yellow] {d.name} is local-intent — post-hoc "
                "scrubbing provides weaker guarantees than capture-time enforcement."
            )

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
        # Skip if per-chunk JSONL already exists (chunked mode)
        if not dry_run:
            has_chunk_events = any(d.glob("events_*.jsonl"))
            jsonl_path = d / "events.jsonl"
            if not has_chunk_events and (not jsonl_path.exists() or force):
                with console.status("[dim]Exporting events...[/dim]"):
                    try:
                        from screencap.exporter import export_recording, build_export_metadata
                        meta = build_export_metadata(exclude_moves=False)
                        count = export_recording(d, str(jsonl_path), exclude_moves=False, metadata=meta)
                        console.print(f"  [dim]Exported {count} events to events.jsonl[/dim]")
                        if count == 0:
                            console.print("[yellow]Warning:[/yellow] Recording contains no events.")
                    except Exception as e:
                        console.print(f"  [yellow]Warning:[/yellow] Export failed ({e}), uploading without events.jsonl")

            # Recovery: generate per-chunk manifests + events if chunks exist but metadata doesn't
            _recover_chunk_metadata(d, console, force=force)

            # Recovery: generate sentinel file if missing (crash/force-quit recovery)
            _sentinel_path = d / "recording_complete.json"
            if not _sentinel_path.exists() and any(d.glob("chunk_*_manifest.json")):
                try:
                    from screencap.chunk_processor import _build_sentinel_data
                    _rec_id_path = d / ".recording_id"
                    _rec_name = _rec_id_path.read_text().strip() if _rec_id_path.exists() else d.name
                    _chunk_count = len(list(d.glob("chunk_*_manifest.json")))
                    _sentinel_data = _build_sentinel_data(
                        recording_name=_rec_name,
                        stop_reason="manual_upload",
                        chunks_expected=_chunk_count,
                    )
                    import json as _json
                    _sentinel_path.write_text(_json.dumps(_sentinel_data, indent=2))
                except Exception:
                    pass

            # Always scrub before upload
            try:
                from screencap.scrubber import scrub_recording

                with console.status(f"[bold]Scrubbing {d.name} for upload...[/bold]"):
                    scrub_result = scrub_recording(d.name)
                entity_total = sum(scrub_result.entity_counts.values())
                console.print(
                    f"  Scrubbed copy at [dim]{scrub_result.output_dir.name}/[/dim] "
                    f"({entity_total} redaction(s) applied)."
                )
                d = scrub_result.output_dir
            except Exception as e:
                console.print(
                    f"[red]Error:[/red] Scrubbing failed: {e}\n"
                    "Upload skipped — cannot upload without scrubbing."
                )
                all_failed += 1
                continue

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
@click.argument("names", nargs=-1)
@click.option("--dest", default=None, help="Destination directory (default: ~/.screencap/downloads/).")
@click.option("--dry-run", is_flag=True, help="Show what would be downloaded without downloading.")
@click.option("--force", is_flag=True, help="Re-download all recordings, ignoring markers.")
@click.option("--jobs", "-j", type=click.IntRange(min=1), default=4,
              help="Parallel file transfers per recording (default: 4).")
@click.option("--sessions", is_flag=True, help="Download processed sessions instead of raw recordings.")
@click.option("--category", "filter_category", default=None,
              type=click.Choice(["development", "communication", "research",
                                 "admin", "creative", "other"], case_sensitive=False),
              help="Only download task folders matching this category (requires --sessions).")
def download(names, dest, dry_run, force, jobs, sessions, filter_category):
    """Download recordings from cloud storage.

    Optionally pass one or more recording NAMES to download only those.
    With no names, all remote recordings are listed and downloaded.
    """
    from screencap.download import (
        _fmt_size,
        _resolve_dest_dir,
        download_recording,
        list_remote_recordings,
    )

    if filter_category and not sessions:
        console.print("[red]Error:[/red] --category requires --sessions")
        sys.exit(1)

    source = "sessions" if sessions else "recordings"

    if sessions and not dest:
        from screencap.config import get_sessions_dir
        try:
            dest_dir = get_sessions_dir()
        except OSError as e:
            console.print(f"[red]Error:[/red] Cannot create sessions directory: {e}")
            sys.exit(1)
    else:
        try:
            dest_dir = _resolve_dest_dir(dest)
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    if names:
        from types import SimpleNamespace
        remote = [SimpleNamespace(name=n, total_size=0, file_count=0) for n in names]
    else:
        try:
            remote = list_remote_recordings(source=source)
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

        if not remote:
            label = "sessions" if sessions else "recordings"
            console.print(f"No {label} available for download.")
            return

    label = "session" if sessions else "recording"
    console.print(
        f"Found [bold]{len(remote)}[/bold] {label}(s) to download."
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
                source=source, category_filter=filter_category,
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


@cli.command()
@click.option("--scan", is_flag=True, help="Rescan and show only new (unconfigured) apps.")
@click.option("--show", is_flag=True, help="Display current classifications (read-only).")
@click.option("--reset", is_flag=True, help="Remove [privacy] section after confirmation.")
def setup(scan, show, reset):
    """Configure privacy settings with an interactive wizard."""
    from screencap.setup_wizard import (
        reset_privacy_config,
        run_setup_wizard,
        show_current_config,
    )

    if show:
        show_current_config()
        return

    if reset:
        reset_privacy_config()
        return

    run_setup_wizard(scan_only=scan)


@cli.command()
@click.argument("name")
@click.option(
    "--pii-engine",
    type=click.Choice(["presidio", "presidio-gliner"]),
    default=None,
    help="PII detection engine (default: auto-detect, prefers GLiNER).",
)
def scrub(name: str, pii_engine: str | None) -> None:
    """Create a privacy-scrubbed copy of a recording."""
    from screencap.scrubber import scrub_recording

    try:
        scrub_recording(name, pii_engine=pii_engine)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)
    except ImportError as e:
        console.print(
            "[red]Error: Privacy dependencies are missing.[/red]\n"
            "Reinstall or update screencap."
        )
        console.print(f"[dim]{e}[/dim]")
        raise SystemExit(1)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


@cli.command()
def settings():
    """Show current ScreenCap configuration."""
    from screencap.config import (
        get_audio_default,
        get_auto_delete_after_upload,
        get_auto_name,
        get_chunk_duration,
        get_recordings_dir,
        get_rest_threshold,
        get_upload_default,
    )

    chunk = get_chunk_duration()
    if chunk >= 3600:
        chunk_str = f"{chunk:.0f}s ({chunk / 3600:.1f} hour)"
    elif chunk >= 60:
        chunk_str = f"{chunk:.0f}s ({chunk / 60:.0f} min)"
    elif chunk > 0:
        chunk_str = f"{chunk:.0f}s"
    else:
        chunk_str = "disabled (legacy single-file)"
    rest = get_rest_threshold()

    console.print("\n[bold]ScreenCap Configuration[/bold]\n")
    console.print(f"  Chunk duration:           {chunk_str}")
    console.print(f"  Auto-delete after upload: {'enabled' if get_auto_delete_after_upload() else 'disabled'}")
    console.print(f"  Rest threshold:           {rest:.0f}s ({rest / 60:.0f} min)")
    console.print(f"  Recordings dir:           {get_recordings_dir()}")
    console.print(f"  Audio default:            {'enabled' if get_audio_default() else 'disabled'}")
    console.print(f"  Auto-name:                {'enabled' if get_auto_name() else 'disabled'}")
    console.print(f"  Upload default:           {get_upload_default()}")
    console.print()


# ---------------------------------------------------------------------------
# _smoke-test (hidden) — validate critical subsystems load in frozen binary
# ---------------------------------------------------------------------------


def _check_presidio_analyzer() -> tuple[str, bool, str]:
    """Instantiate AnalyzerEngine with no-op NLP (validates bundled YAML recognizer configs).

    Uses a stub NLP engine to avoid triggering spaCy model loading, which in
    a frozen binary calls sys.executable -m pip (i.e. screencap -m pip) and
    crashes with a Click UsageError.  spaCy model loading is tested separately
    by _check_spacy_model.
    """
    import traceback as _tb
    name = "presidio_analyzer"
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngine, NlpArtifacts

        class _NoOpNlpEngine(NlpEngine):
            def load(self): pass
            def is_loaded(self): return True
            def process_text(self, text, language):
                return NlpArtifacts(
                    entities=[], tokens=[], lemmas=[],
                    tokens_indices=[], dependencies=[],
                    keywords=[], language=language,
                )
            def process_batch(self, texts, language, **kwargs):
                for text in texts:
                    yield text, self.process_text(text, language)
            def is_stopword(self, word, language): return False
            def is_punct(self, word, language): return False
            def get_supported_entities(self): return []
            def get_supported_languages(self): return ["en"]

        analyzer = AnalyzerEngine(nlp_engine=_NoOpNlpEngine())
        recognizers = analyzer.registry.get_recognizers(
            language="en", all_fields=True,
        )
        if not recognizers:
            return name, False, "No recognizers loaded — YAML config missing?"
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_fast_gliner() -> tuple[str, bool, str]:
    """Import fast_gliner (verifies Rust extension + static ONNX)."""
    import traceback as _tb
    name = "fast_gliner"
    try:
        from fast_gliner import FastGLiNER  # noqa: F401
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_detect_secrets_plugins() -> tuple[str, bool, str]:
    """Instantiate DetectSecretsDetector (validates all plugin submodules load)."""
    import traceback as _tb
    name = "detect_secrets_plugins"
    try:
        from screencap.privacy.secrets import DetectSecretsDetector
        DetectSecretsDetector()
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_spacy_model() -> tuple[str, bool, str]:
    """Load en_core_web_sm spaCy model (verifies bundled model data).

    In frozen binaries, spacy.load() uses importlib.util.find_spec() which
    can't locate bundled packages.  Importing the package first puts it in
    sys.modules, where spacy.load() checks before find_spec().
    """
    import traceback as _tb
    name = "spacy_model"
    try:
        import en_core_web_sm  # noqa: F401 — ensures sys.modules entry for frozen binary
        import spacy
        spacy.load("en_core_web_sm")
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_av_codecs() -> tuple[str, bool, str]:
    """Import av and verify libx264 codec (verifies ffmpeg dylibs)."""
    import traceback as _tb
    name = "av_codecs"
    try:
        import av
        av.codec.Codec("libx264", "w")
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_pynput() -> tuple[str, bool, str]:
    """Import pynput keyboard and mouse listeners (import only)."""
    import traceback as _tb
    name = "pynput"
    try:
        from pynput.keyboard import Listener as KL  # noqa: F401
        from pynput.mouse import Listener as ML  # noqa: F401
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_sounddevice() -> tuple[str, bool, str]:
    """Import sounddevice (import only — device enumeration needs permission)."""
    import traceback as _tb
    name = "sounddevice"
    try:
        import sounddevice  # noqa: F401
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_domain_index() -> tuple[str, bool, str]:
    """Instantiate DefaultContextClassifier (exercises build_domain_index → load_ut1_domains)."""
    import traceback as _tb
    name = "domain_index"
    try:
        from screencap.privacy.context import DefaultContextClassifier
        DefaultContextClassifier()
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_onnxruntime_excluded() -> tuple[str, bool, str]:
    """Verify onnxruntime is NOT importable (confirms exclusion in spec)."""
    name = "onnxruntime_excluded"
    try:
        import onnxruntime  # noqa: F401
        return name, False, "onnxruntime should not be importable but was"
    except ImportError:
        return name, True, ""
    except Exception:
        import traceback as _tb
        return name, False, _tb.format_exc()


_SMOKE_CHECKS = [
    _check_presidio_analyzer,
    _check_fast_gliner,
    _check_detect_secrets_plugins,
    _check_spacy_model,
    _check_av_codecs,
    _check_pynput,
    _check_sounddevice,
    _check_domain_index,
    _check_onnxruntime_excluded,
]


@cli.command("_smoke-test", hidden=True)
@click.option("--verbose", "-v", is_flag=True, help="Show full tracebacks.")
def smoke_test(verbose):
    """Validate critical subsystems load correctly (internal)."""
    import traceback

    frozen = getattr(sys, "frozen", False)
    mode = "frozen binary" if frozen else "dev install"
    console.print(f"\nscreencap smoke test ({mode})\n")

    results: list[tuple[str, bool, str]] = []
    for check_fn in _SMOKE_CHECKS:
        try:
            result = check_fn()
        except BaseException:
            result = (check_fn.__name__.replace("_check_", ""), False, traceback.format_exc())
        results.append(result)

        name, passed, err = result
        if passed:
            console.print(f"  [green]\\[PASS][/green] {name}")
        else:
            # err contains the full traceback; show last line for summary,
            # full traceback when --verbose
            err_summary = err.strip().rsplit("\n", 1)[-1]
            console.print(f"  [red]\\[FAIL][/red] {name} — {err_summary}")
            if verbose:
                console.print(err)

    passed_count = sum(1 for _, p, _ in results if p)
    total = len(results)
    failed_count = total - passed_count

    console.print()
    if failed_count:
        console.print(f"{passed_count}/{total} checks passed, {failed_count} failed")
        sys.exit(1)
    else:
        console.print(f"All {total} checks passed")


if __name__ == "__main__":
    cli()
