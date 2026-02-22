"""Create privacy-scrubbed copies of recordings."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from screencap.config import get_recordings_dir

console = Console()


def scrub_recording(
    name: str,
    recordings_dir: Path | None = None,
    provider: str = "PRESIDIO",
) -> Path:
    """Copy a recording and scrub PII from the copy. Never mutates originals."""
    from openadapt_privacy.providers import ScrubProvider

    if recordings_dir is None:
        recordings_dir = get_recordings_dir()

    src = recordings_dir / name
    dst = recordings_dir / f"{name}-scrubbed"

    if not src.exists():
        console.print(f"[red]Error:[/red] Recording not found: {src}")
        raise SystemExit(1)

    if dst.exists():
        console.print(f"[yellow]Removing existing scrubbed copy:[/yellow] {dst}")
        shutil.rmtree(dst)

    with console.status(f"Copying {name} → {name}-scrubbed ..."):
        shutil.copytree(src, dst)
    console.print(f"Copied {name} → {name}-scrubbed")

    with console.status("Loading NLP model..."):
        scrubber = ScrubProvider.get_scrubber(provider)
    console.print("NLP model loaded")
    entity_counts: dict[str, int] = {}

    # --- Scrub screenshots ---
    screenshots_dir = dst / "screenshots"
    if screenshots_dir.exists():
        pngs = sorted(screenshots_dir.glob("*.png"))
        if pngs:
            with Progress(console=console) as progress:
                task = progress.add_task("Scrubbing screenshots...", total=len(pngs))
                for png in pngs:
                    try:
                        from PIL import Image

                        img = Image.open(png)
                        scrubbed = scrubber.scrub_image(img)
                        scrubbed.save(png)
                    except Exception as e:
                        console.print(f"[yellow]Warning:[/yellow] Failed to scrub {png.name}: {e}")
                    progress.advance(task)

    # --- Scrub text in capture DB ---
    from screencap.catalog import find_db

    db_path = find_db(dst)
    if db_path is not None:
        _scrub_db(db_path, scrubber, entity_counts)

    # --- Scrub transcript ---
    transcript = dst / "transcript.json"
    if transcript.exists():
        _scrub_transcript(transcript, scrubber)

    # --- Scrub system metrics ---
    metrics_file = dst / "system_metrics.json"
    if metrics_file.exists():
        _scrub_metrics(metrics_file)

    # --- Regenerate viewer.html ---
    try:
        from openadapt_capture import create_html

        create_html(str(dst), output=str(dst / "viewer.html"))
        console.print("[dim]Regenerated viewer.html[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not regenerate viewer.html: {e}")

    # NOTE: Video scrubbing (mp4) is NOT supported yet.
    video = dst / "video.mp4"
    if video.exists():
        console.print("[dim]Note: video.mp4 is not scrubbed (not yet supported)[/dim]")

    console.print(f"\n[bold green]Scrubbed copy at {dst}/[/bold green]")
    if entity_counts:
        parts = [f"{count} {etype}" for etype, count in sorted(entity_counts.items())]
        console.print(f"   Entities found: {', '.join(parts)}")

    return dst


def _scrub_db(db_path: Path, scrubber, entity_counts: dict[str, int]) -> None:
    """Scrub text fields in the recording DB. Supports both schemas."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # Detect schema by checking which tables exist
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}

    if "capture" in tables:
        _scrub_capture_schema(cur, tables, scrubber)
    elif "recording" in tables:
        _scrub_recording_schema(cur, tables, scrubber)

    conn.commit()
    conn.close()


def _scrub_capture_schema(cur, tables: set, scrubber) -> None:
    """Scrub capture.db schema (capture + events tables)."""
    # Scrub task_description
    cur.execute("SELECT id, task_description FROM capture WHERE task_description IS NOT NULL")
    for row_id, text in cur.fetchall():
        if text:
            scrubbed = scrubber.scrub_text(text)
            if scrubbed != text:
                cur.execute("UPDATE capture SET task_description = ? WHERE id = ?", (scrubbed, row_id))

    # Scrub event data JSON blobs
    if "events" in tables:
        cur.execute("SELECT id, data FROM events WHERE data IS NOT NULL")
        rows = cur.fetchall()
        if rows:
            with Progress(console=console) as progress:
                task = progress.add_task("Scrubbing events...", total=len(rows))
                for row_id, data_json in rows:
                    if data_json:
                        try:
                            data = json.loads(data_json) if isinstance(data_json, str) else data_json
                            if isinstance(data, dict):
                                scrubbed_data = _scrub_dict_recursive(data, scrubber)
                                cur.execute(
                                    "UPDATE events SET data = ? WHERE id = ?",
                                    (json.dumps(scrubbed_data), row_id),
                                )
                        except (json.JSONDecodeError, TypeError):
                            pass
                    progress.advance(task)


