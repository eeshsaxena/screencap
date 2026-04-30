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

import logging
from typing import TYPE_CHECKING, Callable, Iterator

from screencap.engine.events import BaseEvent, WindowSwitchEvent
from screencap.engine.processing import (
    DOUBLE_CLICK_DISTANCE_PIXELS,
    DOUBLE_CLICK_INTERVAL_SECONDS,
    KEY_TYPE_MERGE_INTERVAL_SECONDS,
    deduplicate_window_events,
    interleave_network_events,
    interleave_window_events,
    process_events,
)

if TYPE_CHECKING:  # pragma: no cover - import-only typing hint
    from screencap.engine.events import ActionEvent
    from screencap.network.export_pipeline import NetworkScrubPipeline

logger = logging.getLogger(__name__)


def unified_export_events(
    action_rows: list[dict],
    window_rows: list[dict],
    *,
    initial_window_row: dict | None = None,
    double_click_interval: float = DOUBLE_CLICK_INTERVAL_SECONDS,
    double_click_distance: float = DOUBLE_CLICK_DISTANCE_PIXELS,
    key_type_merge_interval: float = KEY_TYPE_MERGE_INTERVAL_SECONDS,
    window_filter: Callable[[WindowSwitchEvent], WindowSwitchEvent | None] | None = None,
    network_rows: list[dict] | None = None,
    network_scrub_pipeline: "NetworkScrubPipeline | None" = None,
) -> Iterator[BaseEvent]:
    """Convert raw DB row dicts into a time-ordered Pydantic event stream.

    Steps (mirroring today's CLI export):

    1. Convert ``action_rows`` to Pydantic action events via
       ``dict_to_action_event``; rows that return ``None`` (unrecognized
       shapes) are skipped silently, and rows that **raise** during
       conversion (e.g., Pydantic ``ValidationError`` on type-wrong
       fields, ``KeyError``, ``TypeError``) are also skipped with a
       debug-level log entry. This mirrors the pre-refactor chunk
       processor's tolerance - without the inner try/except, one bad
       row would crash the entire chunk export, and recovery's
       skip-on-error policy would then lose the WHOLE chunk instead
       of just the bad row.
    2. Run ``process_events`` (the 11-stage merge pipeline) with the
       provided thresholds.
    3. ``MouseMoveEvent`` is **not** filtered here (R7). Callers that
       want move-dropping (e.g., CLI ``--exclude-moves``) apply it after
       consuming the iterator.
    4. If ``initial_window_row`` is provided, prepend it to ``window_rows``
       before deduplication. The caller is responsible for any
       timestamp rewrite (R3 - typically ``start_ts - 0.001``).
    5. Run ``deduplicate_window_events`` over the (optionally prepended)
       window rows to produce ``WindowSwitchEvent`` instances keyed by
       ``(app_bundle_id, window_id)``.
    6. If ``window_filter`` is provided, apply it to each
       ``WindowSwitchEvent``; entries where the filter returns ``None``
       are dropped.
    7. Run ``interleave_window_events`` to merge action and window
       sequences into a single time-ordered list.
    8. If ``network_rows`` is provided (V1: every caller keeps it
       ``None`` - events.jsonl emission of ``network.*`` lines is V1.75),
       convert each row via ``dict_to_network_event`` (per-row exception
       tolerance mirrors the action-row block above), then call
       ``interleave_network_events`` so the final stream is window-first,
       action-second, network-third on equality.
    9. Yield each event from the combined sequence.

    R18 honest framing: the function returns an ``Iterator``, but
    ``process_events``' 11-stage merge pipeline materializes its
    action-event list internally - click pairing needs lookahead, drag
    detection needs lookback, key.type merging needs aggregate state.
    The engine cannot stream through that stage. This iterator yields
    events from the materialized post-``interleave`` sequence one at a
    time, which avoids ONE extra list copy at the engine->writer
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
        network_rows: Optional dict rows from ``network_event`` table
            (sorted by timestamp). When ``None`` (the default), the
            output contains zero ``network.*`` events. **In V1, every
            caller passes ``None``** - JSONL emission of network events
            is gated on V1.75's cloud-bound filter wiring; V1's
            premise validation runs against ``recording.db.network_event``
            directly via ``screencap _network-dump``.
        network_scrub_pipeline: Optional V1.5 :class:`NetworkScrubPipeline`
            instance. When provided, network rows with ``body_ciphertext``
            are decrypted, scrubbed, and yielded as export-side events
            (``NetworkRequestExportEvent`` etc. with ``body_text``);
            rows without ciphertext (V1-vintage) pass through as
            capture-side events with no body data. When ``None`` (the
            default), the function emits capture-side events directly --
            **DO NOT use this on cloud-bound paths**, capture-side
            events carry ``body_ciphertext`` fields that must never
            reach JSONL. Construction of the pipeline (KEK lookup,
            DEK unwrap, ``DetectionPipeline`` init) happens at the
            CALLER; this function only consumes a ready pipeline.

    Yields:
        ``BaseEvent`` instances in non-decreasing timestamp order:
        processed ``ActionEvent`` types interleaved with
        ``WindowSwitchEvent`` instances (and, in V1.75+, network events).
        When ``network_scrub_pipeline`` is provided, network entries are
        the export-side ``Network*ExportEvent`` classes (bare
        ``BaseModel`` -- not ``BaseEvent`` subclasses, but the
        timestamp-ordered merge still works because they expose
        ``timestamp`` like ``BaseEvent``).
    """
    from screencap.engine.convert import dict_to_action_event, dict_to_network_event

    # 1. Convert action_rows to Pydantic events; skip rows that fail
    #    conversion. ``dict_to_action_event`` returns ``None`` for
    #    unrecognized shapes (handled silently) but can RAISE for
    #    type-wrong values (Pydantic ``ValidationError``, ``KeyError``,
    #    ``TypeError``, etc.). Mirrors the pre-refactor chunk processor's
    #    tolerance - without this try/except, one bad row would crash
    #    the entire chunk export, and recovery's skip-on-error policy
    #    would then lose the WHOLE chunk instead of just the bad row.
    actions: list[ActionEvent] = []
    for row in action_rows:
        try:
            evt = dict_to_action_event(row)
        except Exception as exc:
            logger.debug(
                "Skipping malformed action event at ts=%s: %s",
                row.get("timestamp", "?"),
                exc,
            )
            continue
        if evt is not None:
            actions.append(evt)

    # 2. Run the merge pipeline. Note: process_events materializes the
    #    full list - the iterator return type does not stream through
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

    # 8. Convert + interleave network rows when supplied. Per-row
    #    exception tolerance mirrors the action-row block above so a
    #    single malformed row is debug-logged and skipped (V1 keeps the
    #    metadata in the DB regardless; JSONL emission lands in V1.75).
    #
    #    V1.5: when ``network_scrub_pipeline`` is provided, the
    #    capture-side event is converted to its export-side counterpart
    #    via ``decrypt_and_scrub`` -- ciphertext fields are decrypted,
    #    plaintext is scrubbed by the DetectionPipeline, and the result
    #    is an export-side class (``Network*ExportEvent``) with
    #    ``body_text`` populated. Capture-side ciphertext NEVER reaches
    #    the iterator output by construction. Pipeline construction
    #    (KEK + DEK + DetectionPipeline) is the caller's responsibility.
    if network_rows is not None:
        network_events: list[BaseEvent] = []
        for row in network_rows:
            try:
                net_evt = dict_to_network_event(row)
            except Exception as exc:
                logger.debug(
                    "Skipping malformed network event at ts=%s: %s",
                    row.get("timestamp", "?"),
                    exc,
                )
                continue
            if net_evt is None:
                continue
            if network_scrub_pipeline is not None:
                # V1.5: decrypt + scrub the body-bearing event types
                # only. Other network event kinds (drop_burst, tunneled)
                # have no ciphertext fields and pass through unchanged
                # so their metadata still flows to JSONL even when
                # bodies are encrypted. Without this whitelist,
                # ``decrypt_and_scrub`` raises ValueError on the
                # non-body kinds and they are silently dropped from
                # the export stream — losing the drop-burst /
                # API-not-observable signal exactly when it's most
                # needed.
                from screencap.engine.events import (  # noqa: PLC0415
                    NetworkRequestEvent,
                    NetworkResponseEvent,
                    NetworkWebSocketFrameEvent,
                    NetworkWebSocketUpgradeEvent,
                )

                if isinstance(net_evt, (
                    NetworkRequestEvent,
                    NetworkResponseEvent,
                    NetworkWebSocketUpgradeEvent,
                    NetworkWebSocketFrameEvent,
                )):
                    try:
                        export_evt = network_scrub_pipeline.decrypt_and_scrub(
                            net_evt,
                        )
                    except Exception as exc:
                        logger.debug(
                            "Skipping network event whose decrypt+scrub "
                            "raised at ts=%s: %s",
                            row.get("timestamp", "?"),
                            exc,
                        )
                        continue
                    network_events.append(export_evt)
                else:
                    # drop_burst, tunneled, future non-body kinds.
                    network_events.append(net_evt)
            else:
                network_events.append(net_evt)
        # Defensive sort - callers fetch by ``ORDER BY timestamp_ns``
        # but the dict-input contract cannot enforce that. Sorting
        # here keeps the merge invariant intact for any caller.
        network_events.sort(key=lambda e: e.timestamp)
        combined = interleave_network_events(combined, network_events)

    # 9. Yield one event at a time. This trims a list copy at the
    #    engine->writer boundary (R18) without changing peak RSS.
    yield from combined
