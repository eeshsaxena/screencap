"""Open recording viewer.html in macOS default browser."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from screencap.catalog import find_db
from screencap.config import get_recordings_dir

console = Console()


def open_viewer(
    name: str,
    recordings_dir: Path | None = None,
) -> None:
    """Open viewer.html for a recording in the default browser."""
    if recordings_dir is None:
        recordings_dir = get_recordings_dir()

    dir_name = name
    rec_dir = recordings_dir / dir_name
    viewer = rec_dir / "viewer.html"

    if not rec_dir.exists():
        raise FileNotFoundError(
            f"Recording '{dir_name}' not found in {recordings_dir}"
        )

    # Auto-generate viewer.html if missing but a recording DB exists
    if not viewer.exists():
        db = find_db(rec_dir)
        if db is None:
            raise FileNotFoundError(f"No recording database found in {rec_dir}")

        # Only recording.db is supported by create_html (CaptureSession).
        # Legacy capture.db has a different schema and can't be auto-generated.
        if db.name == "capture.db":
            raise FileNotFoundError(
                f"viewer.html missing and cannot auto-generate for legacy capture.db format.\n"
                f"Try: capture visualize {rec_dir} --html"
            )

        console.print("[dim]viewer.html not found, generating...[/dim]")
        try:
            from openadapt_capture import create_html

            create_html(str(rec_dir), output=str(viewer))
        except Exception as e:
            raise FileNotFoundError(
                f"Could not generate viewer.html: {e}"
            ) from e

    subprocess.run(["open", str(viewer)], check=True)
