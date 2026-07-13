"""Universal retention & eviction (U8).

Retention is a UNIVERSAL policy decoupled from upload (R10/R11/R12): it applies
to local-only recordings (size/time caps) exactly as it applies to cloud ones,
with a keep-forever default so existing local behavior is unchanged unless a cap
is set. Eviction is safe to run DURING an active recording so a long "run all
day" cloud session stays within bounded disk while upload lags.

The hard floor (prevention rule 3 — NEVER bypassed by age/size math)
--------------------------------------------------------------------
For ``cloud`` / ``both`` recordings, a chunk's rich LOCAL copy is evictable ONLY
when the chunk is ledger-``UPLOADED`` AND a FRESH remote re-confirmation
succeeds NOW. This is routed through
:meth:`~screencap.pipeline_state.PipelineLedger.begin_eviction`, whose
``remote_exists`` callback does the fresh re-stat (reusing U7's
:func:`~screencap.terminal_stage._chunk_confirmed_remote` seam, which derives the
GCS key from ``.recording_id`` correctly). A historical ``UPLOADED`` is NEVER
trusted. This floor holds under EVERY policy — ``delete_after_days`` and
``size_cap`` compute *which* uploaded chunks to evict, never *whether* an
un-uploaded one may be deleted.

For ``local`` recordings, chunks are ``LOCAL_DONE`` (never uploaded) and the
size/time cap evicts them freely with NO upload/remote precondition, via the
sibling :meth:`~screencap.pipeline_state.PipelineLedger.begin_local_eviction`
(``LOCAL_DONE``-only — it can never be used to bypass the cloud floor).

The un-evictable floor (consult the LEDGER, not file mtime)
-----------------------------------------------------------
Independent of the size/age computation, these chunks are NEVER candidates:

  - the currently-recording chunk and ANY chunk whose stages/scrub/upload are
    in-flight (lifecycle ``PENDING`` / ``STAGED`` / ``SCRUBBED``, or any
    non-terminal state);
  - a ``FAILED`` chunk (it blocks completeness — never evicted);
  - a ``SKIPPED`` chunk (uploads intentionally off; not ``UPLOADED``).

Only ``UPLOADED`` (cloud) or ``LOCAL_DONE`` (local) chunks are candidates. An
already-``EVICTED`` chunk is a no-op. The candidate predicate
:func:`_evictable_candidates` reads the ledger exclusively, so a freshly-rotated
chunk that happens to be the oldest by mtime is still never deleted while it is
in-flight.

``both`` — the rich/masked split
--------------------------------
For ``both`` the rich LOCAL copy follows its configured retention policy (e.g.
``keep_forever`` keeps it), while the MASKED cloud copy (in
``<name>-scrubbed/masked_video/``) is evicted IMMEDIATELY post-upload-confirm by
a FIXED rule — it is a derived transient that exists only to be uploaded. That
fixed rule still honours the floor: a masked copy is removed only when its chunk
is ``UPLOADED`` and a fresh remote confirm succeeds (it is a cloud artifact).

Disk-full tension (documented; ``disk_stop`` NOT changed here)
--------------------------------------------------------------
If disk fills with un-uploaded chunks (upload can't keep up), eviction frees
NOTHING — rule 3 forbids deleting an un-uploaded chunk to make room — and the
existing ``config.disk_stop_mb`` backstop stops RECORDING (it gates capture, not
eviction). Precedence: the never-delete-un-uploaded floor wins; ``disk_stop`` is
the backstop. Eviction must NEVER delete an un-uploaded chunk to make room, so
``size_cap`` can leave the recording over its cap when every over-cap chunk is
un-uploaded — that is correct, not a bug. We deliberately do not touch
``disk_stop_mb`` / ``disk_warn_mb`` (they gate recording).

Resumability
------------
Eviction is the two-phase ``EVICT_PENDING`` -> ``EVICTED`` transition the ledger
already enforces: an interrupted unlink resumes on the next pass via
``chunks_in_state(evict=EVICT_PENDING)`` -> re-confirm remote (cloud) ->
``commit_eviction``. No disk leak.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from rich.console import Console

    from screencap.pipeline_policy import ResolvedPolicy
    from screencap.pipeline_state import ChunkRow, PipelineLedger

logger = logging.getLogger(__name__)

__all__ = [
    "EvictionReport",
    "ScreenshotEvictionReport",
    "chunk_capture_bounds",
    "evict_recording",
    "evict_screenshots",
    "task_span_is_orphaned",
]


@dataclass
class EvictionReport:
    """Outcome of one :func:`evict_recording` pass.

    ``evicted_indices`` — chunks whose local rich copy was unlinked this pass
    (committed ``EVICTED``). ``evict_pending_resumed`` — chunks that were already
    ``EVICT_PENDING`` (a prior interrupted eviction) and were resumed to
    ``EVICTED`` this pass. ``masked_copies_evicted`` — chunk indices whose masked
    cloud copy (``<name>-scrubbed/masked_video/``) was removed (``both``).
    ``refused`` — candidate chunks whose eviction was refused this pass (e.g.
    remote re-confirm failed); a refused eviction leaves the file in place
    (safe). ``bytes_freed`` — total bytes unlinked (local rich + masked copies).
    """

    evicted_indices: list[int] = field(default_factory=list)
    evict_pending_resumed: list[int] = field(default_factory=list)
    masked_copies_evicted: list[int] = field(default_factory=list)
    refused: list[int] = field(default_factory=list)
    bytes_freed: int = 0
    # Screenshot retention (search U5): the count + bytes of stills trimmed from
    # the ``screenshots/`` dir by the age/size bound this pass. Independent of the
    # chunk eviction above; ``screenshot_bytes_freed`` is also folded into
    # ``bytes_freed``.
    screenshots_evicted: int = 0
    screenshot_bytes_freed: int = 0


@dataclass
class ScreenshotEvictionReport:
    """Outcome of one :func:`evict_screenshots` pass.

    ``evicted`` — the still filenames unlinked. ``bytes_freed`` — total bytes.
    ``index_rows_purged`` — content-index rows removed for the evicted frames.
    """

    evicted: list[str] = field(default_factory=list)
    bytes_freed: int = 0
    index_rows_purged: int = 0


# ---------------------------------------------------------------------------
# Per-chunk on-disk file set (mirrors chunk_processor._collect_chunk_files /
# _delete_old_chunks naming).
# ---------------------------------------------------------------------------


def _chunk_local_files(recording_dir: Path, idx: int) -> list[Path]:
    """The rich local files belonging to chunk ``idx`` (the eviction unit).

    The .mp4 / .flac / .jsonl media plus the per-chunk manifest. The
    ``recording.db`` (local-only, hosts the ledger) is NEVER touched, nor are
    the screenshots dir contents (kept whole until the recording is stubbed) —
    eviction reclaims the dominant media per chunk while leaving the ledger and
    the recording's metadata intact (the closed-set gate keys off the frozen
    ``chunks_expected``, not on-disk presence).
    """
    return [
        recording_dir / f"chunk_{idx:04d}.mp4",
        recording_dir / f"audio_{idx:04d}.flac",
        recording_dir / f"events_{idx:04d}.jsonl",
        recording_dir / f"chunk_{idx:04d}_manifest.json",
    ]


def _chunk_size_bytes(recording_dir: Path, idx: int) -> int:
    total = 0
    for p in _chunk_local_files(recording_dir, idx):
        try:
            if p.exists():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _unlink_chunk(recording_dir: Path, idx: int) -> int:
    """Unlink chunk ``idx``'s local files; return bytes freed. Best-effort."""
    freed = 0
    for p in _chunk_local_files(recording_dir, idx):
        try:
            if p.exists():
                freed += p.stat().st_size
                p.unlink()
        except OSError as exc:  # noqa: PERF203
            logger.warning("retention: failed to unlink %s: %s", p.name, exc)
    return freed


