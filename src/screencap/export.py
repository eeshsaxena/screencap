"""Single seam for exporting events from a recording.

``export_chunk_events`` owns row fetching, schema-drift handling, and
the ``unified_export_events`` invocation for every export caller (CLI,
chunk processor, recovery). Callers supply the time range and the
``window_filter`` (built via ``build_cloud_window_filter`` for cloud-
bound paths); everything else lives here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from screencap.recording_db import (
    OperationalError,
    Row,
    has_column,
    has_table,
    open_recording_db,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from screencap.engine.events import WindowSwitchEvent
    from screencap.network.export_pipeline import NetworkScrubPipeline

PrivacyFilter = Callable[["WindowSwitchEvent"], "WindowSwitchEvent | None"]


def export_chunk_events(
    capture_dir: Path | str,
    start_ts: float | None = None,
    end_ts: float | None = None,
    *,
    window_filter: PrivacyFilter | None = None,
    materialized: bool = False,
    network_rows: list[dict] | None = None,
    network_scrub_pipeline: "NetworkScrubPipeline | None" = None,
):
    """Yield processed events from ``capture_dir``'s recording.db.

    When ``start_ts``/``end_ts`` are ``None``, the full recording is
    exported (CLI path). When provided, rows are sliced to
    ``[start_ts, end_ts)`` and the last window event before the slice
    is prepended with its timestamp rewritten to ``start_ts - 0.001``
    (R3 chunk-context invariant).

    ``window_filter`` is forwarded to ``unified_export_events`` and
    must come from ``build_cloud_window_filter`` for cloud-bound
    callers (``None`` for non-cloud, the cloud-mode callable for
    cloud).

    ``network_rows`` and ``network_scrub_pipeline`` pass straight
    through to ``unified_export_events``. The CLI is the only caller
    that supplies them today (V1.5 explicit ``--include-network``);
    chunk + recovery leave them at their defaults so JSONL stays
    cloud-safe.

    With ``materialized=True``, the result is a list (CLI's contract).
    Otherwise an iterator is returned so chunk + recovery can stream
    through ``write_events_jsonl``.
    """
    from screencap.engine.export import unified_export_events

    capture_dir = Path(capture_dir)
    db_path = capture_dir / "recording.db"

    with open_recording_db(db_path, row_factory=Row) as conn:
        click_interval, click_distance = _load_click_thresholds(conn)
        action_rows = _fetch_action_rows(conn, start_ts, end_ts)
        window_rows, initial_row = _fetch_window_rows(conn, start_ts, end_ts)

    events_iter = unified_export_events(
        action_rows,
        window_rows,
        initial_window_row=initial_row,
        double_click_interval=click_interval,
        double_click_distance=click_distance,
        window_filter=window_filter,
        network_rows=network_rows,
        network_scrub_pipeline=network_scrub_pipeline,
    )

    if materialized:
        return list(events_iter)
    return events_iter


def _load_click_thresholds(conn) -> tuple[float, float]:
    default_interval = 0.5
    default_distance = 5.0
    if not has_table(conn, "recording"):
        return default_interval, default_distance
    # Older recordings predate the threshold columns; guard the SELECT
    # so missing columns fall back to engine defaults rather than
    # raising OperationalError.
    if not (
        has_column(conn, "recording", "double_click_interval_seconds")
        and has_column(conn, "recording", "double_click_distance_pixels")
    ):
        return default_interval, default_distance
    # Fail-soft on OperationalError: live chunk export races the writer,
    # and a busy_timeout expiry on this optional read must not abort the
    # whole chunk — defaults keep the export flowing.
    try:
        row = conn.execute(
            "SELECT double_click_interval_seconds, double_click_distance_pixels "
            "FROM recording LIMIT 1"
        ).fetchone()
    except OperationalError:
        return default_interval, default_distance
    if row is None:
        return default_interval, default_distance
    interval = row["double_click_interval_seconds"]
    distance = row["double_click_distance_pixels"]
    return (
        float(interval) if interval is not None else default_interval,
        float(distance) if distance is not None else default_distance,
    )


def _fetch_action_rows(conn, start_ts, end_ts) -> list[dict]:
    disabled_clause = (
        " AND (disabled IS NULL OR NOT disabled)"
        if has_column(conn, "action_event", "disabled")
        else ""
    )
    if start_ts is None and end_ts is None:
        sql = f"SELECT * FROM action_event WHERE 1=1{disabled_clause} ORDER BY timestamp"
        params: tuple = ()
    else:
        sql = (
            f"SELECT * FROM action_event "
            f"WHERE timestamp >= ? AND timestamp < ?{disabled_clause} "
            f"ORDER BY timestamp"
        )
        params = (start_ts, end_ts)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _fetch_window_rows(conn, start_ts, end_ts):
    if not has_table(conn, "window_event"):
        return [], None
    disabled_clause = (
        " AND (disabled IS NULL OR NOT disabled)"
        if has_column(conn, "window_event", "disabled")
        else ""
    )
    # Fail-soft on OperationalError: window context is best-effort under
    # live-writer lock contention. A busy_timeout expiry degrades to "no
    # window context for this chunk" rather than failing the whole chunk
    # — action rows still export.
    if start_ts is None and end_ts is None:
        sql = f"SELECT * FROM window_event WHERE 1=1{disabled_clause} ORDER BY timestamp"
        try:
            rows = conn.execute(sql).fetchall()
        except OperationalError:
            return [], None
        return [dict(r) for r in rows], None

    sql = (
        f"SELECT * FROM window_event "
        f"WHERE timestamp >= ? AND timestamp < ?{disabled_clause} "
        f"ORDER BY timestamp"
    )
    try:
        rows = conn.execute(sql, (start_ts, end_ts)).fetchall()
    except OperationalError:
        return [], None
    window_rows = [dict(r) for r in rows]

    init_sql = (
        f"SELECT * FROM window_event "
        f"WHERE timestamp < ?{disabled_clause} "
        f"ORDER BY timestamp DESC LIMIT 1"
    )
    try:
        init_row = conn.execute(init_sql, (start_ts,)).fetchone()
    except OperationalError:
        return window_rows, None
    initial: dict | None = None
    if init_row is not None:
        initial = dict(init_row)
        initial["timestamp"] = start_ts - 0.001
    return window_rows, initial
