"""Export recording events as JSONL."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from screencap import __version__

# Backward-compat re-exports: `build_privacy_filter` moved to
# `screencap.privacy.filter` so cloud callers (chunk processor, recovery)
# can import from a neutral location. Existing tests and downstream
# imports of `screencap.exporter.build_privacy_filter` continue to work.
from screencap.privacy.filter import (  # noqa: F401
    build_cloud_window_filter,
    build_privacy_filter,
)

if TYPE_CHECKING:  # pragma: no cover - import-only typing hint
    from screencap.engine.events import BaseEvent

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


def write_events_jsonl(
    out_path: Path,
    events: Iterable[BaseEvent],
    meta: dict,
) -> int:
    """Stream events to ``out_path`` atomically as JSONL.

    Writes ``meta`` as line 1, then iterates ``events`` writing
    ``event.model_dump_json() + "\\n"`` per line. The actual writes go to
    ``out_path.with_suffix(out_path.suffix + ".tmp")``; on success the
    .tmp file is renamed onto ``out_path`` (atomic on POSIX). On any
    exception during the write loop the .tmp file is removed and the
    exception re-raises.

    Streaming is load-bearing: under R11 the chunk processor keeps
    ``mouse.move`` events by default, so a worst-case idle-reading
    recording can produce tens of MB of events. Iterating one event at a
    time bounds peak writer-side memory regardless of the input size.

    Stale-.tmp cleanup: any pre-existing .tmp at the target path is
    removed before opening the new one. This is defense-in-depth for the
    SIGKILL/OOM case where a previous run left a partial file behind.

    Args:
        out_path: Final destination path for the JSONL file. The .tmp
            sibling is derived by appending ``.tmp`` to the suffix
            (e.g. ``events.jsonl`` → ``events.jsonl.tmp``).
        events: Iterable of Pydantic events. Consumed lazily; supports
            iterators from ``unified_export_events``.
        meta: Header dict written as line 1 via ``json.dumps(meta)``.
            Typically built via :func:`build_export_metadata`. Required
            (no implicit "skip header" path here — callers that want a
            header-less file must use a different writer).

    Returns:
        Count of events written, **excluding** the meta line. Matches the
        return signature of the legacy :func:`_write_events` helper.

    Raises:
        Any exception raised by the events iterator, by Pydantic's
        ``model_dump_json``, or by the underlying file I/O is propagated
        after the .tmp file is unlinked.
    """
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    # Stale .tmp cleanup: a previous SIGKILL/OOM may have left a partial
    # file at this path. Drop it before we start writing.
    tmp_path.unlink(missing_ok=True)

    count = 0
    try:
        with open(tmp_path, "w") as f:
            f.write(json.dumps(meta) + "\n")
            for event in events:
                f.write(event.model_dump_json() + "\n")
                count += 1
        os.rename(tmp_path, out_path)
    except BaseException:
        # Cleanup-on-exception. Use missing_ok in case the open() itself
        # failed before the file was created.
        tmp_path.unlink(missing_ok=True)
        raise

    return count


def _write_events(
    capture,
    out_file,
    exclude_moves: bool,
    metadata: dict | None,
    privacy_filter=None,
) -> int:
    """Stream events to an open file handle. Returns event count.

    Legacy helper retained as a thin shim for backward compatibility.
    The ``privacy_filter`` kwarg is wired through here today; Unit 5
    will retire it once ``CaptureSession.export_events`` calls the
    unified callable with ``window_filter`` upstream.

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
