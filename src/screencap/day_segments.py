"""Day-scoped read surface for the Day timeline (U3).

For a local calendar day, return each recording's span intersecting the day plus
its blocked intervals split into two honesty classes (R7):

* ``blocked_proven`` — explicit ``SCRUB_BLOCK_ACTIONS`` (MASK/EXCLUDE/secure-field)
  policy intervals read from an INTACT local ``recording.db``. Provably
  nothing-capturable at those times; the UI may hatch these "blocked".
* ``unverifiable`` — deleted-row coverage gaps (a screenshot with no covering
  ``window_event``, an orphan screenshot) or a NULL policy-relevant column
  (classification ambiguity). Fail-closed for capture decisions, but NOT proof
  nothing was captured — the UI must render these as neutral gaps, never labelled
  "blocked".
* ``purged`` — retroactive-disable purge spans (SCR-277) read back from the
  ``purged_interval`` ground truth ``scrub_worker`` persisted when it deleted the
  disabled app's rows. NOT capture-time masking, so never in ``blocked_proven``;
  each span optionally carries the disable target's identity joined from the
  ``.menubar_disable_log.jsonl`` audit log (degrading to identity-free on any
  ambiguity — never a guessed identity, R7).

Each recording also carries an additive ``end_status`` (``live`` / ``clean`` /
``interrupted`` / ``unknown``) resolved from its own clean-stop artifacts only —
day-bounded, no cross-day lookback — so the timeline can carry honest gap causes.
Ambiguous or corrupt evidence lands in ``unknown``, never a confident cause.

Local-only and read-only. The proven/unverifiable split reuses
``backfill.skip_intervals.derive_skip_intervals`` (the same reader the SCR-178
backfill and ``frame.nearest`` use) so the classes can't drift from the
capture-time block semantics: the canonical ``SCRUB_BLOCK_ACTIONS`` intervals are
``blocked_proven`` and the fail-closed residual reasons are ``unverifiable``.
"""

from __future__ import annotations

import calendar
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from screencap.backfill.skip_intervals import (
    AMBIGUOUS_BROWSER_URL,
    AMBIGUOUS_SECURE_FIELD,
    AMBIGUOUS_TITLE,
    ORPHAN_SCREENSHOT,
    RETROACTIVE_PURGE,
    UNCOVERED_GAP,
    build_classifier_evaluator,
    derive_skip_intervals,
)
from screencap.enforcement import disable_log
from screencap.recording_db import has_table, open_recording_db

logger = logging.getLogger(__name__)

# The fail-closed residual reasons ``skip_intervals`` adds on top of the canonical
# ``SCRUB_BLOCK_ACTIONS`` set. An interval carrying any of these is UNVERIFIABLE
# (we can't prove nothing was captured); every other reason is a proven block.
_UNVERIFIABLE_REASONS = frozenset(
    {
        UNCOVERED_GAP,
        ORPHAN_SCREENSHOT,
        AMBIGUOUS_BROWSER_URL,
        AMBIGUOUS_TITLE,
        AMBIGUOUS_SECURE_FIELD,
    }
)

_DAY_SECONDS = 86400

# Clean-stop / start-phase artifact filenames consulted by `resolve_end_status`.
# `.recording_ready` is written best-effort by session.py at clean stop (payload:
# elapsed / completed_at / disk_full / force_stopped / terminated_reason);
# `.recording_stop_meta.json` by the engine at stop; `system_metrics.json` at
# recording START with `"end": None` (the end phase fills it in).
_READY_SENTINEL = ".recording_ready"
_STOP_META_FILENAME = ".recording_stop_meta.json"
_METRICS_FILENAME = "system_metrics.json"
# The menubar disable audit log (enforcement/disable_log.py) — JSONL whose first
# line is a `_meta` frontmatter; entries carry `ts_unix` + `target{bundle_id,
# app_name, root_domain}`, joined against `purged_interval.disabled_at`.
_DISABLE_LOG_FILENAME = disable_log.LOG_FILENAME

# Sentinel distinguishing "artifact absent" (None) from "artifact present but
# unparseable" — corrupt evidence must land in `unknown`, never a confident cause.
_CORRUPT = object()


class InvalidDayRequest(ValueError):
    """The date string is malformed (mapped to a typed 400, never a 500)."""