# ---------------------------------------------------------------------------
# Screenshot retention (search U5 / R2) — an age/size bound on the screenshots
# dir, independent of chunk eviction and of the chunk retention policy.
# ---------------------------------------------------------------------------


def _still_timestamp(path: Path) -> float | None:
    """Parse the epoch-seconds capture time from a still filename ``<ts>.jpg[.enc]``.

    Stills are named ``f"{event.timestamp:.6f}.jpg"`` (plus ``.enc`` when
    encrypted). Returns None for any name that does not parse (skipped, never
    evicted)."""
    from screencap import still_io

    name = still_io.logical_still_name(path)  # strips a trailing '.enc'
    if not name.endswith(".jpg"):
        return None
    try:
        return float(name[:-4])
    except ValueError:
        return None


def _last_indexed_ts(recording: str, store: "object | None" = None) -> float | None:
    """The newest indexed frame time (epoch seconds) for ``recording``, or None.

    The "don't race the indexer" high-water mark for the fast path; None when the
    index is absent/empty (no indexer constraint). ``store`` is an already-open
    ``ContentIndex`` to reuse across a batch (the daemon sweep opens ONE for the
    whole run instead of one per recording); when None, opens+closes its own."""
    try:
        from screencap.content_index import ContentIndex, default_index_path

        if store is not None:
            ms = store.max_indexed_timestamp_ms(recording)
            return ms / 1000.0 if ms is not None else None
        path = default_index_path()
        if not path.exists():
            return None
        with ContentIndex(path) as own:
            ms = own.max_indexed_timestamp_ms(recording)
        return ms / 1000.0 if ms is not None else None
    except Exception:  # noqa: BLE001 — the guard is best-effort; fall back to no constraint
        return None


