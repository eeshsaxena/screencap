"""Export recording events as JSONL."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from screencap import __version__


def build_export_metadata(exclude_moves: bool) -> dict:
    """Build metadata dict for the JSONL header line."""
    return {
        "_meta": True,
        "screencap_version": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exclude_moves": exclude_moves,
    }


def export_recording(
    recording_dir,
    output_path: str | None,
    exclude_moves: bool,
    metadata: dict | None = None,
) -> int:
    """Export a single recording to JSONL.

    Returns event count on success, or -1 if the recording uses the
    legacy capture.db format (unsupported).

    When *output_path* is a file path, uses atomic write (write to .tmp,
    rename on success).  When *output_path* is None, writes to stdout.
    """
    from openadapt_capture import Capture

    try:
        with Capture.load(str(recording_dir)) as capture:
            if output_path is None:
                # Write to stdout — no atomic write needed
                return _write_events(capture, sys.stdout, exclude_moves, metadata)

            # Atomic write: .tmp → rename
            tmp_path = output_path + ".tmp"
            try:
                with open(tmp_path, "w") as f:
                    count = _write_events(capture, f, exclude_moves, metadata)
                os.rename(tmp_path, output_path)
                return count
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
    except FileNotFoundError:
        return -1


def _write_events(capture, out_file, exclude_moves: bool, metadata: dict | None) -> int:
    """Stream events to an open file handle. Returns event count."""
    import click

    if metadata is not None:
        click.echo(json.dumps(metadata), file=out_file)

    count = 0
    for action in capture.actions(include_moves=not exclude_moves):
        click.echo(action.event.model_dump_json(), file=out_file)
        count += 1
    return count