def day_bounds(date_str: str, tz_offset_seconds: int) -> tuple[float, float]:
    """``[day_start, day_end)`` as UTC epoch seconds for a local calendar day.

    ``tz_offset_seconds`` is seconds EAST of UTC (``TimeZone.secondsFromGMT()`` on
    the Swift side), so midnight local on ``date_str`` maps to the UTC epoch of
    that Y-M-D at 00:00 minus the offset.
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise InvalidDayRequest(f"invalid date {date_str!r}: {exc}") from exc
    day_start = calendar.timegm(d.timetuple()) - tz_offset_seconds
    return float(day_start), float(day_start + _DAY_SECONDS)


def _clip_ms(
    start_s: float, end_s: float, win_start_s: float, win_end_s: float
) -> tuple[int, int] | None:
    """Clip ``[start_s, end_s)`` to the day window; ``(start_ms, end_ms)`` or None
    when the clipped interval is empty."""
    lo = max(start_s, win_start_s)
    hi = min(end_s, win_end_s)
    if hi <= lo:
        return None
    return int(round(lo * 1000)), int(round(hi * 1000))


def _read_json_dict(path: Path) -> Any:
    """Read ``path`` as a JSON object.

    Returns the dict, ``None`` when the file is absent, or :data:`_CORRUPT` when
    it exists but cannot be read/parsed (or parses to a non-dict) — the caller
    must treat corrupt evidence as ambiguous (→ ``unknown``), never confident.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return _CORRUPT
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return _CORRUPT
    return data if isinstance(data, dict) else _CORRUPT


def resolve_end_status(rec_dir: Path, state: str) -> str:
    """Resolve one recording's end status from its OWN artifacts (day-bounded —
    no cross-day lookback; the classifier only describes this recording).

    * ``live`` — the recording is active right now (``state == "recording"``).
      Checked FIRST: a live recording carries the start-phase marker with a null
      ``end`` (the interrupted signature) by construction.
    * ``interrupted`` — stop metadata records abnormal termination
      (``terminated_reason`` non-null / ``force_stopped`` in the ready payload or
      stop-meta sidecar), OR the start-phase ``system_metrics.json`` exists with
      ``"end": null`` and no ready sentinel — the recorder started and never
      reached its end phase.
    * ``clean`` — clean-stop artifacts present and normal.
    * ``unknown`` — no start-phase marker at all, or corrupt/unparseable
      artifacts. Honesty rule (R7): absence of clean-stop artifacts alone NEVER
      classifies ``interrupted``; ambiguous evidence lands here, never a
      confident cause.
    """
    if state == "recording":
        return "live"

    ready = _read_json_dict(rec_dir / _READY_SENTINEL)
    stop_meta = _read_json_dict(rec_dir / _STOP_META_FILENAME)
    metrics = _read_json_dict(rec_dir / _METRICS_FILENAME)
    if _CORRUPT in (ready, stop_meta, metrics):
        return "unknown"

    for payload in (ready, stop_meta):
        if payload is not None and (
            payload.get("terminated_reason") is not None
            or payload.get("force_stopped")
        ):
            return "interrupted"
    if ready is not None or stop_meta is not None:
        return "clean"  # clean-stop artifact present, nothing abnormal recorded
    if metrics is not None:
        # Start-phase marker exists; a still-null `end` means the end phase
        # never ran and no stop artifact exists either — the recorder died.
        return "interrupted" if metrics.get("end") is None else "clean"
    return "unknown"  # no start-phase marker at all — never guess interrupted


def _disable_log_targets(rec_dir: Path) -> dict[float, dict[str, Any] | None]:
    """Map ``ts_unix`` → disable-target identity from the audit log.

    Fail-open per line AND per file: a corrupt line is skipped, an unreadable /
    missing log yields ``{}`` (purge spans then surface identity-free — never an
    exception, never a dropped span). Two entries sharing a ``ts_unix`` make the
    join ambiguous → mapped to ``None`` (identity-free, never a guess).
    """
    targets: dict[float, dict[str, Any] | None] = {}
    try:
        lines = (rec_dir / _DISABLE_LOG_FILENAME).read_text(
            encoding="utf-8"
        ).splitlines()
    except Exception:
        return {}
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("_meta"):
            continue
        ts = entry.get("ts_unix")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        ts = float(ts)
        if ts in targets:
            targets[ts] = None  # >1 candidate — ambiguous, degrade to no identity
            continue
        target = entry.get("target")
        target = target if isinstance(target, dict) else {}
        ident = {
            k: target.get(k)
            for k in ("bundle_id", "app_name", "root_domain")
            if target.get(k) is not None
        }
        targets[ts] = ident or None
    return targets


