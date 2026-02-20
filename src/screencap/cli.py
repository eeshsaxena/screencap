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
def start(name, description, no_audio, output):
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

    from screencap.recorder import start_recording

    start_recording(name, description or None, audio, output)


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


if __name__ == "__main__":
    cli()
