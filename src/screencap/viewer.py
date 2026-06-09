"""Open recording viewer.html in macOS default browser."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from screencap.catalog import find_db
from screencap.config import resolve_recording_dir

console = Console()

# Progress/diagnostic messages go to stderr so they never corrupt a JSON
# payload on stdout. `_ensure_single_video` is shared with `screencap
# review-data --json`, whose only stdout output is the JSON envelope the
# SwiftUI shell parses; concat progress on stdout would make that unparseable.
err_console = Console(stderr=True)

# Viewer files above this size are assumed to be pre-fix cached files
# that should be auto-regenerated with caps.
_MAX_VIEWER_SIZE_BYTES = 200_000_000  # 200 MB

# Bound how long the on-demand concat waits for the per-recording terminal
# flock (U10). An eviction pass is fast (a few unlinks), so a short wait covers
# the real contention; we do NOT use terminal_lock's 600s default because a
# `screencap view` must not hang for minutes behind a long scrub/upload — it
# warns-and-continues (best-effort) or surfaces a clean failure (review-data)
# instead. Generous enough to ride out a normal eviction, short enough to stay
# interactive.
_CONCAT_LOCK_TIMEOUT = 30.0


def _chunk_offsets_for_concat(rec_dir: Path) -> dict[int, float] | None:
    """Map ``chunk_index -> seconds from recording start`` for absolute concat.

    Lets ``concat_video_chunks`` place each chunk at its real wall-clock offset
    so multi-chunk action-gated recordings keep the inter-chunk idle gaps that
    ``capture.py:get_frame_at`` relies on (SCR-98). Each value is
    ``chunk_start - video_start``, where:

    * ``chunk_start`` is the chunk's first-frame wall-clock, read from the
      ``chunk_NNNN_manifest.json`` ``chunk_start`` field (the durable on-disk
      carrier of ``ChunkedVideoWriter``'s rotation ``chunk_start_time``); and
    * ``video_start`` is the recording's ``video_start_time`` (falling back to
      ``timestamp``) — the exact anchor ``get_frame_at`` subtracts.

    Returns ``None`` (→ engine falls back to legacy summed-span stitching) when
    the data needed to place chunks absolutely is incomplete: fewer than two
    chunks, no/unreadable ``recording.db``, no recording row, or any chunk
    missing a parseable manifest ``chunk_start``. The all-or-nothing contract
    keeps a partial manifest set from producing a half-absolute timeline.
    """
    import json

    from screencap.engine.video import parse_chunk_index
    from screencap.recording_db import has_table, open_recording_db

    chunks = sorted(rec_dir.glob("chunk_*.mp4"))
    if len(chunks) < 2:
        return None

    db_path = find_db(rec_dir)
    if db_path is None:
        return None
    try:
        with open_recording_db(db_path) as conn:
            if not has_table(conn, "recording"):
                return None
            row = conn.execute(
                "SELECT video_start_time, timestamp FROM recording LIMIT 1"
            ).fetchone()
    except Exception as exc:
        err_console.print(
            f"[dim]Could not read chunk offsets from {db_path}: {exc}; "
            f"falling back to summed-span concat[/dim]"
        )
        return None
    if not row:
        return None
    # Mirror get_frame_at's anchor exactly (``video_start_time or timestamp``)
    # so concat places chunks against the same origin the consumer subtracts.
    video_start = row[0] or row[1]
    if video_start is None:
        return None
    video_start = float(video_start)

    offsets: dict[int, float] = {}
    for vf in chunks:
        idx = parse_chunk_index(vf.stem)
        if idx is None:
            return None
        manifest = rec_dir / f"chunk_{idx:04d}_manifest.json"
        if not manifest.exists():
            return None
        try:
            chunk_start = float(json.loads(manifest.read_text())["chunk_start"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            err_console.print(
                f"[dim]Cannot read chunk_start from {manifest.name}: {exc}; "
                f"falling back to summed-span concat[/dim]"
            )
            return None
        offsets[idx] = chunk_start - video_start
    return offsets


def _ensure_single_video(rec_dir: Path, *, fail_loud: bool = False) -> None:
    """If only chunked videos exist, concatenate them into ``rec_dir/video.mp4``.

    ``video.mp4`` is a **derived, on-demand artifact** — never the canonical
    record. The chunk set (``chunk_*.mp4``) plus the U1 ledger is the record
    (R2); this function materializes a single-file view of it for the consumers
    that still want one (the HTML viewer renders it, ``review-data`` reads it as
    a local navigation aid). It is idempotent and safe to delete: a later call
    re-derives it from the surviving chunks.

    Uses the in-process PyAV concat (stream copy, no re-encode) so the merge
    works with nothing installed on PATH — a Finder/Launchpad-launched ``.app``
    gets the minimal GUI PATH and cannot reach a ``brew``-installed ffmpeg.
    The merged file lives at ``rec_dir/video.mp4`` because many consumers depend
    on that location (the HTML viewer renders it, ``catalog`` uses it as one of
    several media-presence signals, ``capture``/``recorder`` read it). Legacy
    single-file recordings (R14) already HAVE a real ``video.mp4`` and no
    chunks, so the no-chunks early return leaves them untouched.

    Idempotent: early-returns if ``video.mp4`` already exists. A single chunk is
    symlinked rather than re-muxed.

    **Eviction-race serialization (U10).** A multi-chunk concat enumerates then
    reads ``chunk_*.mp4`` from the SOURCE dir — the same files U8 eviction
    (``retention.evict_recording``, always run inside the terminal stage's
    per-recording flock) can unlink mid-read. We take the SAME
    ``terminal_stage.terminal_lock`` (blocking-with-timeout) around the concat
    so eviction and concat are serialized on one recording: while we hold it the
    terminal stage cannot be evicting, and vice-versa. Combined with
    ``concat_video_chunks``'s atomic temp+``os.replace`` (a present ``video.mp4``
    is always complete), a partial concat can never leave a ``video.mp4`` that
    looks complete, and a chunk can never vanish mid-read during eviction.

    ``fail_loud`` selects the concat-failure posture. The default (``False``) is
    the HTML viewer's best-effort path: a merge failure (or a contended lock)
    warns and continues so ``screencap view`` still opens. The ``review-data``
    command (U4) passes ``True`` to propagate ``concat_video_chunks``'s error,
    which it translates into the R9 "can't process this video" envelope — it
    needs correctness, not graceful degradation.
    """
    chunks = sorted(rec_dir.glob("chunk_*.mp4"))
    if not chunks:
        return  # nothing to concat (incl. legacy single-file: real video.mp4)
    if (rec_dir / "video.mp4").exists():
        return  # already has the derived single video

    # Both the single-chunk symlink and the multi-chunk concat mutate video.mp4
    # from chunk_*.mp4 the terminal stage can evict, so BOTH go through the
    # per-recording flock (the lock-FIRST contract). A single chunk that gets
    # evicted between this glob and the symlink would otherwise leave a dangling
    # video.mp4 symlink — _concat_under_lock re-globs under the lock and handles
    # the 1-chunk / 0-chunk / 2+-chunk cases atomically w.r.t. eviction.

    # Serialize the concat against U8 eviction via the per-recording
    # terminal flock (the same lock eviction runs under). Import is deferred —
    # terminal_stage pulls heavier modules than the `screencap --help` path
    # tolerates, and the viewer's single-chunk/legacy fast paths above must not
    # pay for it.
    from screencap.terminal_stage import TerminalStageBusy, terminal_lock

    try:
        with terminal_lock(rec_dir.name, timeout=_CONCAT_LOCK_TIMEOUT):
            # Re-check under the lock: a concurrent concat (another `view`, or
            # the terminal stage's own derive) may have completed while we
            # waited, and the chunk set may have shifted under eviction.
            if (rec_dir / "video.mp4").exists():
                return
            _concat_under_lock(rec_dir)
    except TerminalStageBusy as e:
        # The terminal stage (likely mid-eviction) holds the lock past the
        # timeout. Refusing to read mid-eviction is the safe outcome — never a
        # torn read. Best-effort viewer warns and continues; review-data
        # propagates it as a clean "can't process this video" failure.
        if fail_loud:
            raise
        err_console.print(
            f"[yellow]Warning:[/yellow] Video merge skipped (recording busy): {e}"
        )


def _concat_under_lock(rec_dir: Path) -> None:
    """Run the PyAV concat for ``rec_dir`` (caller holds ``terminal_lock``).

    Re-globs the chunk set INSIDE the held lock so the enumeration the engine
    concats is the post-lock-acquisition snapshot, not a stale pre-wait one.
    """
    chunks = sorted(rec_dir.glob("chunk_*.mp4"))
    if len(chunks) < 2:
        # The set shrank to <2 while we waited for the lock (eviction ran first).
        # A single survivor is symlinked; zero is a no-op — never a partial mux.
        if len(chunks) == 1:
            try:
                (rec_dir / "video.mp4").symlink_to(chunks[0])
            except OSError:
                pass
        return

    err_console.print(f"[dim]Concatenating {len(chunks)} video chunks for viewer...[/dim]")
    from screencap.engine.video import concat_video_chunks

    # Absolute per-chunk offsets keep inter-chunk idle gaps so the merged
    # timeline tracks wall-clock recording time (SCR-98); None → engine
    # falls back to legacy summed-span stitching. concat_video_chunks writes
    # to a temp sibling and os.replace()s onto video.mp4 only on success, so a
    # raised failure here never leaves a half-written video.mp4.
    concat_video_chunks(rec_dir, chunk_offsets=_chunk_offsets_for_concat(rec_dir))
    err_console.print(f"[dim]Created merged video ({len(chunks)} chunks)[/dim]")


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