def _purge_identity_map(
    db_path: Path, rec_dir: Path
) -> dict[tuple[float, float], dict[str, Any]]:
    """Map each ``purged_interval`` span ``(start, end)`` → disable-target identity.

    Reads ``(start_ts, end_ts, disabled_at)`` straight from the local-only
    ``purged_interval`` table (read-only, ``busy_timeout``; a missing table means
    no purges → ``{}``) and joins the ``.menubar_disable_log.jsonl`` audit log on
    ``disabled_at == ts_unix``. Degrades to identity-free (span omitted from the
    map) on a NULL ``disabled_at``, a missing/corrupt log entry, or any join
    ambiguity — the span itself still surfaces, only the identity is withheld.
    Keys mirror ``_read_purged_intervals``'s float mapping (NULL ``end_ts`` →
    ``+inf``) so they match the derived intervals exactly.
    """
    spans: dict[tuple[float, float], float | None] = {}
    try:
        with open_recording_db(db_path, busy_timeout_ms=10000) as conn:
            if not has_table(conn, "purged_interval"):
                return {}
            for start_ts, end_ts, disabled_at in conn.execute(
                "SELECT start_ts, end_ts, disabled_at FROM purged_interval "
                "WHERE start_ts IS NOT NULL"
            ):
                key = (
                    float(start_ts),
                    float(end_ts) if end_ts is not None else float("inf"),
                )
                if key in spans and spans[key] != disabled_at:
                    spans[key] = None  # conflicting provenance → identity-free
                else:
                    spans[key] = disabled_at
    except Exception:
        logger.warning(
            "day_segments: purged_interval identity read failed for %s",
            rec_dir, exc_info=True,
        )
        return {}

    log_targets = _disable_log_targets(rec_dir)
    out: dict[tuple[float, float], dict[str, Any]] = {}
    for key, disabled_at in spans.items():
        if disabled_at is None:
            continue
        ident = log_targets.get(float(disabled_at))
        if ident:
            out[key] = ident
    return out


def _blocked_intervals(
    rec_dir: Path, db_path: Path, win_start: float, win_end: float
) -> tuple[list[dict[str, int]], list[dict[str, int]], list[dict[str, Any]]]:
    """Return ``(blocked_proven, unverifiable, purged)`` interval lists for the
    day window.

    Strictly fail-open: any derivation error yields empty lists (a read surface
    must never 500 on one bad recording.db). The split is by the interval's
    ``reason``: residual reasons → unverifiable; ``RETROACTIVE_PURGE`` → purged
    (ground truth from the SCR-277 purge, NOT proven capture-time masking, so it
    must never read as ``blocked_proven``); everything else → proven.
    """
    from screencap.redaction.geometry import list_screenshot_timestamps

    proven: list[dict[str, int]] = []
    unverifiable: list[dict[str, int]] = []
    purged: list[dict[str, Any]] = []
    try:
        classifier, evaluator = build_classifier_evaluator(rec_dir)
        shots = [
            t for t in list_screenshot_timestamps(rec_dir) if win_start <= t < win_end
        ]
        intervals = derive_skip_intervals(
            db_path,
            classifier=classifier,
            evaluator=evaluator,
            time_range=(win_start, win_end),
            screenshot_timestamps=shots or None,
            require_canonical=False,  # fail-OPEN read surface
        )
    except Exception:
        logger.warning(
            "day_segments: skip-interval derivation failed for %s", rec_dir, exc_info=True
        )
        return proven, unverifiable, purged

    identity_map: dict[tuple[float, float], dict[str, Any]] | None = None
    for iv in intervals:
        clipped = _clip_ms(iv.start, iv.end, win_start, win_end)
        if clipped is None:
            continue
        entry: dict[str, Any] = {"start_ms": clipped[0], "end_ms": clipped[1]}
        if iv.reason == RETROACTIVE_PURGE:
            if identity_map is None:  # one identity read per recording, lazily
                identity_map = _purge_identity_map(db_path, rec_dir)
            entry.update(identity_map.get((iv.start, iv.end), {}))
            purged.append(entry)
        elif iv.reason in _UNVERIFIABLE_REASONS:
            unverifiable.append(entry)
        else:
            proven.append(entry)
    return proven, unverifiable, purged


def _read_task_segments(rec_dir: Path) -> list[dict[str, Any]]:
    """Read a recording's named task segments for the day-level ``tasks`` band (U9).

    Reuses the SAME read path ``tasks.list`` uses — ``read_task_segments`` off the
    recording's local-only ``recording.db`` (never uploaded — R4/R8) — so the day
    strip gets every task band in one round-trip without a per-recording
    ``tasks.list`` call, and the two surfaces can't drift.

    Returns ``[]`` on any legitimate absence — a missing ``recording.db`` (legacy /
    pre-U1 recording), a DB with no ``recording`` row, an unreadable DB, or a
    recording whose segmentation produced no tasks. Never raises for those (this is
    a fail-open read surface), so a recording with no tasks yields ``tasks: []``
    rather than a 500. Emits only the 6-field ``TaskSegment`` wire shape — the
    store's ``source`` / ``edited`` ownership columns stay internal. Local-only:
    nothing new leaves the machine.
    """
    from screencap.pipeline_state import read_task_segments_wire

    return read_task_segments_wire(rec_dir)


