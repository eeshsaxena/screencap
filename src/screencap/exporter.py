"""Export recording events as JSONL."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from screencap import __version__

# Backward-compat re-exports: `build_privacy_filter` moved to
# `screencap.privacy.filter` so cloud callers (chunk processor, recovery)
# can import from a neutral location. Existing tests and downstream
# imports of `screencap.exporter.build_privacy_filter` continue to work.
from screencap.privacy.filter import (  # noqa: F401
    build_cloud_window_filter,
    build_privacy_filter,
)

logger = logging.getLogger(__name__)


class ExportError(Exception):
    """Export failed — wraps the underlying cause."""


def build_export_metadata(exclude_moves: bool) -> dict:
    """Build metadata dict for the JSONL header line."""
    return {
        "_meta": True,
        "format_version": 2,
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

    Returns event count on success.  Raises ``ExportError`` if the
    recording database cannot be found (missing directory or missing
    recording.db).

    When *output_path* is a file path, uses atomic write (write to .tmp,
    rename on success).  When *output_path* is None, writes to stdout.
    """
    from screencap.engine import Capture

    try:
        capture_ctx = Capture.load(str(recording_dir))
    except FileNotFoundError as e:
        raise ExportError(
            f"No recording database found in {recording_dir}"
        ) from e

    with capture_ctx as capture:
        if output_path is None:
            # Write to stdout — no atomic write needed
            count = _write_events(capture, sys.stdout, exclude_moves, metadata)
        else:
            # Atomic write: .tmp → rename
            tmp_path = output_path + ".tmp"
            try:
                with open(tmp_path, "w") as f:
                    count = _write_events(capture, f, exclude_moves, metadata)
                os.rename(tmp_path, output_path)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

        if count == 0:
            logger.warning("Recording exported with 0 events: %s", recording_dir)
        return count


def _write_events(
    capture,
    out_file,
    exclude_moves: bool,
    metadata: dict | None,
    privacy_filter=None,
) -> int:
    """Stream events to an open file handle. Returns event count.

    Args:
        capture: CaptureSession instance.
        out_file: Writable file object.
        exclude_moves: Whether to exclude mouse.move events.
        metadata: Optional metadata dict for header line.
        privacy_filter: Optional callable(WindowSwitchEvent) -> WindowSwitchEvent | None.
            Returns None to suppress the event, or a modified event (e.g. masked title).
    """
    import click

    from screencap.engine.events import WindowSwitchEvent

    if metadata is not None:
        click.echo(json.dumps(metadata), file=out_file)

    count = 0
    for event in capture.export_events(include_moves=not exclude_moves):
        if isinstance(event, WindowSwitchEvent) and privacy_filter is not None:
            event = privacy_filter(event)
            if event is None:
                continue
        click.echo(event.model_dump_json(), file=out_file)
        count += 1
    return count