def evict_screenshots(
    recording_dir: Path,
    *,
    now: float | None = None,
    days: int | None = None,
    size_cap_mb: int | None = None,
    last_indexed_ts: float | None = None,
    purge_index: bool = True,
) -> ScreenshotEvictionReport:
    """Evict old stills from ``recording_dir/screenshots`` by age and/or size cap.

    Independent of chunk eviction and of the chunk retention policy (a
    ``keep_forever`` recording still trims stills once a bound is set — search R2).
    ``days`` / ``size_cap_mb`` default to the configured bounds; ``0`` on an axis =
    unbounded there. ``last_indexed_ts`` (epoch seconds) is the "don't race the
    indexer" high-water mark — a still newer than it is never evicted (it may still
    be OCR'd); ``None`` applies no such constraint (converged recordings). Evicted
    frames' content-index rows are purged too (same sensitivity class — retention
    forgets the frame entirely). Fail-open: an unlink/purge hiccup is logged, never
    raised."""
    import time

    from screencap import config

    report = ScreenshotEvictionReport()
    recording_dir = Path(recording_dir)
    screenshots_dir = recording_dir / "screenshots"
    if not screenshots_dir.is_dir():
        return report

    now = time.time() if now is None else now
    days = config.get_screenshot_retention_days() if days is None else days
    size_cap_mb = config.get_screenshot_size_cap_mb() if size_cap_mb is None else size_cap_mb
    if days <= 0 and size_cap_mb <= 0:
        return report  # unbounded on both axes → today's behavior (nothing evicted)

    try:
        entries = list(screenshots_dir.iterdir())
    except OSError:
        return report

    stills: list[tuple[float, Path, int]] = []
    for p in entries:
        if not (p.name.endswith(".jpg") or p.name.endswith(".jpg.enc")):
            continue
        ts = _still_timestamp(p)
        if ts is None:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        stills.append((ts, p, size))
    if not stills:
        return report
    stills.sort(key=lambda s: s[0])  # oldest first

    def _protected(ts: float) -> bool:
        return last_indexed_ts is not None and ts > last_indexed_ts

    selected: list[tuple[float, Path, int]] = []
    selected_paths: set[Path] = set()

    if days > 0:
        cutoff = now - days * 86400
        for ts, p, size in stills:
            if ts < cutoff and not _protected(ts):
                selected.append((ts, p, size))
                selected_paths.add(p)

    if size_cap_mb > 0:
        cap_bytes = size_cap_mb * 1024 * 1024
        remaining = sum(s[2] for s in stills) - sum(s[2] for s in selected)
        for ts, p, size in stills:
            if remaining <= cap_bytes:
                break
            if p in selected_paths or _protected(ts):
                continue
            selected.append((ts, p, size))
            selected_paths.add(p)
            remaining -= size

    if not selected:
        return report

    evicted_ts: list[float] = []
    for ts, p, size in selected:
        try:
            p.unlink()
        except OSError as exc:
            logger.warning("retention: failed to unlink still %s: %s", p.name, exc)
            continue
        report.evicted.append(p.name)
        report.bytes_freed += size
        evicted_ts.append(ts)

    if evicted_ts and purge_index:
        try:
            _purge_screenshot_index_rows(
                recording_dir.name, min(evicted_ts), max(evicted_ts), report
            )
        except Exception:  # noqa: BLE001 — purge is best-effort; eviction already happened
            logger.warning(
                "retention: content-index purge after screenshot eviction failed",
                exc_info=True,
            )
    return report


def _purge_screenshot_index_rows(
    recording: str, min_ts: float, max_ts: float, report: ScreenshotEvictionReport
) -> None:
    """Purge the evicted frames' content-index rows over ``[min_ts, max_ts]`` (secs).

    Mirrors ``scrub_worker._purge_content_index_intervals``: never creates the store
    if indexing was never used, takes the shared write lock so it can't interleave
    with a concurrent inline index write, and widens the ms window to catch a frame
    whose ``round(ts*1000)`` landed on a boundary."""
    import math

    from screencap.content_index import default_index_path

    path = default_index_path()
    if not path.exists():
        return
    from screencap.content_index import ContentIndex, content_index_write_lock

    start_ms = math.floor(min_ts * 1000)
    end_ms = math.ceil(max_ts * 1000) + 1  # inclusive of the newest evicted frame
    with content_index_write_lock(), ContentIndex(path) as store:
        if not store.available:
            return
        report.index_rows_purged += store.delete_recording_interval(recording, start_ms, end_ms)


def _evict_screenshots_in_pass(recording_dir: Path, report: EvictionReport, *, now: float) -> None:
    """Fold a screenshot-retention pass into an in-progress :func:`evict_recording`.

    Resolves the configured bound; a still-unbounded config is a no-op. Uses the
    live content-index high-water mark so the during-recording fast path never
    evicts a frame the indexer has not reached yet."""
    from screencap import config

    days = config.get_screenshot_retention_days()
    size_cap_mb = config.get_screenshot_size_cap_mb()
    if days <= 0 and size_cap_mb <= 0:
        return
    sub = evict_screenshots(
        recording_dir,
        now=now,
        days=days,
        size_cap_mb=size_cap_mb,
        last_indexed_ts=_last_indexed_ts(recording_dir.name),
    )
    report.screenshots_evicted += len(sub.evicted)
    report.screenshot_bytes_freed += sub.bytes_freed
    report.bytes_freed += sub.bytes_freed


