"""In-vault clips store + catalog (U10 / KTD-8).

A **clip** is a durable, deliberately-kept slice of a day's footage. Unlike the
recording tree — which retention evicts and the pipeline uploads — clips are
governed by two extra rules:

* **Retention-exempt + upload-excluded.** Clips live in a dot-prefixed reserved
  directory ``<recordings>/.clips/`` (mp4 files + a JSON catalog), matching the
  ``.store/`` sidecar convention so the recordings-tree enumerators
  (``catalog.list_recordings``, ``backfill.engine``, ``retention_sweep``,
  ``upload``) skip it for free. The store is sealed with the vault and hardened
  ``0o700`` / ``0o600``.
* **Privacy-honest.** ``create_clip`` FAILS CLOSED when the requested range
  overlaps a **policy-purged** interval (``origin`` NULL/absent/``'policy'``) —
  a retroactive privacy removal must never be undone by re-cutting the still-on-
  disk source pixels into a durable, retention-exempt artifact (R17). Conversely
  a **user** range-delete (``origin='user'``) does NOT block a cut (R11): the two
  origins are the entire distinction. When a *later* policy purge lands over an
  existing clip's span, :func:`purge_clips_for_intervals` deletes it (full
  overlap) or flags it (partial overlap); this is the propagation
  ``scrub_worker`` drives.

This module owns the store + catalog read/write and the engine-wrapping
orchestration (``create_clip``). The ``/v0/clip.*`` daemon verbs are thin
wrappers over it. The actual video trim is the shared ``screencap clip`` engine
(``engine.video.export_clip``), reused here in-process with the same failure
taxonomy the CLI reports (``not_eligible`` / ``no_frames_in_range`` /
``masked_video_required`` / ``clip_busy`` / ``trim_failed``) plus the new
``policy_purged`` fail-closed reason.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# The dot-prefixed reserved store dir + catalog, matching the ``.store/``
# convention (a by-literal contract with ``range_delete._overlapping_clips``,
# which reads ``<recordings>/.clips/catalog.json`` to disclose kept clips at
# delete-confirm time).
CLIPS_DIRNAME = ".clips"
CATALOG_NAME = "catalog.json"

# The clip failure taxonomy (reused from the ``screencap clip`` CLI, plus the new
# fail-closed reason). Carried in the verb envelope, never an exit code.
REASON_POLICY_PURGED = "policy_purged"
REASON_NOT_ELIGIBLE = "not_eligible"
REASON_TRIM_FAILED = "trim_failed"

# ``origin`` classification: only an explicit ``'user'`` row is a user delete;
# everything else (``'policy'`` AND legacy NULL/absent) classifies as policy — the
# same NULL→policy rule ``day_segments`` / ``skip_intervals`` apply.
_ORIGIN_USER = "user"

# The clip-scoped consent honesty flag (SCR-219 / KTD-8): every clip's video is
# capture-blocked (window-level) but its in-window on-screen text is NOT post-hoc
# masked, and it can leave to external recipients. Carried into the catalog so an
# agent-created clip stays attributable and the honesty signal survives.
HONESTY_CLIP_CAPTURE_BLOCKED = "clip_video_capture_blocked_only"
# Set on a clip that a LATER policy purge partially overlaps (kept, disclosed).
HONESTY_POLICY_PURGED_PARTIAL = "policy_purged_partial"


@dataclass(frozen=True)
class ClipCreateResult:
    """The outcome of :func:`create_clip`.

    ``ok`` True → ``clip`` is the persisted catalog entry. ``ok`` False → ``reason``
    is one of the taxonomy tokens above; nothing was written.
    """

    ok: bool
    reason: str | None = None
    clip: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Store paths + hardening.
# ---------------------------------------------------------------------------


def _recordings_dir(recordings_dir: Path | None) -> Path:
    if recordings_dir is not None:
        return Path(recordings_dir)
    from screencap import catalog

    return Path(catalog.get_recordings_dir())


def clips_dir(recordings_dir: Path | None = None) -> Path:
    """The reserved ``<recordings>/.clips/`` store dir (not created here)."""
    return _recordings_dir(recordings_dir) / CLIPS_DIRNAME


def _catalog_path(recordings_dir: Path | None = None) -> Path:
    return clips_dir(recordings_dir) / CATALOG_NAME


def clip_mp4_path(clip_id: str, recordings_dir: Path | None = None) -> Path:
    """The on-disk mp4 for ``clip_id`` (filename is id-derived — never re-parsed)."""
    return clips_dir(recordings_dir) / f"{clip_id}.mp4"


def _ensure_store(recordings_dir: Path | None = None) -> Path:
    """Create + harden the ``.clips`` dir (``0o700``, symlink-refusing)."""
    d = clips_dir(recordings_dir)
    if d.is_symlink():
        raise OSError(f"clips store path is a symlink: {d}")
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _harden_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Catalog read / write.
# ---------------------------------------------------------------------------


def read_catalog(recordings_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return the catalog entries (a tolerant read; ``[]`` when absent/torn)."""
    path = _catalog_path(recordings_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a torn/legacy catalog reads as empty, never raises
        logger.warning("clips: catalog read failed (treating as empty)", exc_info=True)
        return []
    entries = data.get("clips") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _write_catalog(entries: list[dict[str, Any]], recordings_dir: Path | None = None) -> None:
    """Atomically persist ``entries`` (tmp + replace), hardened ``0o600``."""
    d = _ensure_store(recordings_dir)
    path = d / CATALOG_NAME
    tmp = path.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps({"clips": entries}, separators=(",", ":"))
    tmp.write_text(payload, encoding="utf-8")
    _harden_file(tmp)
    os.replace(tmp, path)
    _harden_file(path)


def list_clips(recordings_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return the catalog entries newest-first (by ``created_at``)."""
    entries = read_catalog(recordings_dir)
    return sorted(entries, key=lambda e: e.get("created_at", 0.0), reverse=True)


# ---------------------------------------------------------------------------
# Purge-span reads + span math.
# ---------------------------------------------------------------------------


def _read_policy_purge_spans(db_path: Path) -> list[tuple[float, float]]:
    """The POLICY purge spans (seconds; NULL end → +inf) for a recording.

    Policy = every span whose ``origin`` is not the explicit ``'user'`` value —
    i.e. ``'policy'`` OR legacy NULL/absent (the same NULL→policy classification
    ``day_segments`` / ``skip_intervals`` use). A pre-U8 table (no ``origin``
    column) has only policy spans, so every row counts.
    """
    from screencap.recording_db import has_column, has_table, open_recording_db

    # A genuine "no purge table" is a safe empty; a READ ERROR is NOT swallowed
    # here — it propagates so the caller fails CLOSED. Returning [] on a torn
    # read would resurrect purged pixels if this connection hit a transient lock
    # the sibling anchor read didn't (they are separate connections).
    with open_recording_db(db_path, busy_timeout_ms=10000) as conn:
        if not has_table(conn, "purged_interval"):
            return []
        if has_column(conn, "purged_interval", "origin"):
            rows = conn.execute(
                "SELECT start_ts, end_ts FROM purged_interval "
                "WHERE start_ts IS NOT NULL "
                "AND (origin IS NULL OR origin != ?)",
                (_ORIGIN_USER,),
            ).fetchall()
        else:
            # Pre-U8: no origin column → every recorded purge was policy.
            rows = conn.execute(
                "SELECT start_ts, end_ts FROM purged_interval "
                "WHERE start_ts IS NOT NULL"
            ).fetchall()
    return [
        (float(s), float(e) if e is not None else float("inf"))
        for s, e in rows
    ]


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    """Half-open ``[a_start, a_end)`` ∩ ``[b_start, b_end)`` non-empty."""
    return a_start < b_end and b_start < a_end


def _fully_covered(
    cs: float, ce: float, spans: list[tuple[float, float]]
) -> bool:
    """Whether every point of ``[cs, ce)`` lies inside the union of ``spans``.

    Interval subtraction: carve each span out of the remaining fragments; fully
    covered iff nothing remains. (An open-ended ``inf`` span is handled naturally.)
    """
    remaining: list[tuple[float, float]] = [(cs, ce)]
    for s, e in spans:
        nxt: list[tuple[float, float]] = []
        for a, b in remaining:
            if e <= a or s >= b:  # disjoint
                nxt.append((a, b))
                continue
            if s > a:
                nxt.append((a, min(s, b)))
            if e < b:
                nxt.append((max(e, a), b))
        remaining = nxt
        if not remaining:
            return True
    return not remaining


# ---------------------------------------------------------------------------
# Engine wrap (the actual video trim).
# ---------------------------------------------------------------------------


def _export_clip(rec_dir: Path, rel_start_ms: int, rel_end_ms: int, out_path: Path) -> Path:
    """The default exporter — the shared ``screencap clip`` engine, in-process.

    A thin module-level seam so the daemon path uses the real PyAV trim while
    tests inject a byte-writing stub (Vision-free). ``rel_*_ms`` are milliseconds
    from video start (the engine's units); the absolute→relative conversion is the
    caller's job, mirroring the CLI ``clip`` command.
    """
    from screencap.engine.video import export_clip

    return export_clip(rec_dir, rel_start_ms, rel_end_ms, out_path)


def _local_day(start_ms: int, tz_offset_seconds: int) -> str:
    """The local calendar day (``YYYY-MM-DD``) a clip's ``start_ms`` falls in.

    Single-sourced from the KTD-11 rule (the inverse of
    ``day_segments.day_bounds``): a ``ts`` belongs to the day whose UTC-shifted
    value ``ts + tz_offset`` names — identical to ``tasks_query._local_day`` so
    Days cards, task days, and clip source-days can't drift.
    """
    ts = start_ms / 1000.0
    return datetime.fromtimestamp(ts + tz_offset_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )


def _valid_name(recording: str) -> bool:
    """Reject a traversal / separator-bearing recording name (defense-in-depth)."""
    return bool(recording) and "/" not in recording and "\\" not in recording and recording not in (".", "..")


def create_clip(
    recording: str,
    start_ms: int,
    end_ms: int,
    *,
    tz_offset_seconds: int = 0,
    creator: str = "ui",
    recordings_dir: Path | None = None,
    export_fn: Callable[[Path, int, int, Path], Any] | None = None,
) -> ClipCreateResult:
    """Cut ``[start_ms, end_ms)`` of ``recording`` into a durable clip (R11/R17).

    ``start_ms`` / ``end_ms`` are ABSOLUTE epoch milliseconds (the timeline units
    the Swift ``ClipRange`` and the daemon speak). Ordering:

    1. **Eligibility** (mirrors the CLI ``clip`` gate): the recording dir exists,
       holds at least one source ``chunk_*.mp4``, and has a readable video-start
       anchor → else ``not_eligible``.
    2. **Fail-closed policy check** (R17): if ``[start_ms, end_ms)`` overlaps ANY
       policy-purged span (``origin`` NULL/absent/``'policy'``) → ``policy_purged``,
       writing NOTHING. This runs BEFORE the trim so purged pixels are never
       re-cut into a durable artifact.
    3. **Trim** via the shared engine (absolute→relative-ms converted), mapping the
       engine taxonomy (``TerminalStageBusy`` → ``clip_busy``; ``ClipExportError``
       → its ``reason``; anything else → ``trim_failed``).
    4. **Persist** the mp4 + a catalog entry (``source_recording`` + ``source_day``
       + ``creator`` + honesty flag).

    Single-recording in v1 (KTD-8). Never raises for a clip-domain failure — the
    reason travels in the result.
    """
    export = export_fn if export_fn is not None else _export_clip
    if not _valid_name(recording):
        return ClipCreateResult(ok=False, reason=REASON_NOT_ELIGIBLE)

    rec_dir = _recordings_dir(recordings_dir) / recording

    # (1) Eligibility — a local, non-stub recording with source video present.
    if not (rec_dir.is_dir() and any(rec_dir.glob("chunk_*.mp4"))):
        return ClipCreateResult(ok=False, reason=REASON_NOT_ELIGIBLE)

    from screencap.catalog import read_video_start_anchor

    anchor_s = read_video_start_anchor(rec_dir)
    if anchor_s is None:
        return ClipCreateResult(ok=False, reason=REASON_NOT_ELIGIBLE)

    # (2) Fail-closed policy-purge overlap check — BEFORE cutting anything. A
    # purge-span read that ERRORS (transient lock, torn DB) refuses the cut
    # rather than proceeding: we must never resurrect purged pixels because we
    # couldn't positively clear the range.
    start_s, end_s = start_ms / 1000.0, end_ms / 1000.0
    try:
        policy_spans = _read_policy_purge_spans(rec_dir / "recording.db")
    except Exception:  # noqa: BLE001 — cannot clear the range → fail closed
        logger.warning("clips: policy-span read failed for %s; refusing cut", recording, exc_info=True)
        return ClipCreateResult(ok=False, reason=REASON_POLICY_PURGED)
    for ps, pe in policy_spans:
        if _overlaps(start_s, end_s, ps, pe):
            return ClipCreateResult(ok=False, reason=REASON_POLICY_PURGED)

    # (3) Absolute → relative (from-video-start) ms, then trim.
    anchor_ms = round(anchor_s * 1000)
    rel_start_ms = start_ms - anchor_ms
    rel_end_ms = end_ms - anchor_ms

    clip_id = uuid.uuid4().hex
    store = _ensure_store(recordings_dir)
    out_path = store / f"{clip_id}.mp4"

    from screencap.engine.video import ClipExportError
    from screencap.terminal_stage import TerminalStageBusy

    try:
        export(rec_dir, rel_start_ms, rel_end_ms, out_path)
    except TerminalStageBusy:
        # A contended eviction lock is retryable, not a hard failure (KTD-6).
        _safe_unlink(out_path)
        return ClipCreateResult(ok=False, reason="clip_busy")
    except ClipExportError as exc:
        _safe_unlink(out_path)
        return ClipCreateResult(
            ok=False, reason=getattr(exc, "reason", REASON_TRIM_FAILED) or REASON_TRIM_FAILED
        )
    except Exception:  # noqa: BLE001 — any unexpected engine error is a catch-all trim failure
        logger.warning("clips: unexpected trim failure for %s", recording, exc_info=True)
        _safe_unlink(out_path)
        return ClipCreateResult(ok=False, reason=REASON_TRIM_FAILED)

    _harden_file(out_path)

    # (4) Persist the catalog entry. ``source_recording`` is stored so purge
    # matching never has to re-derive it.
    entry = {
        "id": clip_id,
        "source_recording": recording,
        "source_day": _local_day(start_ms, tz_offset_seconds),
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "created_at": time.time(),
        "creator": creator if creator in ("ui", "mcp") else "ui",
        "honesty_flags": {HONESTY_CLIP_CAPTURE_BLOCKED: True},
    }
    entries = read_catalog(recordings_dir)
    entries.append(entry)
    _write_catalog(entries, recordings_dir)
    return ClipCreateResult(ok=True, clip=entry)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def delete_clip(clip_id: str, recordings_dir: Path | None = None) -> bool:
    """Remove ``clip_id``'s mp4 + catalog entry. Returns True iff it existed.

    Idempotent: deleting an unknown id is a benign ``False`` (no catalog rewrite).
    """
    entries = read_catalog(recordings_dir)
    keep = [e for e in entries if e.get("id") != clip_id]
    if len(keep) == len(entries):
        return False
    _safe_unlink(clip_mp4_path(clip_id, recordings_dir))
    _write_catalog(keep, recordings_dir)
    return True


# ---------------------------------------------------------------------------
# R17 — retroactive POLICY-purge propagation (driven by scrub_worker).
# ---------------------------------------------------------------------------


def purge_clips_for_intervals(
    recording: str,
    intervals: list[tuple[float, float]],
    *,
    recordings_dir: Path | None = None,
) -> dict[str, int]:
    """Propagate a POLICY purge of ``recording``'s ``intervals`` to its clips (R17).

    ``intervals`` are float unix SECONDS with a possible ``float('inf')`` trailing
    upper bound (the ``scrub_worker`` purge spans) — the SAME shape
    ``purge_content_index_intervals`` / ``purge_tasks_json_intervals`` receive.
    Widened floor-start / ceil-end (matching those siblings) so a clip whose ms
    bound was rounded can't survive at a sub-second boundary.

    Per catalog clip whose ``source_recording == recording`` and whose
    ``[start_ms, end_ms)`` intersects the purge union:

    * **fully covered** → DELETE (unlink the mp4 + drop the entry) — the whole clip
      is purged footage;
    * **partial overlap** → FLAG (``honesty_flags[policy_purged_partial] = True``,
      keep the file) — some kept footage remains, disclosed as containing purged
      content.

    This is the POLICY cascade only: a user range-delete (``origin='user'``) is
    driven by ``range_delete`` and deliberately never calls here (R11). Strictly
    fail-open — a clips-store hiccup must never break the scrub worker.
    """
    if not intervals:
        return {}
    try:
        # Convert seconds → ms with the widening the sibling purges use.
        spans_ms: list[tuple[float, float]] = []
        for s, e in intervals:
            s_ms = math.floor(s * 1000)
            e_ms = math.inf if e == float("inf") else math.ceil(e * 1000)
            spans_ms.append((float(s_ms), float(e_ms)))

        entries = read_catalog(recordings_dir)
        if not entries:
            return {}

        deleted = 0
        flagged = 0
        kept: list[dict[str, Any]] = []
        changed = False
        for e in entries:
            if e.get("source_recording") != recording:
                kept.append(e)
                continue
            try:
                cs = float(e["start_ms"])
                ce = float(e["end_ms"])
            except (KeyError, TypeError, ValueError):
                kept.append(e)
                continue
            overlaps_any = any(_overlaps(cs, ce, ps, pe) for ps, pe in spans_ms)
            if not overlaps_any:
                kept.append(e)
                continue
            if _fully_covered(cs, ce, spans_ms):
                _safe_unlink(clip_mp4_path(str(e.get("id")), recordings_dir))
                deleted += 1
                changed = True
                continue
            # Partial overlap → flag (kept, disclosed).
            flags = dict(e.get("honesty_flags") or {})
            if not flags.get(HONESTY_POLICY_PURGED_PARTIAL):
                flags[HONESTY_POLICY_PURGED_PARTIAL] = True
                e = {**e, "honesty_flags": flags}
                changed = True
            flagged += 1
            kept.append(e)

        if changed:
            _write_catalog(kept, recordings_dir)
        return {"deleted": deleted, "flagged": flagged}
    except Exception:  # noqa: BLE001 — R17 propagation is best-effort; never break the scrub
        logger.warning(
            "clips: policy-purge propagation failed for %s (non-fatal)",
            recording,
            exc_info=True,
        )
        return {}
