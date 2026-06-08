"""The single, disk-driven, flock-guarded terminal routing & lifecycle stage (U7).

This is the convergence point of the unified pipeline. Every recording — no
matter its destination — passes through ``run_terminal_stage`` exactly once
per (re)entry, and *every* entry point (live finalize, daemon-restart resume,
manual ``screencap upload``) drives this one function. The terminal stage:

* **Routes by the FROZEN per-recording policy** (``catalog.read_intent_policy``):

    - ``local``        → no scrub, no upload; each chunk is ``mark_local_done``.
    - ``cloud`` / ``both`` → produce the scrubbed/masked ``<name>-scrubbed`` cloud
      copy via the merged scrub seam (recovery filtered-export → ``scrub_recording``
      → optional U6 video masking), upload everything EXCEPT the raw
      ``recording.db`` (U2), ``mark_uploaded`` per chunk, and write the sentinel.

* **Is disk-driven & idempotent (R9).** All gating / reconcile / finalize read
  the U1 :class:`~screencap.pipeline_state.PipelineLedger`, never in-memory
  state. On (re)entry the stage first re-stats every not-yet-``UPLOADED`` chunk
  against the server (``upload.request_signed_urls`` returning ``url=None`` ⇒
  "already there") so an interrupted half-upload converges without
  re-uploading confirmed chunks. The completeness **sentinel is the LAST
  write**, gated on the FROZEN ``chunks_expected`` all being ``UPLOADED``
  (``ledger.finalize_gate_satisfied()``). A pre-existing legacy
  ``recording_complete.json`` is NEVER trusted (Bug 3) — it is regenerated
  from the ledger.

* **Serializes concurrent runs with a per-recording advisory flock (AE12).**
  ``terminal_lock`` is acquired as the **FIRST action on EVERY entry point,
  BEFORE any ledger read or reconcile.** A check-then-lock ordering would let
  two runs both observe the same PENDING/FAILED set and both call
  ``request_signed_urls`` — the lock-FIRST contract is what makes exactly-once
  hold. The lock is held across the ENTIRE reconcile→scrub→mask→upload→sentinel
  critical section and released only at the end.

  **flock is advisory.** It only serializes processes that take it. Every
  present and future terminal-stage entry point MUST call ``run_terminal_stage``
  (or otherwise hold ``terminal_lock``) — a path that uploads/evicts without
  the lock defeats AE12.

* **Fails closed.** A chunk ``FAILED`` at scrub/mask blocks the sentinel; the
  recording is NOT stubbed and local media is preserved.

Privacy-filter posture (load-bearing, do NOT relocate here)
-----------------------------------------------------------
The cloud window-title filter (``build_cloud_window_filter``) stays in its
existing GUARDED call sites — ``chunk_processor._export_events`` (live) and
``recovery._recover_chunk_metadata`` (upload). This module ORCHESTRATES the
cloud transform by REUSING that scrub seam (whose recovery export already
applies the filter); it deliberately does NOT call ``export_chunk_events``
itself, so ``tests/test_privacy_filter_call_graph.py`` stays green untouched.
Physically relocating the filter into this module is a deferred cosmetic
refactor (see the unit report), not done here.

Scrub-seam adapter
------------------
The consumed scrub seam (recovery + ``scrub_recording`` + U6 video masking) is
wrapped behind :class:`CloudCopyProducer` so a future upstream change to the
seam touches one place (Key Technical Decision: "isolate the consumed seam
behind a thin adapter").
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from rich.console import Console

    from screencap.pipeline_policy import ResolvedPolicy
    from screencap.pipeline_state import PipelineLedger

logger = logging.getLogger(__name__)

__all__ = [
    "TerminalStageBusy",
    "TerminalResult",
    "CloudCopyProducer",
    "PromotionRefused",
    "terminal_lock",
    "run_terminal_stage",
    "detect_promotion_holes",
    "assert_promotable_to_cloud",
]

# Per-recording terminal-stage lock lives under the daemon run dir (mode 0700,
# created here if needed), so it is never inside a recording dir the uploader
# enumerates. Kernel auto-releases the flock on process death (same property
# pidfile.py relies on) — no stale-lock cleanup needed.
_RUN_DIR = Path.home() / ".screencap" / "run"

# Blocking-with-timeout default: poll LOCK_NB so we can bound the wait and
# surface a clear TerminalStageBusy rather than hang forever. The loser in the
# AE12 race blocks here until the winner finishes (then converges over the
# winner's committed ledger state — idempotent, no double-upload), or raises
# TerminalStageBusy if the winner outlives the timeout.
_DEFAULT_LOCK_TIMEOUT = 600.0
_LOCK_POLL_INTERVAL = 0.1


class TerminalStageBusy(RuntimeError):
    """The per-recording terminal lock is held by another live run.

    Raised by ``terminal_lock`` when ``non_blocking=True`` and the lock is
    contended, or when the blocking-with-timeout wait elapses. The loser's
    safe behavior is to NOT proceed (no ledger read, no reconcile, no
    upload) — the holder owns the critical section.
    """


class PromotionRefused(RuntimeError):
    """A local->cloud promotion was refused because of HOLES (AE8).

    Raised by :func:`assert_promotable_to_cloud` when a chunk required for a
    COMPLETE upload has its local media evicted AND is not confirmed present in
    GCS. Refusing — rather than uploading the surviving chunks — is the safe
    outcome: it never produces a partial cloud copy with silent holes (the
    survivorship-bias data loss reproduced across the local/cloud boundary).
    The message names the missing chunk indices and the recording so the user
    can act.
    """


@dataclass
class TerminalResult:
    """Outcome of one ``run_terminal_stage`` invocation.

    Mirrors the shape ``collaborators.finalize_uploads`` returns so the live
    path can keep producing the same status dict for its post-stop messaging.
    """

    destination: str
    routed: bool = False
    sentinel_uploaded: bool = False
    all_uploaded: bool = False
    finalize_gate_satisfied: bool = False
    n_expected: int | None = None
    n_uploaded: int = 0
    n_failed: int = 0
    n_skipped: int = 0
    n_local_done: int = 0
    reconciled: int = 0
    stubbed: bool = False
    upload_warning: str | None = None
    failed_indices: list[int] = field(default_factory=list)
    # U8 retention/eviction outcome for this run (informational; the ledger is
    # the source of truth). ``evicted`` = chunk indices whose local rich copy
    # was reclaimed; ``masked_copies_evicted`` = chunk indices whose masked
    # cloud copy (<name>-scrubbed/masked_video/) was reclaimed post-upload.
    evicted: list[int] = field(default_factory=list)
    masked_copies_evicted: list[int] = field(default_factory=list)
    bytes_freed: int = 0


# ---------------------------------------------------------------------------
# The per-recording advisory flock — acquired FIRST on every entry point.
# ---------------------------------------------------------------------------


def _lock_path_for(name: str) -> Path:
    return _RUN_DIR / f"terminal-{name}.lock"


@contextlib.contextmanager
def terminal_lock(
    name: str,
    *,
    non_blocking: bool = False,
    timeout: float = _DEFAULT_LOCK_TIMEOUT,
):
    """Hold the per-recording terminal-stage advisory flock (AE12).

    This MUST be the FIRST action on every terminal-stage entry point, BEFORE
    any ledger read or ``reconcile_against_gcs`` — a check-then-lock ordering
    lets two runs both observe the same PENDING/FAILED set and both call
    ``request_signed_urls``, defeating exactly-once. Holding it across the
    entire critical section serializes the *decision*, not just the writes.

    Modes:

    * **blocking-with-timeout** (default) — wait up to ``timeout`` seconds for
      the holder to finish (polling ``LOCK_EX | LOCK_NB``), then raise
      ``TerminalStageBusy``. This is the right default for ``screencap upload``
      and daemon resume: the loser waits for the winner, then converges over
      the winner's committed ledger (idempotent — already-``UPLOADED`` chunks
      are not re-uploaded).
    * **non_blocking** (``non_blocking=True``) — try once; raise
      ``TerminalStageBusy`` immediately if contended. The "loser skips" mode,
      for a best-effort resume sweep that must not stall behind a live run.

    flock is advisory: it only excludes other holders of THIS lock. The kernel
    releases it on process death even if this contextmanager's ``finally`` is
    skipped (crash), so a crashed run never wedges the lock.
    """
    try:
        _RUN_DIR.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(_RUN_DIR, 0o700)
    except OSError:
        # Sandboxed / read-only run dir — degrade to unlocked rather than
        # blocking the operation, mirroring scrubber.recording_scrub_lock's
        # best-effort posture. Strictly no worse than pre-lock behavior; log
        # loudly so the lost serialization is visible.
        logger.warning(
            "terminal_lock: could not create run dir %s; proceeding UNLOCKED "
            "for %s — concurrent runs are NOT serialized",
            _RUN_DIR, name, exc_info=True,
        )
        yield
        return

    lock_path = _lock_path_for(name)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if non_blocking:
                    raise TerminalStageBusy(
                        f"terminal stage for {name!r} is already running "
                        "(non-blocking)"
                    ) from None
                if time.monotonic() >= deadline:
                    raise TerminalStageBusy(
                        f"terminal stage for {name!r} still busy after "
                        f"{timeout:.0f}s — another run holds the lock"
                    ) from None
                time.sleep(_LOCK_POLL_INTERVAL)
            except OSError as exc:
                # NFS / virtual-FS flock unsupported (EOPNOTSUPP/EINVAL): the
                # pidfile precedent closes the fd and propagates, but here a
                # terminal run must not be permanently un-runnable on such a
                # mount. Degrade to unlocked (loud warning), same as the
                # run-dir failure above.
                if exc.errno in (errno.EOPNOTSUPP, errno.EINVAL, errno.ENOLCK):
                    logger.warning(
                        "terminal_lock: flock unsupported on this filesystem "
                        "(%s); proceeding UNLOCKED for %s",
                        exc.strerror, name,
                    )
                    break
                raise
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


# ---------------------------------------------------------------------------
# Thin adapter over the merged native-redaction-review scrub seam.
# ---------------------------------------------------------------------------


@dataclass
class CloudCopyOutcome:
    """Result of producing the scrubbed/masked cloud copy for a recording.

    ``scrubbed_dir`` is the ``<name>-scrubbed`` sibling whose artifacts the
    terminal stage uploads (NEVER the raw source dir's ``recording.db``).
    ``failed_chunks`` lists chunk indices whose U6 video-mask failed
    (fail-closed) — non-empty means the sentinel is blocked and local media is
    preserved.
    """

    scrubbed_dir: Path
    failed_chunks: list[int] = field(default_factory=list)
    masked_chunks: list[int] = field(default_factory=list)


class CloudCopyProducer:
    """Thin adapter wrapping the consumed cloud-copy production seam.

    The seam is three merged pieces this unit deliberately does NOT reimplement:

    1. ``recovery._recover_chunk_metadata(cloud_bound=True)`` — regenerates
       per-chunk manifests + ``events_*.jsonl`` applying the cloud window
       filter (the GUARDED filter call site stays there, R5 satisfied by
       orchestration here).
    2. ``scrubber.scrub_recording(cloud_bound_recovery=True)`` — copies the
       recording into ``<name>-scrubbed`` and scrubs text/screenshots,
       writing the ``.scrub_complete`` sentinel as its last step.
    3. ``scrubber.mask_video_chunk_for_cloud`` — U6 post-hoc video masking,
       flag-gated (returns ``None`` when OFF → today's capture-blocked video
       is the cloud copy). Maps each chunk's outcome onto the ledger.

    Isolating these here means a future upstream reshape of the seam touches
    ONE module (this class), per the Key Technical Decision.
    """

    def __init__(self, recording_dir: Path, *, console: "Console | None" = None):
        self._recording_dir = Path(recording_dir)
        self._console = console

    def produce(
        self,
        *,
        ledger: "PipelineLedger | None",
        force: bool = False,
    ) -> CloudCopyOutcome:
        """Run recovery → scrub → (optional) video-mask; return the cloud copy.

        Returns the ``<name>-scrubbed`` dir to upload and the per-chunk video
        mask classification. Reuses the existing scrub-reuse guard so a
        review-prepared scrubbed copy is not rebuilt needlessly (reviewed ==
        uploaded), exactly as the CLI upload path does.

        Caller holds the terminal flock; this also takes
        ``recording_scrub_lock`` (a different, finer lock the scrub seam owns)
        via ``scrub_recording`` — they nest cleanly (different lock files).
        """
        from screencap.recovery import _recover_chunk_metadata
        from screencap.scrubber import (
            is_scrubbed_copy_reusable,
            scrub_recording,
        )

        console = self._console or _null_console()
        name = self._recording_dir.name
        scrubbed_dir = self._recording_dir.parent / f"{name}-scrubbed"

        # 1. Recovery — regenerate any missing manifests/events for chunks on
        # disk. cloud_bound=True UNCONDITIONALLY: at terminal-stage cloud
        # routing the data IS becoming cloud-bound (closes the
        # local-then-uploaded threat). This is the GUARDED filter call site.
        # LOAD-BEARING ORDERING: recovery MUST precede scrub.
        _recover_chunk_metadata(
            self._recording_dir, console, force=force, cloud_bound=True,
        )

        # 2. Scrub → <name>-scrubbed (or reuse a current review-prepared copy).
        if not force and is_scrubbed_copy_reusable(self._recording_dir, scrubbed_dir):
            logger.debug("Reusing reviewed scrubbed copy at %s", scrubbed_dir.name)
        else:
            # cloud_bound_recovery=True records the recovery provenance so a
            # later reuse is valid. We do NOT pass _already_locked — the
            # terminal flock is a *different* lock file than
            # recording_scrub_lock, so scrub_recording self-serializes its
            # own rebuild without a re-entrant deadlock.
            scrub_recording(name, cloud_bound_recovery=True)

        # 3. U6 post-hoc video masking (flag-gated; None when OFF).
        outcome = CloudCopyOutcome(scrubbed_dir=scrubbed_dir)
        self._mask_videos(scrubbed_dir, ledger, outcome)
        return outcome

    def _mask_videos(
        self,
        scrubbed_dir: Path,
        ledger: "PipelineLedger | None",
        outcome: CloudCopyOutcome,
    ) -> None:
        """Apply U6 video masking per chunk and map results onto the ledger.

        Flag OFF (today's default): ``mask_video_chunk_for_cloud`` returns
        ``None`` for every chunk → no masked copy, the capture-blocked source
        chunk IS the cloud copy. Flag ON: a ``MaskOutcome.ok`` chunk →
        ``mark_scrubbed``; a ``FAILED`` chunk → ``mark_failed`` (blocking the
        sentinel + eviction) and recorded in ``outcome.failed_chunks``.
        """
        from screencap.config import get_masked_video_upload_enabled

        if not get_masked_video_upload_enabled():
            # Conservative posture: masker not invoked. The source chunk media
            # (capture-blocked for cloud today) is the cloud copy. No ledger
            # scrub-state change here — the agnostic STAGED state plus the
            # upload confirmation drives the gate.
            return

        from screencap.scrubber import mask_video_chunk_for_cloud

        db_path = self._recording_dir / "recording.db"
        chunk_videos = sorted(self._recording_dir.glob("chunk_*.mp4"))
        chunk_start_abs, chunk_dur = _chunk_timing(db_path, len(chunk_videos))
        for vf in chunk_videos:
            try:
                idx = int(vf.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            start_ts = chunk_start_abs + idx * chunk_dur
            end_ts = start_ts + chunk_dur
            try:
                mask_outcome = mask_video_chunk_for_cloud(
                    vf, db_path, scrubbed_dir,
                    chunk_index=idx, start_ts=start_ts, end_ts=end_ts,
                    # The chunk's OWN first-frame absolute time is start_ts, not
                    # the recording base: each chunk mp4's PTS restarts near 0,
                    # so video_mask maps frames as start_ts + frame_pts. Passing
                    # the recording base would shift every chunk idx>=1's frames
                    # outside the coverage span (mis-aligned masking).
                    chunk_start_abs=start_ts,
                )
            except Exception as exc:  # noqa: BLE001 — fail closed on any error
                logger.error("video mask raised for chunk %d: %s", idx, exc)
                outcome.failed_chunks.append(idx)
                if ledger is not None:
                    with contextlib.suppress(Exception):
                        ledger.mark_failed(idx, detail=f"video_mask error: {exc}")
                continue
            if mask_outcome is None:
                # Flag flipped off mid-loop, or this chunk produced no copy by
                # design — treat as no-op (handled by the flag short-circuit).
                continue
            if mask_outcome.ok:
                outcome.masked_chunks.append(idx)
                if ledger is not None:
                    with contextlib.suppress(Exception):
                        # Do NOT downgrade a chunk a prior reconcile already
                        # advanced to UPLOADED/EVICTED — mark_scrubbed sets
                        # lifecycle=SCRUBBED, and the later _mark_uploaded_chunks
                        # pass skips an already-UPLOADED upload_state, so the
                        # row would be stuck SCRUBBED and never satisfy the
                        # finalize gate.
                        from screencap.pipeline_state import Lifecycle as _LC
                        _row = ledger.get_chunk(idx)
                        if _row is None or _row.lifecycle not in (
                            _LC.UPLOADED, _LC.EVICTED,
                        ):
                            ledger.mark_scrubbed(idx)
            else:
                outcome.failed_chunks.append(idx)
                if ledger is not None:
                    with contextlib.suppress(Exception):
                        ledger.mark_failed(idx, detail=mask_outcome.reason or "video_mask FAILED")


def _chunk_timing(db_path: Path, n_chunks: int) -> tuple[float, float]:
    """Derive (chunk_start_abs, chunk_dur) from recording.db, mirroring recovery.

    Best-effort: on any failure return ``(0.0, configured-duration)`` so the
    caller still produces frame spans (the U6 coverage gate fails closed on
    bad spans anyway).
    """
    from screencap.config import get_chunk_duration

    chunk_dur = get_chunk_duration()
    try:
        from screencap.recording_db import Row, open_recording_db

        with open_recording_db(db_path, row_factory=Row) as conn:
            rec = conn.execute("SELECT timestamp FROM recording LIMIT 1").fetchone()
            first = conn.execute("SELECT MIN(timestamp) ts FROM action_event").fetchone()
            last = conn.execute("SELECT MAX(timestamp) ts FROM action_event").fetchone()
        rec_start = rec["timestamp"] if rec else None
        first_ts = first["ts"] if first else None
        last_ts = last["ts"] if last else None
        if first_ts is None:
            return (rec_start or 0.0, chunk_dur if chunk_dur > 0 else 900.0)
        if chunk_dur <= 0:
            chunk_dur = (last_ts - first_ts) / max(n_chunks, 1) if last_ts else 900.0
        base_ts = min(rec_start, first_ts) if rec_start is not None else first_ts
        return (base_ts, chunk_dur)
    except Exception:  # noqa: BLE001
        return (0.0, chunk_dur if chunk_dur > 0 else 900.0)


def _null_console() -> Any:
    from rich.console import Console

    return Console(quiet=True)


# ---------------------------------------------------------------------------
# The entry point — flock FIRST, then ledger-driven reconcile / route / finalize.
# ---------------------------------------------------------------------------


def run_terminal_stage(
    recording_dir: Path,
    *,
    console: "Console | None" = None,
    force: bool = False,
    dry_run: bool = False,
    non_blocking: bool = False,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    _remote_exists: Callable[[int], bool] | None = None,
    _on_locked: Callable[[], None] | None = None,
) -> TerminalResult:
    """Run the disk-driven terminal stage for one recording, behind the flock.

    The lock is acquired FIRST (before ANY ledger read / reconcile). Routing
    reads the frozen ``ResolvedPolicy``; reconcile / gating / finalize read the
    U1 ledger; the sentinel is the last write, gated on the frozen
    ``chunks_expected``.

    Args:
        recording_dir: the recording's *source* directory (where
            ``recording.db`` + chunks live).
        console: rich console for user output (``None`` → quiet).
        force: re-run even if already uploaded (rebuild scrubbed copy).
        dry_run: route + report without scrubbing/uploading/sentinel.
        non_blocking: if True, raise ``TerminalStageBusy`` immediately when the
            lock is contended instead of waiting (the "loser skips" mode).
        lock_timeout: blocking-with-timeout bound (ignored if ``non_blocking``).
        _remote_exists: test/eviction seam — a callback ``(idx) -> bool`` used
            in place of a real GCS stat.
        _on_locked: test hook invoked immediately after the lock is acquired,
            before any ledger read (used by the AE12 decision-time race test).

    Returns:
        :class:`TerminalResult`.
    """
    recording_dir = Path(recording_dir)
    name = recording_dir.name

    # === STEP 0: flock FIRST. Nothing below runs until we hold it. ===
    with terminal_lock(name, non_blocking=non_blocking, timeout=lock_timeout):
        if _on_locked is not None:
            _on_locked()
        return _run_locked(
            recording_dir,
            console=console,
            force=force,
            dry_run=dry_run,
            remote_exists=_remote_exists,
        )


def _run_locked(
    recording_dir: Path,
    *,
    console: "Console | None",
    force: bool,
    dry_run: bool,
    remote_exists: Callable[[int], bool] | None,
) -> TerminalResult:
    """The critical section — runs only while the terminal flock is held."""
    from screencap.catalog import read_intent_policy
    from screencap.pipeline_policy import Destination

    # --- Resolve the FROZEN routing policy (U3). ---
    policy = read_intent_policy(recording_dir)
    destination = _resolve_destination(recording_dir, policy)
    result = TerminalResult(destination=destination.value)

    ledger = _open_ledger(recording_dir)
    if ledger is not None:
        result.n_expected = ledger.chunks_expected()

    if destination is Destination.LOCAL:
        # local → no scrub, no upload. Mark each chunk LOCAL_DONE so the
        # ledger reads "complete" without being "uploaded" (R7: local
        # artifacts stay rich and unscrubbed). AE1 holds by construction —
        # we never produce a scrubbed copy for a local recording.
        _route_local(ledger, result)
        result.routed = True
        # Retention is UNIVERSAL (R11): a local recording with a size/time cap
        # also evicts its LOCAL_DONE chunks. keep_forever (the default) is a
        # no-op. No remote precondition for local eviction.
        _apply_retention(recording_dir, policy, ledger, result, remote_exists=remote_exists)
        return result

    # --- cloud / both routing ---
    if dry_run:
        result.routed = True
        return result

    return _route_cloud(
        recording_dir,
        ledger=ledger,
        console=console,
        force=force,
        result=result,
        remote_exists=remote_exists,
        policy=policy,
    )


def _resolve_destination(recording_dir: Path, policy: "ResolvedPolicy | None"):
    """Resolve the routing destination, falling back for legacy intents.

    Prefers the FROZEN ``ResolvedPolicy.destination`` (U3). When the policy is
    absent (legacy ``version: 1`` intent, or no intent file) we read the bare
    ``destination`` string; a fully-absent intent defaults to ``local`` (the
    conservative, no-upload, no-account branch — R16).
    """
    from screencap.pipeline_policy import Destination

    if policy is not None:
        return policy.destination
    from screencap.catalog import read_intent

    raw = read_intent(recording_dir)
    if raw is None:
        return Destination.LOCAL
    try:
        return Destination(raw)
    except ValueError:
        return Destination.LOCAL


def _open_ledger(recording_dir: Path) -> "PipelineLedger | None":
    """Open the U1 ledger over ``recording.db``, or ``None`` for legacy dirs.

    A missing ``recording.db`` (legacy single-file recording) or a DB with no
    recording row yields ``None`` — the terminal stage still routes/uploads via
    the whole-dir scrub path; only the per-chunk ledger bookkeeping is skipped.
    """
    db_path = recording_dir / "recording.db"
    if not db_path.exists():
        return None
    try:
        from screencap.pipeline_state import (
            PipelineLedger,
            ensure_pipeline_state_schema,
        )

        ensure_pipeline_state_schema(db_path)
        return PipelineLedger(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("terminal_stage: ledger unavailable (%s); proceeding without it", exc)
        return None


def _open_ledger_readonly(recording_dir: Path) -> "PipelineLedger | None":
    """Open the U1 ledger WITHOUT migrating the schema (read-only detection).

    Used by promotion hole detection: unlike :func:`_open_ledger` it does NOT
    call ``ensure_pipeline_state_schema`` (which would ALTER ``recording.db`` —
    adding the ``pipeline_chunk_state`` table + ``chunks_expected`` column —
    and so change the source dir's content hash, defeating the scrubbed-copy
    reuse check on the upload path). Returns ``None`` when ``recording.db`` is
    absent, the ledger table does not yet exist, or the recording row is
    unreadable — all of which mean "no closed chunk set to gate a promotion on"
    (legacy / not-yet-seeded), so detection correctly treats it as no-holes
    and lets the whole-dir scrub branch handle it (R14).
    """
    db_path = recording_dir / "recording.db"
    if not db_path.exists():
        return None
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='pipeline_chunk_state'"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None  # ledger never seeded — not a per-chunk recording yet.
        from screencap.pipeline_state import PipelineLedger

        return PipelineLedger(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "terminal_stage: read-only ledger unavailable (%s); treating as no-ledger",
            exc,
        )
        return None


def _route_local(ledger: "PipelineLedger | None", result: TerminalResult) -> None:
    """Mark every seeded chunk LOCAL_DONE (no scrub, no upload)."""
    if ledger is None:
        return
    from screencap.pipeline_state import Lifecycle

    for row in ledger.all_chunks():
        if row.lifecycle in (Lifecycle.LOCAL_DONE, Lifecycle.EVICTED):
            result.n_local_done += 1
            continue
        with contextlib.suppress(Exception):
            ledger.mark_local_done(row.chunk_index)
            result.n_local_done += 1


def _route_cloud(
    recording_dir: Path,
    *,
    ledger: "PipelineLedger | None",
    console: "Console | None",
    force: bool,
    result: TerminalResult,
    remote_exists: Callable[[int], bool] | None,
    policy: "ResolvedPolicy | None" = None,
) -> TerminalResult:
    """Produce the cloud copy, reconcile, upload, gate the sentinel.

    Order (all inside the held flock):

    1. **Reconcile from disk FIRST** — re-stat not-yet-UPLOADED chunks so a
       half-finished prior run's confirmed chunks are recognized before we do
       any work (no re-upload).
    2. Produce the scrubbed/masked ``<name>-scrubbed`` cloud copy (scrub seam
       adapter); fail-closed chunks block the sentinel.
    3. Upload the scrubbed copy's artifacts (never ``recording.db`` — U2).
    4. Mark each uploaded chunk UPLOADED in the ledger (closed-set gate).
    5. Sentinel LAST, gated on the FROZEN ``chunks_expected`` all UPLOADED.
    """
    from screencap.upload import upload_recording

    name = recording_dir.name

    # 1. Reconcile from disk before doing work (R9). Confirmed-in-GCS chunks
    # flip to UPLOADED so we never re-upload them.
    if ledger is not None:
        result.reconciled = _reconcile_ledger_against_gcs(
            recording_dir, ledger, remote_exists=remote_exists,
        )

    # 2. Produce the cloud copy via the scrub seam adapter.
    producer = CloudCopyProducer(recording_dir, console=console)
    try:
        copy = producer.produce(ledger=ledger, force=force)
    except Exception as exc:  # noqa: BLE001
        logger.error("terminal_stage: cloud-copy production failed for %s: %s", name, exc)
        result.upload_warning = f"scrub/mask failed: {exc}"
        result.failed_indices = _ledger_failed_indices(ledger)
        return result

    result.failed_indices = sorted(set(copy.failed_chunks) | set(_ledger_failed_indices(ledger)))

    # Fail-closed: any FAILED chunk (scrub/mask) blocks the sentinel and the
    # stub. Local media is preserved (we never reach stub_recording).
    if copy.failed_chunks:
        result.upload_warning = (
            f"{len(copy.failed_chunks)} chunk(s) failed cloud masking — "
            "sentinel withheld, local media preserved"
        )
        # We still attempt to upload the chunks that DID succeed (resumable),
        # but the sentinel gate below will not be satisfied.

    # 3. Upload the scrubbed copy's artifacts. ``upload_recording`` enumerates
    # via ``list_recording_files`` which already excludes recording.db + WAL
    # sidecars + *.scrub_failed (U2) — the raw DB never enters the set (AE4).
    try:
        upload_result = upload_recording(copy.scrubbed_dir, force=force)
    except Exception as exc:  # noqa: BLE001
        logger.error("terminal_stage: upload failed for %s: %s", name, exc)
        result.upload_warning = f"upload failed: {exc}"
        return result

    result.routed = True
    uploaded_ok = not upload_result.failed

    # 4. Map upload onto the ledger per chunk (closed-set). A chunk is UPLOADED
    # only when its core files confirmed in GCS; otherwise it stays
    # PENDING/FAILED and blocks the gate.
    if ledger is not None and uploaded_ok:
        _mark_uploaded_chunks(
            recording_dir, ledger,
            failed_chunks=set(copy.failed_chunks),
            remote_exists=remote_exists,
        )
        _refresh_counts(ledger, result)
        result.all_uploaded = ledger.all_uploaded()
        result.finalize_gate_satisfied = ledger.finalize_gate_satisfied()
    elif ledger is None:
        # Legacy / no-ledger dir: fall back to "upload had no failures" as the
        # gate (the whole-dir scrub path has no closed chunk set).
        result.all_uploaded = uploaded_ok
        result.finalize_gate_satisfied = uploaded_ok

    # 5. Sentinel LAST — gated on the frozen closed set. NEVER trust a
    # pre-existing legacy sentinel (Bug 3): we regenerate from the ledger.
    if not copy.failed_chunks and result.finalize_gate_satisfied:
        result.sentinel_uploaded = _write_sentinel(
            recording_dir, ledger, result,
        )

    # 6. Retention/eviction (U8) — AFTER upload-confirm, per the FROZEN policy.
    # The eviction floor (UPLOADED + fresh remote re-confirm) is enforced inside
    # the executor via the ledger; for `both` the masked cloud copy is evicted
    # immediately post-upload-confirm. Safe to run during recording (only
    # UPLOADED chunks are candidates) — the per-chunk pass that bounds disk on a
    # long cloud session (R12) and the finalize pass are the SAME call. Eviction
    # of a local copy before finalize is safe: the sentinel/finalize gate keys
    # off the frozen ledger count, not on-disk presence.
    _apply_retention(recording_dir, policy, ledger, result, remote_exists=remote_exists)

    return result


def _apply_retention(
    recording_dir: Path,
    policy: "ResolvedPolicy | None",
    ledger: "PipelineLedger | None",
    result: TerminalResult,
    *,
    remote_exists: Callable[[int], bool] | None,
) -> None:
    """Invoke the U8 eviction executor for this recording, behind our flock.

    A thin seam onto :func:`screencap.retention.evict_recording`. We are already
    inside the per-recording terminal flock, so eviction cannot race a
    concurrent terminal run reading the same chunk (AE12 / the U10 read-vs-evict
    serialization). The executor enforces the never-delete-un-uploaded /
    fresh-remote-confirm floor; we only surface its report onto ``result``. Any
    error is non-fatal — a failed eviction never compromises an already-uploaded
    recording (it just leaves more local files than retention wanted).
    """
    if ledger is None:
        return
    try:
        from screencap.retention import evict_recording

        report = evict_recording(
            recording_dir,
            policy=policy,
            ledger=ledger,
            remote_exists=remote_exists,
            during_recording=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("terminal_stage: retention pass failed for %s: %s",
                       recording_dir.name, exc)
        return
    result.evicted = sorted(set(report.evicted_indices) | set(report.evict_pending_resumed))
    result.masked_copies_evicted = list(report.masked_copies_evicted)
    result.bytes_freed = report.bytes_freed


# ---------------------------------------------------------------------------
# Ledger-driven reconcile / gating helpers (the disk-driven idempotency core).
# ---------------------------------------------------------------------------


def _reconcile_ledger_against_gcs(
    recording_dir: Path,
    ledger: "PipelineLedger",
    *,
    remote_exists: Callable[[int], bool] | None,
) -> int:
    """Re-stat not-yet-UPLOADED chunks against the server; flip confirmed ones.

    Mirrors ``ChunkProcessor.reconcile_against_gcs`` but reads the LEDGER, not
    in-memory ``_chunk_results``: for every chunk whose ``upload_state`` is
    PENDING or FAILED we ask the server (via ``request_signed_urls`` —
    ``url=None`` ⇒ already there). When the server confirms a chunk's core
    files are all present, we ``mark_uploaded``. PENDING/FAILED only — an
    already-``UPLOADED`` chunk is never re-probed. Returns the flip count.

    This runs FIRST on (re)entry so an interrupted prior run's confirmed chunks
    are recognized before any scrub/upload work — the R9 "converge, don't
    duplicate" contract.
    """
    from screencap.pipeline_state import UploadState

    needs = [
        r.chunk_index
        for r in ledger.all_chunks()
        if r.upload_state in (UploadState.PENDING, UploadState.FAILED)
    ]
    if not needs:
        return 0

    flipped = 0
    for idx in needs:
        if _chunk_confirmed_remote(recording_dir, idx, remote_exists=remote_exists):
            with contextlib.suppress(Exception):
                ledger.mark_uploaded(idx)
                flipped += 1
                logger.info("terminal_stage: reconciled chunk %d (already in GCS)", idx)
    return flipped


def _chunk_confirmed_remote(
    recording_dir: Path,
    idx: int,
    *,
    remote_exists: Callable[[int], bool] | None,
) -> bool:
    """True iff chunk ``idx``'s core files are all confirmed present in GCS.

    Uses the injected ``remote_exists`` callback when supplied (tests /
    eviction); otherwise probes ``request_signed_urls`` and treats an
    all-``url=None`` response as "already there" (the resumability contract).
    A missing/None URL or any exception is conservative → returns False (never
    a false positive — fail-closed per prevention rule #3).
    """
    if remote_exists is not None:
        try:
            return bool(remote_exists(idx))
        except Exception:  # noqa: BLE001
            return False

    from screencap.upload import FileInfo, _content_type, request_signed_urls

    # The cloud copy's scrubbed artifacts live in <name>-scrubbed; that is what
    # was uploaded, so we re-stat those. Probe the chunk's core files only.
    scrubbed_dir = recording_dir.parent / f"{recording_dir.name}-scrubbed"
    base = scrubbed_dir if scrubbed_dir.exists() else recording_dir
    core_names = [
        f"chunk_{idx:04d}.mp4",
        f"audio_{idx:04d}.flac",
        f"events_{idx:04d}.jsonl",
        f"chunk_{idx:04d}_manifest.json",
    ]
    infos: list[FileInfo] = []
    for nm in core_names:
        p = base / nm
        if p.exists() and p.stat().st_size > 0:
            infos.append(FileInfo(nm, p, _content_type(p), p.stat().st_size))
    if not infos:
        return False
    # Derive the GCS recording key EXACTLY as ``upload_recording`` does — from
    # ``base/.recording_id`` (the original id, copied into <name>-scrubbed),
    # NOT ``base.name`` (which would be "<name>-scrubbed" and probe the wrong
    # prefix, so a confirmed chunk would never be recognized and a cloud
    # recording would never finalize). Tests inject ``remote_exists`` so this
    # mismatch is only reachable on the real-GCS path.
    id_file = base / ".recording_id"
    recording_name = id_file.read_text().strip() if id_file.exists() else base.name
    try:
        urls, _ = request_signed_urls(recording_name, infos)
    except Exception:  # noqa: BLE001
        return False
    return all(fi.name in urls and urls[fi.name] is None for fi in infos)


def _mark_uploaded_chunks(
    recording_dir: Path,
    ledger: "PipelineLedger",
    *,
    failed_chunks: set[int],
    remote_exists: Callable[[int], bool] | None,
) -> None:
    """Advance each successfully-uploaded chunk to UPLOADED in the ledger.

    A chunk in ``failed_chunks`` (U6 mask fail-closed) is left FAILED. Every
    other seeded chunk whose core files we can confirm present is marked
    UPLOADED. Confirmation is via the same ``_chunk_confirmed_remote`` probe so
    "uploaded" always reflects remote evidence, never optimism.
    """
    from screencap.pipeline_state import Lifecycle, UploadState

    for row in ledger.all_chunks():
        idx = row.chunk_index
        if idx in failed_chunks:
            continue
        if row.upload_state == UploadState.UPLOADED:
            continue
        if row.lifecycle in (Lifecycle.LOCAL_DONE, Lifecycle.SKIPPED):
            continue
        if _chunk_confirmed_remote(recording_dir, idx, remote_exists=remote_exists):
            with contextlib.suppress(Exception):
                ledger.mark_uploaded(idx)


def _refresh_counts(ledger: "PipelineLedger", result: TerminalResult) -> None:
    from screencap.pipeline_state import Lifecycle, UploadState

    rows = ledger.all_chunks()
    result.n_uploaded = sum(1 for r in rows if r.upload_state == UploadState.UPLOADED)
    result.n_failed = sum(1 for r in rows if r.upload_state == UploadState.FAILED)
    result.n_skipped = sum(1 for r in rows if r.upload_state == UploadState.SKIPPED)
    result.n_local_done = sum(1 for r in rows if r.lifecycle == Lifecycle.LOCAL_DONE)


def _ledger_failed_indices(ledger: "PipelineLedger | None") -> list[int]:
    if ledger is None:
        return []
    from screencap.pipeline_state import UploadState

    return [r.chunk_index for r in ledger.all_chunks() if r.upload_state == UploadState.FAILED]


def _write_sentinel(
    recording_dir: Path,
    ledger: "PipelineLedger | None",
    result: TerminalResult,
) -> bool:
    """Write + upload the completeness sentinel — the LAST write (rule #5).

    Gated on ``ledger.finalize_gate_satisfied()`` (the frozen ``chunks_expected``
    all UPLOADED) by the caller. We NEVER reuse a pre-existing legacy
    ``recording_complete.json`` (Bug 3): ``chunks_expected`` is taken from the
    FROZEN ledger count, not a live glob, and ``upload_sentinel`` overwrites any
    stale local file.
    """
    from screencap.chunk_processor import upload_sentinel

    # chunks_expected from the FROZEN ledger; fall back to a manifest glob only
    # for legacy/no-ledger dirs (where there is no frozen count).
    if ledger is not None and ledger.chunks_expected() is not None:
        chunks_expected = ledger.chunks_expected()
    else:
        chunks_expected = len(list(recording_dir.glob("chunk_*_manifest.json")))

    show_on_website = _read_show_on_website(recording_dir)
    rec_name = _read_recording_name(recording_dir)
    try:
        return upload_sentinel(
            recording_dir, rec_name,
            stop_reason="terminal_stage",
            chunks_expected=chunks_expected or 0,
            show_on_website=show_on_website,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("terminal_stage: sentinel upload failed: %s", exc)
        return False


def _read_show_on_website(recording_dir: Path) -> bool:
    import json

    intent_path = recording_dir / ".recording_intent"
    if not intent_path.exists():
        return True
    try:
        return bool(json.loads(intent_path.read_text()).get("show_on_website", True))
    except Exception:  # noqa: BLE001
        return True


def _read_recording_name(recording_dir: Path) -> str:
    rid = recording_dir / ".recording_id"
    if rid.exists():
        with contextlib.suppress(OSError):
            return rid.read_text().strip() or recording_dir.name
    return recording_dir.name


# ---------------------------------------------------------------------------
# U9 — local->cloud promotion hole detection (AE8).
#
# Promoting a recording whose terminal stage already ran (and may have evicted
# chunks under a local retention policy) must NEVER upload a recording with
# silent holes. A "hole" is a chunk REQUIRED for a complete upload whose local
# media is gone AND that is not confirmed present in GCS. Detect holes via the
# ledger's frozen `chunks_expected` (the closed set, never shrunk by eviction);
# refuse on any hole rather than reproducing survivorship-bias data loss across
# the local/cloud boundary.
# ---------------------------------------------------------------------------


_PROMOTION_CORE_GLOBS = ("chunk_{idx:04d}.mp4",)


def _chunk_local_media_present(recording_dir: Path, idx: int) -> bool:
    """True iff chunk ``idx``'s primary local media (the video) is on disk.

    The video chunk is the irreplaceable artifact eviction reclaims; a missing
    ``chunk_NNNN.mp4`` means the rich local copy is gone. Manifests/events can
    be regenerated from ``recording.db``, but the source media cannot — so the
    video's presence is the load-bearing signal for "can we still upload this
    chunk locally?".
    """
    return (recording_dir / f"chunk_{idx:04d}.mp4").exists()


def detect_promotion_holes(
    recording_dir: Path | str,
    *,
    remote_exists: Callable[[int], bool] | None = None,
) -> list[int]:
    """Return the chunk indices that are HOLES for a local->cloud promotion (AE8).

    A chunk is a hole iff BOTH:

    * its local media (``chunk_NNNN.mp4``) is NOT on disk (evicted, or otherwise
      missing), AND
    * a FRESH remote re-confirm says it is NOT present in GCS.

    The required set is the ledger's frozen ``chunks_expected`` (``0..N-1``) —
    the closed set that eviction never shrinks (so an evicted chunk still
    counts as required). A chunk whose media is present locally is fine
    (upload re-scrubs it); an evicted chunk that IS in GCS is fine (the cloud
    copy survives, the normal delete-after-upload case). Only a chunk that is
    BOTH gone locally AND absent remotely is an unrecoverable hole.

    Detection keys off on-disk presence + a fresh remote re-stat, NOT only the
    ledger's ``EVICTED`` state — so a chunk whose media vanished WITHOUT a
    ledger eviction record (manual delete, partial copy) is still caught
    (defense-in-depth). Conservative on the remote side: ``remote_exists`` that
    raises (GCS unreachable) is treated as "not present", so an unverifiable
    evicted chunk is a hole and the promotion is refused — never optimistically
    promoted.

    Returns an empty list when there are no holes (the recording is promotable;
    re-run the terminal stage on the surviving chunks). A legacy single-file /
    no-ledger recording has no frozen chunk set, so it returns ``[]`` (it is
    handled by the whole-dir scrub branch, R14 — not a per-chunk promotion).
    """
    recording_dir = Path(recording_dir)
    # Read-only: detection must NOT mutate the source recording.db schema (that
    # would change its content hash and defeat the scrubbed-copy reuse check on
    # the upload path). Only open the ledger when its table already exists.
    ledger = _open_ledger_readonly(recording_dir)
    if ledger is None:
        return []  # legacy / no ledger — not a per-chunk promotion (R14).
    expected = ledger.chunks_expected()
    if not expected:  # None or 0 — no closed chunk set to gate on.
        return []

    confirm = _make_promotion_confirm(recording_dir, remote_exists)
    holes: list[int] = []
    for idx in range(expected):
        if _chunk_local_media_present(recording_dir, idx):
            continue  # rich local copy survives — upload can re-scrub it.
        if confirm(idx):
            continue  # cloud copy survives — no hole.
        holes.append(idx)
    return holes


def assert_promotable_to_cloud(
    recording_dir: Path | str,
    *,
    remote_exists: Callable[[int], bool] | None = None,
) -> None:
    """Raise :class:`PromotionRefused` if promoting this recording has holes (AE8).

    The guard for the ``screencap upload`` promotion path: it reconciles holes
    via :func:`detect_promotion_holes` and refuses with a clear, actionable
    message naming the missing chunks and the recording — never producing a
    partial cloud copy. A no-op (clean return) means every required chunk is
    present locally or confirmed in GCS, so the caller may re-run the terminal
    stage on the surviving chunks.
    """
    recording_dir = Path(recording_dir)
    holes = detect_promotion_holes(recording_dir, remote_exists=remote_exists)
    if not holes:
        return
    name = recording_dir.name
    missing = ", ".join(str(i) for i in holes)
    raise PromotionRefused(
        f"Cannot promote {name!r} to the cloud: chunk(s) {missing} were evicted "
        "locally and are not present in the cloud, so a complete upload is "
        "impossible. Refusing to upload a recording with missing chunks "
        "(it would be a partial copy with silent gaps). If you have a local "
        "backup of the missing chunk media, restore it before promoting."
    )


def _make_promotion_confirm(
    recording_dir: Path,
    remote_exists: Callable[[int], bool] | None,
) -> Callable[[int], bool]:
    """Fresh-remote-confirm callback for promotion hole detection (fail-closed).

    Injected ``remote_exists`` is used but any exception is swallowed to a
    conservative ``False`` (GCS unreachable -> treat the evicted chunk as a
    hole, refuse the promotion). ``None`` reuses :func:`_chunk_confirmed_remote`
    (the same seam reconcile/eviction use).
    """
    if remote_exists is not None:
        def _confirm_injected(idx: int) -> bool:
            try:
                return bool(remote_exists(idx))
            except Exception:  # noqa: BLE001 — unreachable -> conservative hole
                return False
        return _confirm_injected

    def _confirm_real(idx: int) -> bool:
        return _chunk_confirmed_remote(recording_dir, idx, remote_exists=None)

    return _confirm_real