# ---------------------------------------------------------------------------
# The un-evictable floor predicate — consults the LEDGER only.
# ---------------------------------------------------------------------------


def _is_local_destination(policy: "ResolvedPolicy") -> bool:
    from screencap.pipeline_policy import Destination

    return policy.destination is Destination.LOCAL


def _evictable_candidates(
    ledger: "PipelineLedger", *, local: bool,
) -> list["ChunkRow"]:
    """Return ledger rows that are eligible eviction candidates, oldest-first.

    The un-evictable floor (NOT size/age math — pure ledger state):

      - ``local=True``  -> only ``LOCAL_DONE`` chunks (no upload precondition).
      - ``local=False`` -> only ``UPLOADED`` chunks (the cloud floor; a fresh
        remote re-confirm is still required at delete time by ``begin_eviction``).

    In BOTH cases an already-``EVICTED`` chunk is excluded (no-op), and every
    in-flight / ``FAILED`` / ``SKIPPED`` chunk is excluded by construction
    (they are neither ``UPLOADED`` nor ``LOCAL_DONE``). Ordering is by
    ``chunk_index`` (the rotation order — oldest first), so age/size policies
    evict the oldest evictable chunks first.
    """
    from screencap.pipeline_state import EvictState, Lifecycle, UploadState

    rows = ledger.all_chunks()  # already ordered by chunk_index
    out: list[ChunkRow] = []
    for r in rows:
        if r.evict_state == EvictState.EVICTED:
            continue
        if local:
            if r.lifecycle == Lifecycle.LOCAL_DONE:
                out.append(r)
        else:
            # Cloud floor: ONLY UPLOADED chunks. (EVICTED rows kept their
            # UPLOADED upload_state but are filtered above.)
            if r.upload_state == UploadState.UPLOADED and r.lifecycle != Lifecycle.EVICTED:
                out.append(r)
    return out


# ---------------------------------------------------------------------------
# SCR-214 U8/KTD5 — capture-time chunk bounds + kept-task-span protection.
#
# A "kept task span" pins its footage past the retention window (R14). Mapping a
# task span [start_ts, end_ts] (CAPTURE-time coords) to the chunk indices it
# overlaps MUST use per-chunk CAPTURE bounds — never the ledger ``updated_at``,
# which is a state-transition time (a chunk re-transitioned near eviction would
# mis-map). The chunk manifest carries ``chunk_start`` / ``chunk_end`` (Unix
# epoch) in BOTH the v1 and v2 formats; it is the authoritative capture window.
# The recording.db frame tables (action_event / screenshot / window_event) carry
# only ``timestamp`` and no ``chunk_index``, so there is no reliable per-chunk
# frame fallback — the manifest is the single source. It is unlinked ONLY at
# eviction, so every surviving (candidate) chunk still has it.
# ---------------------------------------------------------------------------


def chunk_capture_bounds(recording_dir: Path, idx: int) -> tuple[float, float] | None:
    """Per-chunk CAPTURE-time bounds ``(chunk_start, chunk_end)`` from its manifest.

    Reads ``chunk_<idx>_manifest.json``'s ``chunk_start`` / ``chunk_end`` (present
    in the v1 and v2 formats). Returns ``None`` when the manifest is missing /
    unparseable / lacks finite ordered bounds — the safe "unknown window" signal
    the protector treats conservatively. NEVER derived from the ledger
    ``updated_at`` (a state-transition time, not a capture window — KTD5).
    """
    import json
    import math

    manifest = recording_dir / f"chunk_{idx:04d}_manifest.json"
    try:
        data = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    start = data.get("chunk_start")
    end = data.get("chunk_end")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
        return None
    return float(start), float(end)


def _kept_task_spans(ledger: "PipelineLedger") -> list[tuple[float, float]]:
    """The recording's KEPT (user-curated) task spans: ``source='user' OR edited=1``.

    Only user-curated tasks pin footage past the retention window (R14/KTD5). The
    agent's ephemeral auto-split (``source='agent' AND edited=0``) does NOT — it
    relabels most of the active day, so pinning its spans would leave retention
    almost nothing to evict. Fail-safe: a read error yields no spans (retention
    proceeds under the age floor; a spurious protection is never invented).
    """
    from screencap.pipeline_state import task_row_is_protected

    try:
        rows = ledger.read_task_segments()
    except Exception as exc:  # noqa: BLE001 — protection is best-effort over the age floor
        logger.debug("retention: kept-task read failed (%s); no task protection", exc)
        return []
    spans: list[tuple[float, float]] = []
    for r in rows:
        if not task_row_is_protected(r):
            continue
        try:
            s, e = float(r.start_ts), float(r.end_ts)
        except (TypeError, ValueError):
            continue
        if e > s:
            spans.append((s, e))
    return spans


