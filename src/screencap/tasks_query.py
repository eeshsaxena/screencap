"""Cross-day task-query read surface for the Tasks list (U5).

For a local calendar date range, answer "which named tasks exist across
recordings", grouped by the local calendar day each task belongs to, plus a
per-recording honest-status rollup so the UI can render honest zero states (R21)
rather than a bare empty list ("you did nothing").

Design (single day-mapping rule, KTD-11):

* The requested ``[start_date, end_date]`` window is resolved via
  ``day_segments.day_bounds`` — the SAME local-calendar-day intersection
  ``/v0/timeline.day`` uses (tz offset at request time). ``day_bounds`` raises
  :class:`day_segments.InvalidDayRequest` on a malformed date, which the handler
  maps to a typed 400.
* A task maps to ONE day: the local calendar day of its ``start_ts`` (the inverse
  of ``day_bounds`` — :func:`_local_day`). A midnight-spanning task therefore
  lands on the day it began, never duplicated across days. This is the pointer
  vocabulary of KTD-1 (``(recording, timestamp_ms)``) projected to a day.
* Tasks are read via the SAME per-recording read path ``tasks.list`` and the
  Day-timeline band use — ``pipeline_state.read_task_segments_wire`` off the
  recording's local-only ``recording.db`` (never uploaded — R4/R8) — so the three
  surfaces can't drift.

Local-only and read-only. Strictly fail-open per recording: a corrupt/unreadable
``recording.db`` contributes no tasks and a null honest status rather than 500-ing
the whole range.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from screencap.day_segments import day_bounds  # KTD-11: the single day-mapping rule


def _local_day(ts: float, tz_offset_seconds: int) -> str:
    """The local calendar day (``YYYY-MM-DD``) a Unix-seconds ``ts`` falls in.

    The inverse of :func:`day_segments.day_bounds`: ``day_bounds`` places local
    midnight of ``date`` at ``timegm(date) - tz_offset``, so a ``ts`` belongs to
    the day whose UTC-shifted value ``ts + tz_offset`` names. Guaranteed
    consistent with ``day_bounds`` (KTD-11) — ``day_bounds(_local_day(ts), tz)``
    always brackets ``ts``.
    """
    return datetime.fromtimestamp(ts + tz_offset_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )


def _read_outcome(rec_dir: Path) -> tuple[str | None, str | None]:
    """This recording's segmentation OUTCOME ``(reason, detail)`` — the honest
    status the rollup exposes, read fail-open.

    Mirrors the daemon's ``_run_recording_outcome``: ``(None, None)`` for a
    missing ``recording.db`` (legacy / pre-U2 recording), a DB with no recorded
    outcome, or any read error. Never raises — an outcome read must not fail the
    range query.
    """
    from screencap.pipeline_state import PipelineLedger

    db_path = rec_dir / "recording.db"
    if not db_path.exists():
        return None, None
    try:
        return PipelineLedger(db_path).get_recording_outcome_with_detail()
    except Exception:  # noqa: BLE001 — a status read must never fail the query
        return None, None


def query_tasks(
    start_date: str,
    end_date: str,
    tz_offset_seconds: int = 0,
    recordings_dir: Path | None = None,
) -> dict[str, Any]:
    """Return per-day task segments + honest-status rollup for a date range.

    Shape::

        {"start_date": "YYYY-MM-DD",
         "end_date": "YYYY-MM-DD",
         "days": [
            {"date": "YYYY-MM-DD",              # reverse-chronological (newest first)
             "tasks": [{"recording", "recording_id", "task_index",
                        "start_ts", "end_ts", "name",
                        "category", "confidence"}, ...]},  # start_ts-ordered
            ...
         ],
         "recordings": [                        # honest-status rollup (R21)
            {"name", "recording_id", "state",
             "reason",                          # produced_tasks / mechanical_only
                                                # / nothing_to_name / couldnt_run
                                                # / in_progress / None (unknown)
             "detail"},                         # SCR-275 U6 degradation detail / None
            ...
         ]}

    Every recording whose day-clamped coverage intersects the window contributes
    to ``recordings`` (so the rollup answers "why no tasks" even when it named
    none); only tasks whose ``start_ts`` falls inside the window are grouped into
    ``days``. Raises :class:`day_segments.InvalidDayRequest` on a malformed date
    or an inverted range (the handler maps that to a typed 400).
    """
    from screencap import catalog

    # KTD-11: resolve the window via the shared day rule. `day_bounds` raises
    # InvalidDayRequest on a malformed date; an inverted range is rejected the
    # same way so a client bug is a typed 400 rather than a silent empty result.
    range_start, _ = day_bounds(start_date, tz_offset_seconds)
    _, range_end = day_bounds(end_date, tz_offset_seconds)
    if range_end <= range_start:
        from screencap.day_segments import InvalidDayRequest

        raise InvalidDayRequest(
            f"inverted range: end_date {end_date!r} precedes start_date {start_date!r}"
        )

    if recordings_dir is None:
        recordings_dir = catalog.get_recordings_dir()
    recordings_dir = Path(recordings_dir)

    from screencap.pipeline_state import read_task_segments_wire

    days: dict[str, list[dict[str, Any]]] = {}
    # (started_at, rollup entry) pairs so the rollup can be ordered newest-first
    # without re-reading each recording's start time.
    rollup: list[tuple[float, dict[str, Any]]] = []

    for meta in catalog.list_recordings(recordings_dir):
        started = meta.started_at
        if started is None:
            # Unplaceable span (corrupt/unreadable recording.db) — can't confirm
            # it intersects the range, so it contributes nothing. (The Day
            # timeline surfaces this as incomplete coverage; the flat Tasks list
            # has no per-day coverage flag, so it is simply skipped.)
            continue
        end = started + (meta.duration_seconds or 0.0)
        # Half-open overlap with the window (mirrors day_segments): a recording
        # touching the range at all is a candidate.
        if not (started < range_end and end > range_start):
            continue

        rec_dir = recordings_dir / meta.name
        reason, detail = _read_outcome(rec_dir)
        rollup.append(
            (
                started,
                {
                    "name": meta.name,
                    "recording_id": meta.recording_id,
                    "state": meta.state,
                    "reason": reason,
                    "detail": detail,
                },
            )
        )

        for seg in read_task_segments_wire(rec_dir):
            # A task belongs to the range iff its start falls in the window; the
            # recording may extend past the range while individual tasks don't.
            if not (range_start <= seg["start_ts"] < range_end):
                continue
            day = _local_day(seg["start_ts"], tz_offset_seconds)
            days.setdefault(day, []).append(
                {
                    "recording": meta.name,
                    "recording_id": meta.recording_id,
                    "task_index": seg["task_index"],
                    "start_ts": seg["start_ts"],
                    "end_ts": seg["end_ts"],
                    "name": seg["name"],
                    "category": seg["category"],
                    "confidence": seg["confidence"],
                }
            )

    # Tasks within a day: chronological by start (then task_index / recording for
    # a stable order across recordings). Days: reverse-chronological (newest
    # first) so the Tasks surface (U6) renders most-recent-day-first directly.
    day_list = []
    for date in sorted(days, reverse=True):
        tasks = sorted(
            days[date], key=lambda t: (t["start_ts"], t["task_index"], t["recording"])
        )
        day_list.append({"date": date, "tasks": tasks})

    # Rollup: newest recording first (stable name tiebreak) — same reverse-
    # chronological framing as the days.
    rollup.sort(key=lambda pair: pair[1]["name"])
    rollup.sort(key=lambda pair: pair[0], reverse=True)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "days": day_list,
        "recordings": [entry for _started, entry in rollup],
    }
