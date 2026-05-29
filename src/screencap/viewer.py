"""Open recording viewer.html in macOS default browser."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from screencap.catalog import find_db
from screencap.config import resolve_recording_dir

console = Console()

# Viewer files above this size are assumed to be pre-fix cached files
# that should be auto-regenerated with caps.
_MAX_VIEWER_SIZE_BYTES = 200_000_000  # 200 MB


def _ensure_single_video(rec_dir: Path) -> None:
    """If only chunked videos exist, concatenate them into ``rec_dir/video.mp4``.

    Uses the in-process PyAV concat (stream copy, no re-encode) so the merge
    works with nothing installed on PATH — a Finder/Launchpad-launched ``.app``
    gets the minimal GUI PATH and cannot reach a ``brew``-installed ffmpeg.
    The merged file lives at ``rec_dir/video.mp4`` because many consumers depend
    on that location (``screencap upload`` ships it, the HTML viewer renders it,
    ``catalog`` uses it for stub detection, ``capture``/``recorder`` read it).

    Idempotent: early-returns if ``video.mp4`` already exists. A single chunk is
    symlinked rather than re-muxed.
    """
    chunks = sorted(rec_dir.glob("chunk_*.mp4"))
    if not chunks:
        return  # nothing to concat
    if (rec_dir / "video.mp4").exists():
        return  # already has single video

    # Only concat if we have 2+ chunks
    if len(chunks) == 1:
        # Symlink single chunk for compatibility
        try:
            (rec_dir / "video.mp4").symlink_to(chunks[0])
        except OSError:
            pass
        return

    console.print(f"[dim]Concatenating {len(chunks)} video chunks for viewer...[/dim]")
    try:
        from screencap.engine.video import concat_video_chunks

        concat_video_chunks(rec_dir)
        console.print(f"[dim]Created merged video ({len(chunks)} chunks)[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Video merge failed: {e}")


def _needs_regeneration(viewer: Path, regenerate: bool) -> bool:
    """Check if viewer.html needs to be (re)generated."""
    if not viewer.exists():
        return True
    if regenerate:
        return True
    # Auto-regenerate oversized cached files from before the fix
    if viewer.stat().st_size > _MAX_VIEWER_SIZE_BYTES:
        return True
    return False


def open_viewer(
    name: str,
    regenerate: bool = False,
    max_events: int | None = 500,
) -> None:
    """Open viewer.html for a recording in the default browser."""
    rec_dir = resolve_recording_dir(name)
    viewer = rec_dir / "viewer.html"

    if not rec_dir.exists():
        raise FileNotFoundError(
            f"Recording '{name}' not found"
        )

    # For chunked recordings, ensure a single video file exists for the viewer
    _ensure_single_video(rec_dir)

    # Generate viewer.html if missing, explicitly requested, or oversized
    if _needs_regeneration(viewer, regenerate):
        db = find_db(rec_dir)
        if db is None:
            raise FileNotFoundError(f"No recording database found in {rec_dir}")

        if viewer.exists():
            size_mb = viewer.stat().st_size / 1_000_000
            console.print(f"[dim]Regenerating viewer.html ({size_mb:.0f} MB)...[/dim]")
            viewer.unlink()
        else:
            console.print("[dim]viewer.html not found, generating...[/dim]")
        try:
            from screencap.engine import create_html
            from screencap.engine.visualize.html import (
                DEFAULT_VIEWER_FRAME_QUALITY,
                DEFAULT_VIEWER_FRAME_SCALE,
            )

            create_html(
                str(rec_dir),
                output=str(viewer),
                max_events=max_events,
                frame_scale=DEFAULT_VIEWER_FRAME_SCALE,
                frame_quality=DEFAULT_VIEWER_FRAME_QUALITY,
            )
        except Exception as e:
            raise RuntimeError(
                f"Could not generate viewer.html: {e}"
            ) from e

    subprocess.run(["open", str(viewer)], check=True)