def day_segments(
    date_str: str,
    tz_offset_seconds: int = 0,
    recordings_dir: Path | None = None,
    store_mounted: bool = True,
) -> dict[str, Any]:
    """Return the day-scoped read surface (see module docstring).

    Shape::

        {"date": "YYYY-MM-DD",
         "store_mounted": bool,               # False → gaps are "can't verify"
         "recordings": [
            {"name", "recording_id", "state",
             "end_status",                    # "live"|"clean"|"interrupted"|"unknown"
             "start_ms", "end_ms",            # span clamped to the day
             "blocked_proven": [{"start_ms","end_ms"}, ...],
             "unverifiable":   [{"start_ms","end_ms"}, ...],
             "purged": [{"start_ms","end_ms",              # SCR-277 purge spans
                         "bundle_id"?, "app_name"?, "root_domain"?}, ...],
             "tasks": [{"task_index","start_ts","end_ts","name",
                        "category","confidence"}, ...]},  # U9 day-level bands
            ...
        ]}

    ``store_mounted``: this module cannot resolve the vault store state itself
    (``daemon/store_lifecycle.py`` owns it, and ``day_segments`` must not import
    the daemon package), so the handler passes it in; default ``True``. When
    False — or when the recordings dir is absent, the caller-independent signal
    of a locked/absent store — the result carries ``store_mounted: False`` and NO
    recordings, so the UI resolves the whole day to "can't verify" rather than a
    confident empty day.

    A recording that spans midnight appears in BOTH days, clamped to each. Raises
    :class:`InvalidDayRequest` on a malformed ``date_str`` (the handler maps that
    to a typed 400).
    """
    from screencap import catalog

    win_start, win_end = day_bounds(date_str, tz_offset_seconds)  # may raise InvalidDayRequest

    if recordings_dir is None:
        recordings_dir = catalog.get_recordings_dir()
    recordings_dir = Path(recordings_dir)

    mounted = bool(store_mounted) and recordings_dir.exists()
    result: dict[str, Any] = {
        "date": date_str,
        "recordings": [],
        "store_mounted": mounted,
    }
    if not mounted:
        return result

    # Single library scan: `list_recordings` already carries each recording's span
    # (`started_at` / `duration_seconds`), stable id, and derived state, so this
    # reads them straight off rather than re-walking the dir and re-opening every
    # recording.db a second time (`list_recordings` is the expensive pass).
    for meta in catalog.list_recordings(recordings_dir):
        started = meta.started_at
        if started is None:
            continue
        end = started + (meta.duration_seconds or 0.0)

        # Half-open overlap with the day window (a midnight-spanning recording
        # overlaps both days and is clamped into each). Both bounds are exclusive
        # of the far edge — `end > win_start`, not `>=`, so a recording that ends
        # exactly at local midnight belongs only to the previous day, not as a
        # zero-width span at the start of this one (matches `_clip_ms`'s convention).
        if not (started < win_end and end > win_start):
            continue
        clamped_start = max(started, win_start)
        clamped_end = max(min(end, win_end), clamped_start)

        rec_dir = recordings_dir / meta.name
        proven, unverifiable, purged = _blocked_intervals(
            rec_dir, rec_dir / "recording.db", clamped_start, clamped_end
        )

        result["recordings"].append(
            {
                "name": meta.name,
                "recording_id": meta.recording_id,
                "state": meta.state,
                # U1: per-recording end status resolved from this recording's
                # own artifacts (day-bounded — no cross-day lookback).
                "end_status": resolve_end_status(rec_dir, meta.state),
                "start_ms": int(round(clamped_start * 1000)),
                "end_ms": int(round(clamped_end * 1000)),
                "blocked_proven": proven,
                "unverifiable": unverifiable,
                # SCR-277 purge spans with optional disable-target identity —
                # honest provenance, never conflated with proven masking.
                "purged": purged,
                # U9: every task band for this recording, so the day strip
                # renders all bands without a per-recording tasks.list round-trip.
                "tasks": _read_task_segments(rec_dir),
            }
        )
    return result