def _scrub_recording_schema(cur, tables: set, scrubber) -> None:
    """Scrub recording.db schema (recording + action_event + window_event)."""
    # Scrub task_description
    cur.execute("SELECT id, task_description FROM recording WHERE task_description IS NOT NULL")
    for row_id, text in cur.fetchall():
        if text:
            scrubbed = scrubber.scrub_text(text)
            if scrubbed != text:
                cur.execute("UPDATE recording SET task_description = ? WHERE id = ?", (scrubbed, row_id))

    if "action_event" in tables:
        # Scrub text columns
        for col in ("key_char", "canonical_key_char", "key_name", "canonical_key_name"):
            try:
                cur.execute(f"SELECT id, {col} FROM action_event WHERE {col} IS NOT NULL")
                for row_id, text in cur.fetchall():
                    if text and text.strip():
                        scrubbed = scrubber.scrub_text(text)
                        if scrubbed != text:
                            cur.execute(
                                f"UPDATE action_event SET {col} = ? WHERE id = ?",
                                (scrubbed, row_id),
                            )
            except sqlite3.OperationalError:
                pass

        # Scrub element_state JSON
        try:
            cur.execute("SELECT id, element_state FROM action_event WHERE element_state IS NOT NULL")
            rows = cur.fetchall()
            if rows:
                with Progress(console=console) as progress:
                    task = progress.add_task("Scrubbing action events...", total=len(rows))
                    for row_id, state_json in rows:
                        if state_json:
                            try:
                                state = json.loads(state_json) if isinstance(state_json, str) else state_json
                                if isinstance(state, dict):
                                    scrubbed_state = _scrub_dict_recursive(state, scrubber)
                                    cur.execute(
                                        "UPDATE action_event SET element_state = ? WHERE id = ?",
                                        (json.dumps(scrubbed_state), row_id),
                                    )
                            except (json.JSONDecodeError, TypeError):
                                pass
                        progress.advance(task)
        except sqlite3.OperationalError:
            pass

    # Scrub window titles
    if "window_event" in tables:
        try:
            cur.execute("SELECT id, title FROM window_event WHERE title IS NOT NULL")
            for row_id, text in cur.fetchall():
                if text:
                    scrubbed = scrubber.scrub_text(text)
                    if scrubbed != text:
                        cur.execute("UPDATE window_event SET title = ? WHERE id = ?", (scrubbed, row_id))
        except sqlite3.OperationalError:
            pass


def _scrub_dict_recursive(d: dict, scrubber) -> dict:
    """Recursively scrub string values in a dict."""
    result = {}
    for k, v in d.items():
        if isinstance(v, str) and v.strip():
            result[k] = scrubber.scrub_text(v)
        elif isinstance(v, dict):
            result[k] = _scrub_dict_recursive(v, scrubber)
        elif isinstance(v, list):
            result[k] = [
                _scrub_dict_recursive(item, scrubber) if isinstance(item, dict) else
                scrubber.scrub_text(item) if isinstance(item, str) and item.strip() else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def _scrub_metrics(metrics_path: Path) -> None:
    """Redact PII fields in system_metrics.json.

    Note: locale data (system_locale, preferred_languages, timezone, etc.)
    and running_applications (app names, bundle IDs, versions) are not
    considered PII and are intentionally not redacted. Only hostname is PII
    because it often contains the user's name.
    """
    try:
        data = json.loads(metrics_path.read_text())
        static = data.get("static", {})
        if "hostname" in static:
            static["hostname"] = "<REDACTED>"
        metrics_path.write_text(json.dumps(data, indent=2))
        console.print("[dim]Scrubbed system_metrics.json[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Failed to scrub system metrics: {e}")


def _scrub_transcript(transcript_path: Path, scrubber) -> None:
    """Scrub text in transcript.json."""
    try:
        data = json.loads(transcript_path.read_text())
        if isinstance(data, dict):
            if "text" in data and isinstance(data["text"], str):
                data["text"] = scrubber.scrub_text(data["text"])
            if "segments" in data and isinstance(data["segments"], list):
                for seg in data["segments"]:
                    if isinstance(seg, dict) and "text" in seg:
                        seg["text"] = scrubber.scrub_text(seg["text"])
            if "words" in data and isinstance(data["words"], list):
                for word in data["words"]:
                    if isinstance(word, dict) and "word" in word:
                        word["word"] = scrubber.scrub_text(word["word"])
        transcript_path.write_text(json.dumps(data, indent=2))
        console.print("[dim]Scrubbed transcript.json[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Failed to scrub transcript: {e}")