def _task_protected_indices(
    recording_dir: Path, ledger: "PipelineLedger", candidates: list["ChunkRow"],
) -> set[int]:
    """Candidate chunk indices to EXCLUDE because a KEPT task span covers them (R14).

    Maps each kept span to chunk indices via :func:`chunk_capture_bounds`
    (capture-time, never ``updated_at``) and protects any candidate whose capture
    window overlaps a kept span — whole-chunk granularity, so a chunk partially
    under a kept span is kept WHOLE (chunks are the eviction unit). A candidate
    with an unknown capture window while kept spans exist is conservatively
    protected (never evict footage that MIGHT lie under a kept task). Empty when
    there are no kept spans — the common non-ambient / no-user-task case is a
    zero-cost no-op.
    """
    from screencap.pipeline_state import spans_overlap

    spans = _kept_task_spans(ledger)
    if not spans:
        return set()
    protected: set[int] = set()
    for c in candidates:
        bounds = chunk_capture_bounds(recording_dir, c.chunk_index)
        if bounds is None:
            protected.add(c.chunk_index)
            continue
        cs, ce = bounds
        if any(spans_overlap(cs, ce, ss, se) for ss, se in spans):
            protected.add(c.chunk_index)
    return protected


def task_span_is_orphaned(
    recording_dir: Path, ledger: "PipelineLedger", start_ts: float, end_ts: float,
) -> bool:
    """True iff ``[start_ts, end_ts)`` points ONLY at EVICTED / absent footage (U8).

    The orphan guard for ``tasks.create``: a task must point at PLAYABLE footage,
    so persisting one over already-evicted chunks would leave an unplayable task.
    Evicted chunks have their media AND manifest unlinked, so their capture window
    is unknowable from disk; surviving chunks keep their manifest. To avoid
    blocking normal creation over live / legacy recordings, this refuses ONLY on
    POSITIVE eviction evidence: the recording has >=1 ``EVICTED`` chunk AND the
    requested span overlaps NONE of the surviving on-disk chunk windows. A
    recording with no evicted chunks (or no chunk/ledger info at all) fails OPEN
    (returns False). Any read hiccup fails open.
    """
    from screencap.pipeline_state import EvictState, spans_overlap

    try:
        rows = ledger.all_chunks()
    except Exception as exc:  # noqa: BLE001 — the guard must never block a create on a hiccup
        logger.debug("retention: orphan-guard chunk read failed (%s); allowing", exc)
        return False
    has_evicted = False
    for r in rows:
        if r.evict_state == EvictState.EVICTED:
            has_evicted = True
            continue
        bounds = chunk_capture_bounds(recording_dir, r.chunk_index)
        if bounds is not None and spans_overlap(bounds[0], bounds[1], start_ts, end_ts):
            return False  # surviving footage covers the span → playable → not orphaned
    return has_evicted


# ---------------------------------------------------------------------------
# Per-policy candidate selection — decides WHICH evictable chunks to evict.
# The floor is applied first; these only narrow within the candidate set.
# ---------------------------------------------------------------------------


def _select_for_policy(
    recording_dir: Path,
    candidates: list["ChunkRow"],
    policy: "ResolvedPolicy",
    *,
    now: float,
    age_reader: Callable[[list["ChunkRow"]], dict[int, float]],
    keep_recent: int = 0,
) -> list[int]:
    """Return the chunk indices to evict, drawn ONLY from ``candidates``.

    ``candidates`` has already passed the un-evictable floor, so every policy
    here is choosing among already-evictable chunks — it can never widen the
    set to an un-uploaded / in-flight / FAILED chunk. ``age_reader`` maps the
    candidate set to ledger ``updated_at`` for ``delete_after_days``.

    ``keep_recent`` (``delete_after_upload`` only) keeps the N most-recent
    (highest-index) UPLOADED candidates on disk — the during-recording window
    the live ``chunk_processor`` reclaim uses (it kept the 2 most recent), so
    routing the live path through this floor never deletes the freshest
    confirmed chunks mid-recording. ``0`` (the finalize default) evicts every
    candidate.
    """
    from screencap.pipeline_policy import RetentionPolicy

    rp = policy.retention_policy
    if rp == RetentionPolicy.KEEP_FOREVER:
        return []
    if rp == RetentionPolicy.DELETE_AFTER_UPLOAD:
        # Every candidate is evictable (cloud candidates are UPLOADED; local
        # candidates do not reach delete_after_upload meaningfully). Candidates
        # are oldest-first by chunk_index, so the keep_recent window is the tail.
        selected = [c.chunk_index for c in candidates]
        if keep_recent > 0:
            selected = selected[:-keep_recent]
        return selected
    if rp == RetentionPolicy.DELETE_AFTER_DAYS:
        return _select_past_days(candidates, policy.params, now=now, age_reader=age_reader)
    if rp == RetentionPolicy.SIZE_CAP:
        return _select_over_size_cap(recording_dir, candidates, policy.params)
    return []


