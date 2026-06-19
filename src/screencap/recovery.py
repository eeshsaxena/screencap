"""Crash-recovery for chunked recordings — shared by upload and native review.

When ``ChunkProcessor`` failed mid-recording but chunk video files were written,
``_recover_chunk_metadata`` regenerates the per-chunk manifests + ``events_*.jsonl``
the cloud processor (and the scrub-before-review path) need. It is consumed by
both ``screencap upload`` (``cli/__init__.py``) and ``screencap.review``; it lives
in this dependency-free leaf module so neither importer has to reach into the
other (previously ``review`` imported it from ``cli``, which imports ``review`` —
a cycle masked only by deferred imports).

All heavy imports are deferred inside the function bodies so importing this
module stays cheap; only ``json`` (for the intent reader) and ``Path`` are needed
at module scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console


def derive_chunk_grid(
    db_path: Path, n_chunks: int
) -> tuple[float, float, float] | None:
    """The single source of chunk-grid arithmetic: ``(base_ts, chunk_dur, last_ts)``.

    Shared by recovery (manifest / event windowing via :func:`derive_chunk_ranges`)
    and the terminal video masker (each chunk's absolute ORIGIN = ``base_ts +
    idx*chunk_dur``), so the two cannot drift (SCR-126 Fix 2 / R3). ``base_ts =
    min(recording.timestamp, MIN(action_event.timestamp))``; a config ``chunk_dur``
    (resolved to ``(last_ts - first_ts) / n_chunks`` when ``<= 0``); ``last_ts =
    MAX(action_event.timestamp)`` for the last-chunk extension.

    Returns ``None`` when the DB lacks a recording row or any action event, or on
    any read error — callers fall back to their own best-effort posture.
    """
    if n_chunks <= 0:
        return None
    from screencap.config import get_chunk_duration
    from screencap.recording_db import Row, open_recording_db

    try:
        with open_recording_db(db_path, row_factory=Row) as conn:
            rec = conn.execute(
                "SELECT timestamp FROM recording LIMIT 1"
            ).fetchone()
            if not rec:
                return None
            rec_start = rec["timestamp"]
            first_evt = conn.execute(
                "SELECT MIN(timestamp) as ts FROM action_event"
            ).fetchone()
            last_evt = conn.execute(
                "SELECT MAX(timestamp) as ts FROM action_event"
            ).fetchone()
        if not first_evt or first_evt["ts"] is None:
            return None
        first_ts = first_evt["ts"]
        last_ts = last_evt["ts"] if last_evt and last_evt["ts"] is not None else first_ts
        chunk_dur = get_chunk_duration()
        if chunk_dur <= 0:
            chunk_dur = (last_ts - first_ts) / max(n_chunks, 1)
        base_ts = min(rec_start, first_ts) if rec_start is not None else first_ts
        return (base_ts, chunk_dur, last_ts)
    except Exception:  # noqa: BLE001 — caller falls back to best-effort
        return None


def derive_chunk_ranges(
    db_path: Path, n_chunks: int
) -> list[tuple[int, float, float]]:
    """Per-chunk ``(idx, c_start, c_end)`` absolute event-windowing spans.

    Built from :func:`derive_chunk_grid` (the shared base/dur source) — a uniform
    ``chunk_dur`` grid whose LAST chunk's end extends to ``max(grid_end, last_ts +
    1.0)`` to cover all trailing events. The masker shares only the *origin*
    (``c_start``) via ``derive_chunk_grid``; it derives each chunk's END from the
    chunk's own decoded PTS extent (frames, not events), which legitimately differs
    from ``c_end``. Returns ``[]`` when the grid is unavailable.
    """
    grid = derive_chunk_grid(db_path, n_chunks)
    if grid is None:
        return []
    base_ts, chunk_dur, last_ts = grid
    ranges: list[tuple[int, float, float]] = []
    for idx in range(n_chunks):
        c_start = base_ts + idx * chunk_dur
        c_end = base_ts + (idx + 1) * chunk_dur
        if idx == n_chunks - 1:
            c_end = max(c_end, last_ts + 1.0)  # last chunk covers all events
        ranges.append((idx, c_start, c_end))
    return ranges


def _recover_chunk_metadata(
    recording_dir: Path, console: "Console", *, force: bool = False,
    cloud_bound: bool,
) -> None:
    """Generate per-chunk manifests + events JSONL when chunks exist but metadata doesn't.

    This is a recovery path for when ChunkProcessor failed during recording
    but chunk video files were created. Uses recording.db to derive chunk
    time ranges and generate the metadata files the Cloud Run processor needs.

    The ``cloud_bound`` keyword argument is REQUIRED (no default) — omitting
    it raises ``TypeError`` at the call site rather than silently falling
    open. ``screencap upload`` always passes ``cloud_bound=True`` regardless
    of ``.recording_intent`` content, because at upload time the data IS
    becoming cloud-bound by user choice. This closes the local-then-uploaded
    threat case (recording captured as ``destination=local``, later uploaded).

    LOAD-BEARING ORDERING: ``_recover_chunk_metadata`` MUST be followed by
    ``scrub_recording`` for cloud-bound recordings before upload. The
    scrub-layer pointer suppression (Unit 2) only protects recovered
    cloud-bound JSONL when this ordering holds. The upload command's
    inner per-recording loop satisfies this — see the ``LOAD-BEARING
    ORDERING`` comment block immediately preceding the
    ``_recover_chunk_metadata`` call inside ``upload``. Reordering or
    adding a recovery path that bypasses the scrubber MUST replicate the
    in-interval ``mouse.move`` drop at the engine layer or the cloud-bound
    privacy posture silently degrades.

    Skip-on-error policy: if ``unified_export_events`` raises mid-chunk on
    a corrupt action_event row that trips ``process_events`` aggregate-state
    or ``interleave_window_events``, we log a warning and skip writing
    that chunk's ``events_NNNN.jsonl`` (no file written). Other chunks
    proceed normally. ``screencap upload --force`` re-runs recovery once
    underlying data is fixed. Recovery's previous "raw dump tolerates
    everything" behaviour is intentionally retired by this refactor.
    """
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

    # Stale .tmp cleanup sweep: a previous SIGKILL/OOM may have left
    # .tmp files for chunks we're about to re-recover.
    # write_events_jsonl / generate_manifest also clean their own .tmp,
    # but this defense-in-depth sweep covers chunks we plan to skip
    # (e.g. corrupt-row failures) where the writer is never reached.
    for idx in missing_events:
        (recording_dir / f"events_{idx:04d}.jsonl.tmp").unlink(missing_ok=True)
    for idx in missing_manifests:
        (recording_dir / f"chunk_{idx:04d}_manifest.json.tmp").unlink(missing_ok=True)

    # Derive chunk time ranges from recording.db via the shared helper (the same
    # arithmetic the terminal masker uses for each chunk's absolute origin, so the
    # two cannot drift — SCR-126 Fix 2 / R3). Click thresholds and the
    # disabled-row / schema-drift handling live inside
    # ``screencap.export.export_chunk_events`` — recovery only needs the ranges.
    chunk_ranges = derive_chunk_ranges(db_path, len(chunk_videos))
    if not chunk_ranges:
        # No recording row / no action events / read error — nothing to window
        # against. Skip generation (best-effort recovery), as before.
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

    # Generate missing per-chunk events via the shared export seam.
    if missing_events:
        with console.status("[dim]Exporting per-chunk events...[/dim]"):
            from screencap.export import export_chunk_events
            from screencap.exporter import build_export_metadata, write_events_jsonl
            from screencap.enforcement.window_filter import build_cloud_window_filter

            # Resolve privacy_mode: prefer the locked-at-record-time value
            # in .recording_intent (matches what the live chunk processor
            # used) and fall back to current config when the intent file
            # is missing/corrupt.
            privacy_mode = _read_intent_privacy_mode(recording_dir)
            if privacy_mode is None:
                try:
                    from screencap.config import get_privacy_config
                    privacy_mode = get_privacy_config().mode.value
                except Exception:
                    privacy_mode = "internal"

            exported = 0
            for idx, c_start, c_end in chunk_ranges:
                if idx not in missing_events:
                    continue

                jsonl_path = recording_dir / f"events_{idx:04d}.jsonl"

                try:
                    # Per-chunk try so a config-load failure
                    # (e.g. InvalidPrivacyConfigError) marks one chunk as
                    # failed instead of aborting the whole recovery.
                    # Build the cloud filter inline at the call site so
                    # the privacy posture is visible — and statically
                    # auditable (see test_privacy_filter_call_graph.py)
                    # — at every cloud-capable caller.
                    events_iter = export_chunk_events(
                        recording_dir,
                        c_start,
                        c_end,
                        window_filter=build_cloud_window_filter(
                            cloud_bound=cloud_bound,
                            privacy_mode=privacy_mode,
                            capture_dir=recording_dir,
                        ),
                    )

                    meta = build_export_metadata(exclude_moves=False)
                    write_events_jsonl(jsonl_path, events_iter, meta)
                    exported += 1
                except Exception as e:
                    # Skip-on-error: a corrupt row that trips process_events
                    # or interleave invalidates this chunk's export. Other
                    # chunks proceed normally. write_events_jsonl already
                    # removed any partial .tmp.
                    console.print(
                        f"  [yellow]Warning:[/yellow] Event export failed for chunk {idx}: {e}"
                    )
            if exported:
                console.print(f"  [dim]Exported events for {exported} chunk(s)[/dim]")


def _read_intent_privacy_mode(recording_dir: Path) -> str | None:
    """Read ``privacy_mode`` from ``.recording_intent``, or ``None`` if absent.

    Mirrors :func:`screencap.catalog.read_intent`'s fail-safe behaviour:
    parse errors, missing keys, and missing files all return ``None`` so
    the caller falls back to whatever default it considers safe.
    """
    intent_path = recording_dir / ".recording_intent"
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
    except Exception:
        return None
    mode = data.get("privacy_mode")
    if isinstance(mode, str):
        return mode
    return None
