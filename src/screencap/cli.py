"""ScreenCap CLI — Click-based entry point."""

from __future__ import annotations

import json
import sys

import click
from rich.console import Console
from rich.table import Table

from screencap import __version__

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="screencap")
def cli():
    """ScreenCap — macOS screen capture with privacy scrubbing."""


@cli.command()
@click.option("--name", "-n", default=None, help="Recording name.")
@click.option("--description", "-d", default=None, help="Task description.")
@click.option("--no-audio", is_flag=True, default=False, help="Disable audio capture.")
@click.option("--output", "-o", type=click.Path(), default=None, help="Custom output directory.")
@click.option("--no-wifi-metrics", is_flag=True, default=False, help="Disable WiFi metrics collection.")
@click.option("--force", is_flag=True, default=False, help="Auto-clean orphaned processes before starting.")
def start(name, description, no_audio, output, no_wifi_metrics, force):
    """Record a screen capture session. Ctrl+C to stop."""
    if not name:
        name = click.prompt("Recording name")
        description = click.prompt("Description (optional)", default="", show_default=False)
        audio_input = click.prompt(
            "Record audio?",
            type=click.Choice(["y", "n"], case_sensitive=False),
            default="y",
        )
        no_audio = audio_input.lower() == "n"

    audio = not no_audio
    wifi_metrics = not no_wifi_metrics

    from screencap.recorder import start_recording

    start_recording(
        name, description or None, audio, output,
        wifi_metrics=wifi_metrics, force_clean=force,
    )


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

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Date")
    table.add_column("Duration")
    table.add_column("Size")
    table.add_column("Audio")
    table.add_column("Scrubbed")

    for r in recordings:
        table.add_row(
            r.name,
            r.date,
            r.duration,
            r.size_mb,
            "[green]\u2713[/green]" if r.has_audio else "[dim]\u2717[/dim]",
            "[green]\u2713[/green]" if r.has_scrubbed else "[dim]\u2717[/dim]",
        )

    console.print(table)


@cli.command()
@click.argument("name")
@click.option("--scrubbed", is_flag=True, help="Open the scrubbed version.")
def view(name, scrubbed):
    """Open recording viewer in browser."""
    from screencap.viewer import open_viewer

    try:
        open_viewer(name, scrubbed=scrubbed)
        suffix = "-scrubbed" if scrubbed else ""
        console.print(f"[dim]Opening {name}{suffix}/viewer.html ...[/dim]")
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def info(name, as_json):
    """Show details and system metrics for a recording."""
    from screencap.catalog import find_db, _read_recording_meta
    from screencap.config import get_recordings_dir
    from screencap.metrics import METRICS_FILENAME

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

    # Read metrics
    metrics_path = recording_dir / METRICS_FILENAME
    metrics = None
    if metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if as_json:
        click.echo(json.dumps({"recording": rec_meta, "metrics": metrics}, indent=2))
        return

    # Human-readable output
    from rich.panel import Panel

    console.print(Panel(f"[bold]{name}[/bold]", title="Recording"))

    if rec_meta:
        for key, val in rec_meta.items():
            console.print(f"  [cyan]{key}:[/cyan] {val}")
    else:
        console.print("  [dim]No recording metadata available.[/dim]")

    if metrics is None:
        console.print("\n[dim]No system metrics available (recorded before metrics feature).[/dim]")
        return

    static = metrics.get("static", {})
    if static:
        console.print(Panel("[bold]System Info[/bold]"))
        for key, val in static.items():
            if key == "displays":
                for i, d in enumerate(val):
                    console.print(f"  [cyan]display {i}:[/cyan] {d.get('width')}x{d.get('height')}")
            elif key == "locale" and isinstance(val, dict):
                console.print(f"  [cyan]locale:[/cyan]")
                for lk, lv in val.items():
                    if isinstance(lv, list):
                        console.print(f"    [cyan]{lk}:[/cyan] {', '.join(str(x) for x in lv)}")
                    elif isinstance(lv, dict):
                        console.print(f"    [cyan]{lk}:[/cyan] {lv}")
                    else:
                        console.print(f"    [cyan]{lk}:[/cyan] {lv}")
            elif key == "wifi" and isinstance(val, dict):
                console.print(f"  [cyan]wifi:[/cyan]")
                for wk, wv in val.items():
                    console.print(f"    [cyan]{wk}:[/cyan] {wv}")
            else:
                console.print(f"  [cyan]{key}:[/cyan] {val}")

    for phase in ("start", "end"):
        snapshot = metrics.get(phase)
        if snapshot:
            console.print(Panel(f"[bold]{phase.title()} Snapshot[/bold]"))
            for key, val in snapshot.items():
                if key == "wifi" and isinstance(val, dict):
                    console.print(f"  [cyan]wifi:[/cyan]")
                    for wk, wv in val.items():
                        console.print(f"    [cyan]{wk}:[/cyan] {wv}")
                else:
                    console.print(f"  [cyan]{key}:[/cyan] {val}")
        elif phase == "end":
            console.print(f"\n  [dim]No end snapshot (recording may have been interrupted).[/dim]")


def _check_scrub_deps() -> bool:
    """Check if scrubbing dependencies are installed. Prompt to install if missing."""
    try:
        import spacy  # noqa: F401
        import presidio_analyzer  # noqa: F401
        import presidio_anonymizer  # noqa: F401
    except ImportError:
        console.print(
            "[yellow]Scrubbing requires heavy dependencies (spaCy, Presidio, transformers) "
            "which are not currently installed (~500 MB download).[/yellow]"
        )
        if not click.confirm("Install them now?", default=True):
            console.print("[dim]Scrub cancelled.[/dim]")
            return False

        import subprocess

        console.print("[dim]Installing screencap[privacy] ...[/dim]")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "screencap[privacy]"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]Installation failed:[/red]\n{result.stderr.strip()}")
            return False

        console.print("[green]Dependencies installed.[/green]")
        import spacy  # noqa: F811

    # Ensure spaCy model is downloaded
    import spacy

    from openadapt_privacy.config import config as privacy_config

    model_name = privacy_config.SPACY_MODEL_NAME
    if not spacy.util.is_package(model_name):
        with console.status(f"[bold]Downloading spaCy model ({model_name}) ...[/bold]"):
            spacy.cli.download(model_name)
        console.print(f"[green]Model {model_name} installed.[/green]")

    return True


@cli.command()
@click.argument("name")
@click.option("--provider", default="PRESIDIO", help="Scrubbing provider.")
def scrub(name, provider):
    """Create a privacy-scrubbed copy of a recording."""
    if not _check_scrub_deps():
        return

    from screencap.scrubber import scrub_recording

    scrub_recording(name, provider=provider)


@cli.command()
@click.option("--force", is_flag=True, help="Skip SIGTERM and go straight to SIGKILL.")
def stop(force):
    """Stop orphaned recording processes."""
    from screencap.pidfile import (
        delete_pidfile,
        find_orphaned_processes,
        terminate_processes,
    )

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


if __name__ == "__main__":
    cli()