def _select_past_days(
    candidates: list["ChunkRow"],
    params: dict,
    *,
    now: float,
    age_reader: Callable[[list["ChunkRow"]], dict[int, float]],
) -> list[int]:
    """Candidates whose ledger ``updated_at`` is older than ``days``.

    Uses the ledger ``updated_at`` (the last state transition for the chunk) as
    the age signal rather than file mtime, so age is computed from the same
    authoritative store the floor consults. ``days <= 0`` means every candidate
    is past the threshold. A candidate with no readable age is treated as fresh
    (kept) — conservative.
    """
    days = params.get("days")
    if not isinstance(days, (int, float)) or days < 0:
        return []
    cutoff = now - float(days) * 86400.0
    ages = age_reader(candidates)
    return [c.chunk_index for c in candidates if ages.get(c.chunk_index, now) <= cutoff]


def _select_over_size_cap(
    recording_dir: Path, candidates: list["ChunkRow"], params: dict,
) -> list[int]:
    """Evict the OLDEST evictable chunks until total media is under the cap.

    Computes total on-disk media for the candidate set (the only media eviction
    can reclaim — non-candidates are off-limits) and evicts oldest-first until
    the *remaining* candidate media is within ``size_cap_mb``. NEVER evicts a
    non-candidate to make room (the floor): if every over-cap chunk is a
    non-candidate, eviction frees nothing and the recording legitimately stays
    over its cap (the disk-full precedence documented in the module docstring).
    """
    cap_mb = params.get("size_cap_mb")
    if not isinstance(cap_mb, (int, float)) or cap_mb <= 0:
        return []
    cap_bytes = int(cap_mb) * 1024 * 1024

    # Candidate sizes, oldest-first (candidates already ordered by index).
    sized = [
        (c.chunk_index, _chunk_size_bytes(recording_dir, c.chunk_index))
        for c in candidates
    ]
    total = sum(sz for _, sz in sized)
    to_evict: list[int] = []
    # Evict oldest-first until the retained candidate media is within the cap.
    for idx, sz in sized:
        if total <= cap_bytes:
            break
        to_evict.append(idx)
        total -= sz
    return to_evict


# ---------------------------------------------------------------------------
# The executor.
# ---------------------------------------------------------------------------


def evict_recording(
    recording_dir: Path,
    *,
    policy: "ResolvedPolicy | None" = None,
    ledger: "PipelineLedger | None" = None,
    remote_exists: Callable[[int], bool] | None = None,
    now: float | None = None,
    during_recording: bool = False,
    keep_recent: int = 0,
    console: "Console | None" = None,
) -> EvictionReport:
    """Evaluate the frozen retention policy and evict per the hard floor.

    This is the single eviction entry point, invoked by the terminal stage both
    during recording (per-chunk pass, R12) and at finalize, AND by the live
    ``chunk_processor`` reclaim. It is idempotent and resumable: an interrupted
    ``EVICT_PENDING`` is finished first, then the policy selects further
    candidates.

    ``keep_recent`` (``delete_after_upload`` only) keeps the N most-recent
    UPLOADED chunks on disk — the live path passes 2 to preserve its
    during-recording window; the finalize/terminal pass leaves it 0 (evict all).

    Args:
        recording_dir: the recording's *source* directory (chunks + recording.db).
        policy: the FROZEN :class:`~screencap.pipeline_policy.ResolvedPolicy`. If
            ``None``, it is read from ``.recording_intent`` via
            ``catalog.read_intent_policy``; a missing/legacy policy is treated as
            ``keep_forever`` (no eviction — R11 default).
        ledger: the U1 ledger. If ``None`` it is opened over
            ``recording.db``; a legacy dir with no ledger evicts nothing.
        remote_exists: ``(idx) -> bool`` fresh remote re-confirmation for the
            CLOUD floor (and the masked-copy fixed rule). If ``None``, the real
            GCS re-stat seam (U7's ``_chunk_confirmed_remote``) is used. Local
            eviction ignores it entirely (no remote precondition).
        now: wall clock for age math (defaults to ``time.time()``).
        during_recording: True when called mid-recording (R12). Currently
            informational — the un-evictable floor already excludes the
            in-flight chunk by ledger state — but recorded for callers/logging.
        console: optional rich console (unused for output today; reserved).

    Returns:
        :class:`EvictionReport`.
    """
    import time

    from screencap.pipeline_policy import RetentionPolicy

    recording_dir = Path(recording_dir)
    now = time.time() if now is None else now
    report = EvictionReport()

    if policy is None:
        policy = _read_frozen_policy(recording_dir)
    if policy is None:
        # Legacy / no policy -> keep_forever (no eviction).
        return report

    if ledger is None:
        ledger = _open_ledger(recording_dir)
    if ledger is None:
        # Legacy single-file / no ledger -> nothing to evict by chunk.
        return report

    local = _is_local_destination(policy)
    # The cloud floor's remote re-confirmation callback (fresh re-stat NOW).
    confirm = _make_remote_confirm(recording_dir, remote_exists)

    # --- 1. Resume any interrupted eviction (EVICT_PENDING) FIRST. ---
    _resume_pending(
        recording_dir, ledger, report,
        local=local, confirm=confirm,
    )

    # --- 2. both: evict masked cloud copies immediately post-upload-confirm
    #        (a FIXED rule, independent of the configurable local retention). ---
    _evict_masked_cloud_copies(
        recording_dir, ledger, report, policy=policy, confirm=confirm,
    )

    # --- 2.5. Screenshot retention (search U5 / R2): an independent age/size bound
    #          on the screenshots dir, run under EVERY chunk policy (incl.
    #          keep_forever — a kept-forever recording still trims old stills). ---
    _evict_screenshots_in_pass(recording_dir, report, now=now)

    # --- 3. Select & evict per the configured policy for the LOCAL rich copy. ---
    if policy.retention_policy == RetentionPolicy.KEEP_FOREVER:
        return report

    candidates = _evictable_candidates(ledger, local=local)
    if not candidates:
        return report

    # SCR-214 U8/KTD5 (R14): never evict a chunk covered by a KEPT (user-curated)
    # task span. Map kept spans → chunk indices via CAPTURE-time bounds and drop
    # those candidates BEFORE the policy selects. A no-op (empty set) whenever the
    # recording has no user/edited task rows — the entire non-ambient path.
    protected = _task_protected_indices(recording_dir, ledger, candidates)
    if protected:
        candidates = [c for c in candidates if c.chunk_index not in protected]
        if not candidates:
            return report

    selected = _select_for_policy(
        recording_dir, candidates, policy,
        now=now, age_reader=_ledger_age_reader(ledger),
        keep_recent=keep_recent,
    )

    for idx in selected:
        _evict_one(
            recording_dir, ledger, report, idx,
            local=local, confirm=confirm,
        )

    return report


