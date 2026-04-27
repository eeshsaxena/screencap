"""Unified row-to-event transform for JSONL export.

This module is the single source of truth for the row-to-Pydantic-event
pipeline used by every export caller (CLI ``screencap export``, chunk
processor, recovery). Keeping this transform in one callable means a
fix to any pipeline stage applies once and propagates everywhere.

The function is pure: dict rows in, sorted Pydantic events out as an
``Iterator[BaseEvent]``. It performs no DB access, no file I/O, and
no privacy semantics for mouse coordinates (R6 places coordinate
suppression at the scrub layer, not here). Callers handle:

- Row fetching and the ``disabled`` filter (R16).
- ``initial_window_row`` timestamp rewrite to ``start_ts - 0.001`` (R3).
- ``mouse.move`` dropping (e.g., the CLI's ``--exclude-moves`` flag, R7).
- Privacy-aware ``window_filter`` construction via
  ``screencap.privacy.filter.build_cloud_window_filter`` (R5).
- ``_meta`` header generation and atomic file writes (R13, R14).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterator

from screencap.engine.events import BaseEvent, WindowSwitchEvent
from screencap.engine.processing import (
    DOUBLE_CLICK_DISTANCE_PIXELS,
    DOUBLE_CLICK_INTERVAL_SECONDS,
    KEY_TYPE_MERGE_INTERVAL_SECONDS,
    deduplicate_window_events,
    interleave_window_events,
    process_events,
)

if TYPE_CHECKING:  # pragma: no cover - import-only typing hint
    from screencap.engine.events import ActionEvent


def unified_export_events(
    action_rows: list[dict],
    window_rows: list[dict],
    *,
    initial_window_row: dict | None = None,
    double_click_interval: float = DOUBLE_CLICK_INTERVAL_SECONDS,
    double_click_distance: float = DOUBLE_CLICK_DISTANCE_PIXELS,
    key_type_merge_interval: float = KEY_TYPE_MERGE_INTERVAL_SECONDS,
    window_filter: Callable[[WindowSwitchEvent], WindowSwitchEvent | None] | None = None,
) -> Iterator[BaseEvent]:
    """Convert raw DB row dicts into a time-ordered Pydantic event stream.

    Steps (mirroring today's CLI export):

    1. Convert ``action_rows`` to Pydantic action events via
       ``dict_to_action_event``; rows that return ``None`` are skipped
       silently (matching the chunk processor's tolerance for malformed
       rows).
    2. Run ``process_events`` (the 11-stage merge pipeline) with the
       provided thresholds.
    3. ``MouseMoveEvent`` is **not** filtered here (R7). Callers that
       want move-dropping (e.g., CLI ``--exclude-moves``) apply it after
       consuming the iterator.
    4. If ``initial_window_row`` is provided, prepend it to ``window_rows``
       before deduplication. The caller is responsible for any
       timestamp rewrite (R3 — typically ``start_ts - 0.001``).
    5. Run ``deduplicate_window_events`` over the (optionally prepended)
       window rows to produce ``WindowSwitchEvent`` instances keyed by
       ``(app_bundle_id, window_id)``.
    6. If ``window_filter`` is provided, apply it to each
       ``WindowSwitchEvent``; entries where the filter returns ``None``
       are dropped.
    7. Run ``interleave_window_events`` to merge action and window
       sequences into a single time-ordered list.
    8. Yield each event from the combined sequence.

    R18 honest framing: the function returns an ``Iterator``, but
    ``process_events``' 11-stage merge pipeline materializes its
    action-event list internally — click pairing needs lookahead, drag
    detection needs lookback, key.type merging needs aggregate state.
    The engine cannot stream through that stage. This iterator yields
    events from the materialized post-``interleave`` sequence one at a
    time, which avoids ONE extra list copy at the engine→writer
    boundary. Peak RSS is bounded by ``process_events``' working set
    (~the size of the materialized action-event list), not by single
    event size.

    Args:
        action_rows: Dict rows from ``action_event`` table (sorted by
            timestamp). Caller has already applied any ``disabled``
            filter (R16).
        window_rows: Dict rows from ``window_event`` table (sorted by
            timestamp). Caller has already applied any ``disabled``
            filter (R16).
        initial_window_row: Optional last window-event row from before
            the chunk's ``start_ts``. When provided, the caller has
            already rewritten its timestamp to fall before the chunk
            (per R3). Pass ``None`` for full-recording exports (R15).
        double_click_interval: Time threshold for double-click detection
            (seconds). Defaults to ``process_events``' default.
        double_click_distance: Distance threshold for double-click
            detection (pixels). Defaults to ``process_events``' default.
        key_type_merge_interval: Time threshold for merging adjacent
            ``KeyTypeEvent`` instances (seconds). Defaults to
            ``process_events``' default.
        window_filter: Optional callable that receives each
            ``WindowSwitchEvent`` and returns either a possibly-modified
            event or ``None`` to suppress. Cloud-bound callers MUST
            construct this via
            ``screencap.privacy.filter.build_cloud_window_filter`` (R5).
            ``None`` (the default) passes all dedup'd window events
            through unchanged.

    Yields:
        ``BaseEvent`` instances in non-decreasing timestamp order:
        processed ``ActionEvent`` types interleaved with
        ``WindowSwitchEvent`` instances.
    """
    from screencap.engine.convert import dict_to_action_event

    # 1. Convert action_rows to Pydantic events; skip rows that return None.
    actions: list[ActionEvent] = []
    for row in action_rows:
        evt = dict_to_action_event(row)
        if evt is not None:
            actions.append(evt)

    # 2. Run the merge pipeline. Note: process_events materializes the
    #    full list — the iterator return type does not stream through
    #    this stage (see docstring R18 discussion).
    processed = process_events(
        actions,
        double_click_interval=double_click_interval,
        double_click_distance=double_click_distance,
        key_type_merge_interval=key_type_merge_interval,
    )

    # 3. Per R7, do NOT filter MouseMoveEvent here. Callers handle that.

    # 4. Optionally prepend initial_window_row (caller has rewritten ts per R3).
    if initial_window_row is not None:
        window_rows = [initial_window_row, *window_rows]

    # 5. Deduplicate by (app_bundle_id, window_id).
    window_switches = deduplicate_window_events(window_rows)

    # 6. Apply window_filter (typically the cloud privacy filter).
    if window_filter is not None:
        window_switches = [
            filtered
            for ws in window_switches
            if (filtered := window_filter(ws)) is not None
        ]

    # 7. Interleave action and window sequences by timestamp.
    combined = interleave_window_events(processed, window_switches)

    # 8. Yield one event at a time. This trims a list copy at the
    #    engine→writer boundary (R18) without changing peak RSS.
    yield from combined