# ---------------------------------------------------------------------------
# Eviction primitives — every delete routes through the ledger's two-phase
# begin -> unlink -> commit so resumability + the floor hold uniformly.
# ---------------------------------------------------------------------------


def _evict_one(
    recording_dir: Path,
    ledger: "PipelineLedger",
    report: EvictionReport,
    idx: int,
    *,
    local: bool,
    confirm: Callable[[int], bool],
) -> None:
    """Evict one selected chunk via the ledger's two-phase transition.

    Cloud: ``begin_eviction`` re-confirms remote NOW (rule 3) then commits
    ``EVICT_PENDING``; on a False/refused confirm the file is left in place and
    the chunk is recorded in ``report.refused`` (safe). Local:
    ``begin_local_eviction`` (no remote precondition). Both finish with the
    SAME ``commit_eviction(unlink=...)`` so the unlink runs while still
    ``EVICT_PENDING`` (crash-resumable).
    """
    from screencap.pipeline_state import EvictionRefused

    try:
        if local:
            ledger.begin_local_eviction(idx)
        else:
            ledger.begin_eviction(idx, remote_exists=lambda: confirm(idx))
    except EvictionRefused as exc:
        logger.info("retention: eviction refused for chunk %d: %s", idx, exc)
        report.refused.append(idx)
        return

    _commit_unlink(recording_dir, ledger, report, idx)


def _resume_pending(
    recording_dir: Path,
    ledger: "PipelineLedger",
    report: EvictionReport,
    *,
    local: bool,
    confirm: Callable[[int], bool],
) -> None:
    """Finish any chunk left ``EVICT_PENDING`` by a prior interrupted run.

    Resumability contract: the unlink may have been interrupted, so the file
    can still be on disk. For CLOUD chunks we RE-CONFIRM remote existence before
    the unlink even on resume — a crash must not let a since-deleted remote copy
    be removed locally (rule 3 survives the crash). If the re-confirm now fails,
    we DO NOT unlink; the chunk stays ``EVICT_PENDING`` for a future pass
    (recorded as ``refused``). Local resume has no remote precondition.
    """
    from screencap.pipeline_state import EvictState

    pending = ledger.chunks_in_state(evict=EvictState.EVICT_PENDING)
    for row in pending:
        idx = row.chunk_index
        if not local:
            if not confirm(idx):
                logger.info(
                    "retention: resume re-confirm failed for chunk %d — leaving "
                    "EVICT_PENDING, not unlinking", idx,
                )
                report.refused.append(idx)
                continue
        before = len(report.evicted_indices)
        _commit_unlink(recording_dir, ledger, report, idx)
        if len(report.evicted_indices) > before:
            report.evict_pending_resumed.append(idx)


def _commit_unlink(
    recording_dir: Path,
    ledger: "PipelineLedger",
    report: EvictionReport,
    idx: int,
) -> None:
    """Run ``commit_eviction`` (unlink while EVICT_PENDING, then commit EVICTED)."""
    from screencap.pipeline_state import EvictionRefused

    freed_holder: dict[str, int] = {"bytes": 0}

    def _do_unlink() -> None:
        freed_holder["bytes"] = _unlink_chunk(recording_dir, idx)

    try:
        ledger.commit_eviction(idx, unlink=_do_unlink)
    except EvictionRefused as exc:
        logger.info("retention: commit_eviction refused for chunk %d: %s", idx, exc)
        report.refused.append(idx)
        return
    report.evicted_indices.append(idx)
    report.bytes_freed += freed_holder["bytes"]


# ---------------------------------------------------------------------------
# both: masked cloud copy immediate eviction (fixed rule, honours the floor).
# ---------------------------------------------------------------------------


def _evict_masked_cloud_copies(
    recording_dir: Path,
    ledger: "PipelineLedger",
    report: EvictionReport,
    *,
    policy: "ResolvedPolicy",
    confirm: Callable[[int], bool],
) -> None:
    """Remove masked cloud copies immediately post-upload-confirm (``both``).

    The masked copy lives at ``<name>-scrubbed/masked_video/chunk_*.mp4``. It is
    a derived transient that exists only to be uploaded, so its retention is a
    FIXED "evict immediately once UPLOADED + remote-confirmed" — NOT the
    configurable local retention. It still honours the floor: a masked copy is
    removed only when its chunk is ledger-``UPLOADED`` AND a fresh remote
    re-confirm succeeds (it is a cloud artifact). Applies to ``cloud`` and
    ``both`` (a ``cloud``-only recording with a masked copy on disk benefits
    identically); a no-op when the masked-video dir does not exist (the common
    case while the masked-video upload flag is OFF).
    """
    from screencap.pipeline_policy import Destination
    from screencap.pipeline_state import UploadState
    from screencap.scrubber import masked_video_dir

    if policy.destination is Destination.LOCAL:
        return
    scrubbed_dir = recording_dir.parent / f"{recording_dir.name}-scrubbed"
    mv_dir = masked_video_dir(scrubbed_dir)
    if not mv_dir.is_dir():
        return

    uploaded = {
        r.chunk_index for r in ledger.all_chunks()
        if r.upload_state == UploadState.UPLOADED
    }
    for vf in sorted(mv_dir.glob("chunk_*.mp4")):
        try:
            idx = int(vf.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        if idx not in uploaded:
            continue
        # Floor: fresh remote re-confirm before removing the cloud artifact.
        if not confirm(idx):
            logger.info(
                "retention: masked copy for chunk %d not removed — remote "
                "re-confirm failed", idx,
            )
            continue
        try:
            report.bytes_freed += vf.stat().st_size
            vf.unlink()
            report.masked_copies_evicted.append(idx)
        except OSError as exc:
            logger.warning("retention: failed to remove masked copy %s: %s", vf.name, exc)


# ---------------------------------------------------------------------------
# Wiring helpers — policy / ledger resolution + the remote-confirm seam.
# ---------------------------------------------------------------------------


def _read_frozen_policy(recording_dir: Path) -> "ResolvedPolicy | None":
    from screencap.catalog import read_intent_policy

    return read_intent_policy(recording_dir)


def _open_ledger(recording_dir: Path) -> "PipelineLedger | None":
    db_path = recording_dir / "recording.db"
    if not db_path.exists():
        return None
    try:
        from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

        ensure_pipeline_state_schema(db_path)
        return PipelineLedger(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("retention: ledger unavailable (%s); no eviction", exc)
        return None


def _make_remote_confirm(
    recording_dir: Path,
    remote_exists: Callable[[int], bool] | None,
) -> Callable[[int], bool]:
    """Return the fresh remote re-confirmation callback for the cloud floor.

    When ``remote_exists`` is injected (tests / a caller with its own re-stat),
    use it. Otherwise reuse U7's :func:`terminal_stage._chunk_confirmed_remote`
    — the SAME seam the terminal stage uses, which derives the GCS key from
    ``.recording_id`` and probes the chunk's core files via ``request_signed_urls``
    (``url=None`` ⇒ already there). Any error is conservative -> False
    (fail-closed: never a false positive that authorizes a delete).
    """
    if remote_exists is not None:
        def _confirm_injected(idx: int) -> bool:
            try:
                return bool(remote_exists(idx))
            except Exception:  # noqa: BLE001
                return False
        return _confirm_injected

    def _confirm_real(idx: int) -> bool:
        from screencap.terminal_stage import _chunk_confirmed_remote

        return _chunk_confirmed_remote(recording_dir, idx, remote_exists=None)

    return _confirm_real


def _ledger_age_reader(
    ledger: "PipelineLedger",
) -> Callable[[list["ChunkRow"]], dict[int, float]]:
    """Build a reader returning {chunk_index: updated_at} for the candidates."""

    def _reader(candidates: list["ChunkRow"]) -> dict[int, float]:
        if not candidates:
            return {}
        try:
            return ledger.updated_at_map([c.chunk_index for c in candidates])
        except Exception as exc:  # noqa: BLE001
            logger.debug("retention: age read failed (%s); treating as fresh", exc)
            return {}

    return _reader
