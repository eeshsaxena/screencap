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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from rich.console import Console

    from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
    from screencap.pipeline_state import PipelineLedger
    from screencap.segmentation.provider import SegmentResult
    from screencap.upload import FileInfo

logger = logging.getLogger(__name__)

__all__ = [
    "TerminalStageBusy",
    "TerminalStageInterrupted",
    "TerminalResult",
    "AccountMismatch",
    "CloudCopyProducer",
    "PromotionRefused",
    "terminal_lock",
    "run_terminal_stage",
    "run_incremental_segmentation",
    "detect_promotion_holes",
    "assert_promotable_to_cloud",
]

# Per-recording terminal-stage lock lives under the daemon run dir (mode 0700,
# created here if needed), so it is never inside a recording dir the uploader
# enumerates. Kernel auto-releases the flock on process death (same property
# pidfile.py relies on) — no stale-lock cleanup needed.
# RUN-DIR boundary (SCR-236 R3): stays OUTSIDE the at-rest container as plaintext
# live-process state. Do NOT route through config.get_data_root().
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


class TerminalStageInterrupted(RuntimeError):
    """A cooperative ``stop_event`` halted the terminal stage at a safe boundary.

    Raised by :func:`run_terminal_stage` (SCR-258 U9) when the caller-supplied
    ``stop_event`` is set — checked ONLY BETWEEN per-chunk ledger transitions, so
    the halt always lands at a ledger-safe boundary (the ``PipelineLedger``'s
    crash-safe ordering is what makes the interrupted cycle resumable). This is
    the quiescence engine's seam: a ``storage.lock`` sets the event so an in-flight
    resume stops promptly and the store can seal within the grace budget WITHOUT
    waiting for a slow upload to finish; the recording resumes on unlock (AE7).
    Never leaves a partial ledger transition — no chunk is marked UPLOADED that was
    not actually confirmed, and no sentinel is written on this path.
    """


def _check_stop(stop_event: "threading.Event | None") -> None:
    """Raise :class:`TerminalStageInterrupted` iff ``stop_event`` is set (U9).

    Call ONLY at ledger-safe boundaries (before a phase, between per-chunk
    transitions) — never mid-transition — so the halt is always resumable.
    """
    if stop_event is not None and stop_event.is_set():
        raise TerminalStageInterrupted(
            "terminal stage halted at a ledger-safe boundary by stop_event "
            "(store lock); the recording resumes on unlock"
        )


@dataclass(frozen=True)
class AccountMismatch:
    """Structured account-ownership-mismatch signal (SCR-171).

    Set on :class:`TerminalResult` by the cloud-routing account gate when a
    recording's pinned ``owner_uid`` differs from the currently signed-in uid.
    This is the *typed discriminator* the daemon lifts onto the ``/v0/events``
    bus as an ``account_mismatch`` event — distinct from the free-text
    ``upload_warning`` (which is also set by scrub/upload failures, so it can
    NOT drive a specific event). Both uids are gate-authoritative for the
    comparison that fired; the daemon enriches with ``whoami`` email/stale at
    emit time.
    """

    owner_uid: str
    signed_in_uid: str


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
    # SCR-127 — count of already-UPLOADED rows the re-validation pass downgraded
    # this run because GCS confirmed their core files absent (informational).
    downgraded: int = 0
    stubbed: bool = False
    upload_warning: str | None = None
    # SCR-171 — typed account-ownership-mismatch signal, set ONLY by the cloud
    # account gate (never by other upload_warning setters). The daemon publishes
    # an ``account_mismatch`` /v0/events event off this field; ``upload_warning``
    # stays set in parallel for back-compat (CLI / shutdown messaging).
    account_mismatch: AccountMismatch | None = None
    failed_indices: list[int] = field(default_factory=list)
    # U8 retention/eviction outcome for this run (informational; the ledger is
    # the source of truth). ``evicted`` = chunk indices whose local rich copy
    # was reclaimed; ``masked_copies_evicted`` = chunk indices whose masked
    # cloud copy (<name>-scrubbed/masked_video/) was reclaimed post-upload.
    evicted: list[int] = field(default_factory=list)
    masked_copies_evicted: list[int] = field(default_factory=list)
    bytes_freed: int = 0
    # U4 — count of named task segments persisted to the LOCAL tasks store this
    # run (0 when the recording is not local, the provider returned no tasks, or
    # segmentation failed open). Informational; the ledger + tasks.json are the
    # source of truth. ``tasks_persisted`` stays 0 on any fail-open path so a
    # caller can tell "segmented, no tasks" apart from "segmented N tasks".
    tasks_persisted: int = 0


# ---------------------------------------------------------------------------
# The per-recording advisory flock — acquired FIRST on every entry point.
# ---------------------------------------------------------------------------


def _lock_path_for(name: str) -> Path:
    return _RUN_DIR / f"terminal-{name}.lock"


# In-process per-recording locks. flock (below) serializes across PROCESSES but
# degrades to unlocked on flock-unsupported filesystems (SCR-124); these locks
# serialize THREADS within one process regardless of the filesystem, closing the
# most likely race (a daemon finalize vs. a manual upload vs. the viewer concat,
# all in one process) even when flock no-ops.
_INPROC_LOCKS_GUARD = threading.Lock()
_INPROC_LOCKS: dict[str, threading.Lock] = {}


def _inproc_lock_for(name: str) -> threading.Lock:
    """Return the process-wide lock for recording ``name`` (created on demand)."""
    with _INPROC_LOCKS_GUARD:
        lk = _INPROC_LOCKS.get(name)
        if lk is None:
            lk = threading.Lock()
            _INPROC_LOCKS[name] = lk
        return lk


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

    **In-process serialization (SCR-124).** flock serializes across PROCESSES
    but degrades to unlocked on flock-unsupported filesystems (NFS/SMB/some
    sandbox mounts). To keep the most likely race — two THREADS in one process
    racing the destructive rmtree+copytree+upload — serialized even on those
    mounts, a per-name in-process lock is acquired FIRST and held across the
    whole critical section. Cross-process serialization on a no-flock mount
    remains a documented gap.
    """
    # In-process lock FIRST (covers same-process threads even when flock no-ops).
    inproc = _inproc_lock_for(name)
    if non_blocking:
        if not inproc.acquire(blocking=False):
            raise TerminalStageBusy(
                f"terminal stage for {name!r} is already running in this "
                "process (non-blocking)"
            )
    elif not inproc.acquire(blocking=True, timeout=max(0.0, timeout)):
        raise TerminalStageBusy(
            f"terminal stage for {name!r} still busy after {timeout:.0f}s "
            "(in-process lock)"
        )
    try:
        try:
            _RUN_DIR.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(_RUN_DIR, 0o700)
        except OSError:
            # Sandboxed / read-only run dir — degrade to no cross-process flock
            # (the in-process lock is still held), mirroring
            # scrubber.recording_scrub_lock's best-effort posture. Log loudly so
            # the lost cross-process serialization is visible.
            logger.warning(
                "terminal_lock: could not create run dir %s; proceeding without "
                "the cross-process flock for %s (in-process lock still held; "
                "cross-process runs are NOT serialized)",
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
                    # NFS / virtual-FS flock unsupported (EOPNOTSUPP/EINVAL): a
                    # terminal run must not be permanently un-runnable on such a
                    # mount. Degrade to the in-process lock only (loud warning).
                    if exc.errno in (errno.EOPNOTSUPP, errno.EINVAL, errno.ENOLCK):
                        logger.warning(
                            "terminal_lock: flock unsupported on this filesystem "
                            "(%s); proceeding with the in-process lock only for "
                            "%s (cross-process runs NOT serialized)",
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
    finally:
        inproc.release()


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
            console.print(
                f"  Reusing reviewed scrubbed copy at "
                f"[dim]{scrubbed_dir.name}/[/dim] (reviewed == uploaded)."
            )
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
        """Apply U6 video masking per chunk via the SHARED seam (SCR-125 U1).

        Gated on the FROZEN per-recording masked-video-upload decision (not the
        mutable global), so the terminal stage and the live ``chunk_processor``
        mask identically for a given recording. Flag OFF (today's default): no
        masked copy — the capture-blocked source chunk IS the cloud copy, no
        ledger scrub-state change (the agnostic STAGED state + the upload
        confirmation drives the gate). Flag ON: each chunk goes through the same
        :func:`pipeline_chunk_ops.mask_chunk_for_cloud` body the live path uses
        — ok -> ``mark_scrubbed`` (recorded in ``masked_chunks``), FAILED ->
        ``mark_failed`` (recorded in ``failed_chunks``, blocking the sentinel).
        """
        from screencap.pipeline_chunk_ops import (
            get_frozen_masked_video_upload,
            mask_chunk_for_cloud,
        )
        from screencap.recovery import derive_chunk_grid
        from screencap.scrubber import (
            is_masked_video_reusable,
            purge_orphan_masked_videos,
            write_masked_provenance,
        )

        if not get_frozen_masked_video_upload(self._recording_dir):
            return

        db_path = self._recording_dir / "recording.db"
        chunk_videos = sorted(self._recording_dir.glob("chunk_*.mp4"))
        expected: set[int] = set()
        for vf in chunk_videos:
            try:
                expected.add(int(vf.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue

        # SCR-126 Fix 3 — AUTHORITATIVE closed-set reconcile FIRST: purge any
        # masked_video/chunk_*.mp4 whose source chunk no longer exists (an orphan
        # the per-chunk pass never visits — e.g. a source evicted since the last
        # pass). This MUST run before the reuse-skip below: the reuse check only
        # validates the EXPECTED set, so an orphan would otherwise survive a skipped
        # produce and reach the rglob upload.
        purge_orphan_masked_videos(scrubbed_dir, expected_indices=expected)

        # SCR-126 Fix 3 / R4 — reuse skip: when every expected chunk already has a
        # masked copy provably current for ALL masking inputs (source bytes,
        # geometry, policy, pixel_ratio, mask-logic version), the re-encode is
        # skipped (reviewed == uploaded). Provenance + ledger scrub-state persist
        # from the pass that wrote them.
        if is_masked_video_reusable(
            self._recording_dir, scrubbed_dir, expected_indices=expected
        ):
            outcome.masked_chunks.extend(sorted(expected))
            return

        # SCR-126 Fix 2 / R3: each chunk's absolute ORIGIN is base_ts + idx*chunk_dur
        # keyed on the FILENAME index (correct for non-contiguous indices, e.g. after
        # per-index eviction), from the SHARED grid helper recovery also uses — so
        # the masked video and recovery's manifests/events cannot drift. The masker
        # derives each chunk's END from its own decoded PTS extent (advisory here).
        grid = derive_chunk_grid(db_path, len(chunk_videos))
        if grid is not None:
            base_ts, chunk_dur, _last_ts = grid
        else:
            # No events / read error: origin 0.0 makes the masker fail closed on the
            # epoch span (no geometry there) — never an over-wide clear span.
            from screencap.config import get_chunk_duration

            base_ts, chunk_dur = 0.0, get_chunk_duration()
        for vf in chunk_videos:
            try:
                idx = int(vf.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            start_ts = base_ts + idx * chunk_dur
            end_ts = start_ts + chunk_dur
            cls = mask_chunk_for_cloud(
                self._recording_dir, scrubbed_dir, idx,
                start_ts=start_ts, end_ts=end_ts,
                enabled=True,  # already gated on the frozen value above
                ledger=ledger, db_path=db_path,
            )
            if cls.masked:
                outcome.masked_chunks.append(idx)
            elif cls.failed:
                outcome.failed_chunks.append(idx)

        # Record provenance over the successfully-masked set so the reuse +
        # convergence-fast-path gates can trust the copies on the next entry.
        write_masked_provenance(
            self._recording_dir, scrubbed_dir, masked_indices=outcome.masked_chunks,
        )


def _null_console() -> Any:
    from rich.console import Console

    return Console(quiet=True)


def _append_warning(existing: str | None, fragment: str) -> str:
    """Join a new warning fragment onto an existing one (`; `-separated), or return it alone."""
    return f"{existing}; {fragment}" if existing else fragment


def _notify_progress(
    on_progress: Callable[[str], None] | None, phase: str
) -> None:
    """Best-effort progress ping for the interactive upload watchdog (SCR-175).

    The terminal stage's pre-upload prep — the contended-lock handoff, the GCS
    reconcile, and the scrub/mask ``produce()`` — is SILENT: no stderr event
    fires before ``upload_started`` at the transfer step. The Swift
    ``UploadController`` arms a single 120s inactivity watchdog reset ONLY on a
    parsed event, so a contended-then-released lock can leave too little of that
    one budget for the silent converge and re-trip the watchdog as a false
    ``.failed("upload timed out")`` (SCR-165 shortened the lock wait but left
    this gap). Calling this at the prep boundaries lets the interactive CLI emit
    an ``upload_preparing`` event so the watchdog sees the prep as activity.

    ``None`` on the daemon / live-finalize callers (no watcher). Swallows
    everything: a progress ping must NEVER perturb the terminal stage's outcome.
    """
    if on_progress is None:
        return
    try:
        on_progress(phase)
    except Exception:  # noqa: BLE001 — a progress ping must not affect convergence
        logger.debug("terminal_stage: on_progress(%s) raised; ignored", phase, exc_info=True)


def _masked_convergence_ok(recording_dir: Path) -> bool:
    """SCR-126 R8 gate for the convergence fast path.

    Returns True when the fast path may declare 'done' WITHOUT running ``produce``:
    either the frozen ``masked_video_upload`` flag is OFF (no masked cloud video is
    involved — today's default), or it is ON and a masked-video provenance record
    exists at the CURRENT ``MASK_PROVENANCE_VERSION``. A recording converged under
    older mask logic returns False → the caller falls through to ``produce`` and
    re-masks rather than shipping the stale cloud copy."""
    from screencap.pipeline_chunk_ops import get_frozen_masked_video_upload

    if not get_frozen_masked_video_upload(recording_dir):
        return True
    from screencap.scrubber import masked_provenance_version_current

    scrubbed_dir = recording_dir.parent / f"{recording_dir.name}-scrubbed"
    return masked_provenance_version_current(scrubbed_dir)


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
    force_destination: "Destination | str | None" = None,
    retention_override: "RetentionPolicy | str | None" = None,
    on_progress: Callable[[str], None] | None = None,
    stop_event: "threading.Event | None" = None,
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
        force_destination: SCR-125 U3 explicit promotion — override the frozen
            routed destination (e.g. ``cloud`` to promote a ``local``-intent /
            legacy recording that would otherwise route LOCAL → no-op). ``None``
            routes by the frozen ``.recording_intent`` (unchanged).
        retention_override: SCR-125 U3 per-run retention override (e.g.
            ``keep_forever`` for ``screencap upload --no-delete``). ``None`` uses
            the frozen policy's retention.
        on_progress: SCR-175 prep-phase heartbeat — a callback ``(phase) -> None``
            invoked at the lock-acquired boundary (``"locked"``), per reconcile
            probe (``"reconcile"``), and right before the scrub/mask
            ``produce()`` (``"scrub"``). The interactive ``screencap upload``
            wires it to an ``upload_preparing`` stderr event so the Swift
            inactivity watchdog sees the otherwise-silent prep as activity.
            ``None`` (the default) on the daemon / live-finalize callers — no
            watcher there; best-effort and never affects the outcome.
        _remote_exists: test/eviction seam — a callback ``(idx) -> bool`` used
            in place of a real GCS stat.
        stop_event: SCR-258 U9 cooperative quiescence seam — a ``threading.Event``
            checked ONLY BETWEEN per-chunk ledger transitions (never mid-
            transition). When set, the stage raises :class:`TerminalStageInterrupted`
            at the next ledger-safe boundary so a ``storage.lock`` can seal within
            its grace budget without waiting for a slow upload; the recording
            resumes on unlock (AE7). ``None`` (the default) on every existing caller
            — byte-identical to today. Mirrors ``backfill_job._stop_event``.
        _on_locked: test hook invoked immediately after the lock is acquired,
            before any ledger read (used by the AE12 decision-time race test).

    Returns:
        :class:`TerminalResult`.

    Raises:
        PromotionRefused: when the cloud route detects a HOLE (a chunk required
            for a complete upload whose local media is gone AND is unconfirmable
            in GCS — AE8). Callers (CLI upload, finalize, daemon resume) handle
            it; never a partial cloud copy.
        TerminalStageBusy: when the lock is contended (per ``non_blocking`` /
            ``lock_timeout``).
        TerminalStageInterrupted: when ``stop_event`` is set at a ledger-safe
            boundary (U9 quiescence). The ledger is left consistent and the
            recording resumes on unlock.
    """
    recording_dir = Path(recording_dir)
    name = recording_dir.name

    # Dry-run is a READ-ONLY preview (route + report; no scrub / upload / sentinel
    # / eviction), so it does NOT take the flock — it must never block on, or be
    # blocked by, a concurrent real run.
    if dry_run:
        return _run_locked(
            recording_dir,
            console=console,
            force=force,
            dry_run=True,
            force_destination=force_destination,
            retention_override=retention_override,
            remote_exists=_remote_exists,
        )

    # U9: if the lock already fired before we even acquired the flock, bail at
    # this ledger-safe boundary (nothing written yet).
    _check_stop(stop_event)
    # === STEP 0: flock FIRST. Nothing below runs until we hold it. ===
    with terminal_lock(name, non_blocking=non_blocking, timeout=lock_timeout):
        if _on_locked is not None:
            _on_locked()
        # SCR-175: the lock wait just ended (≤lock_timeout, silent). Ping NOW so a
        # contended-then-released lock resets the interactive watchdog before the
        # equally-silent reconcile + produce begin — they no longer share one
        # budget with the wait.
        _notify_progress(on_progress, "locked")
        return _run_locked(
            recording_dir,
            console=console,
            force=force,
            dry_run=dry_run,
            force_destination=force_destination,
            retention_override=retention_override,
            on_progress=on_progress,
            remote_exists=_remote_exists,
            stop_event=stop_event,
        )


def _run_locked(
    recording_dir: Path,
    *,
    console: "Console | None",
    force: bool,
    dry_run: bool,
    force_destination: "Destination | str | None" = None,
    retention_override: "RetentionPolicy | str | None" = None,
    on_progress: Callable[[str], None] | None = None,
    remote_exists: Callable[[int], bool] | None = None,
    stop_event: "threading.Event | None" = None,
) -> TerminalResult:
    """The critical section — runs only while the terminal flock is held."""
    from screencap.catalog import read_intent_policy
    from screencap.pipeline_policy import Destination

    # --- Resolve the routing policy. ---
    policy = read_intent_policy(recording_dir)
    if force_destination is not None:
        # SCR-125 U3 explicit promotion: the caller overrides the frozen routed
        # destination (e.g. promote a local/legacy recording to cloud). The
        # frozen retention/params are still honored unless retention_override.
        destination = (
            Destination(force_destination)
            if isinstance(force_destination, str)
            else force_destination
        )
    else:
        destination = _resolve_destination(recording_dir, policy)
    result = TerminalResult(destination=destination.value)

    # Dry-run preview: report the routing decision, write NOTHING (no schema
    # migration, no LOCAL_DONE mark, no scrub/upload/sentinel, no eviction). Use
    # the READ-ONLY ledger opener — ``_open_ledger`` migrates the schema (ALTER
    # recording.db), which would mutate the source dir's content hash and defeat
    # the scrubbed-copy reuse check. Universal so a dry-run never touches disk.
    if dry_run:
        ro_ledger = _open_ledger_readonly(recording_dir)
        if ro_ledger is not None:
            result.n_expected = ro_ledger.chunks_expected()
        result.routed = True
        return result

    ledger = _open_ledger(recording_dir)
    if ledger is not None:
        result.n_expected = ledger.chunks_expected()

    # U9: ledger-safe boundary between the (cheap, no-op-safe) schema-ensure above
    # and the routing work below. Bail here if the lock fired.
    _check_stop(stop_event)

    if destination is Destination.LOCAL:
        # local → no scrub, no upload. Mark each chunk LOCAL_DONE so the
        # ledger reads "complete" without being "uploaded" (R7: local
        # artifacts stay rich and unscrubbed). AE1 holds by construction —
        # we never produce a scrubbed copy for a local recording.
        _route_local(ledger, result)
        result.routed = True
        # U4 (R3, R4): run session→named-task segmentation on the Mac for this
        # LOCAL recording and persist the named tasks to the LOCAL-only tasks
        # store (tasks.json + the pipeline_task_segments ledger table). Runs
        # inside the terminal flock, AFTER the routing decision and BEFORE
        # retention. Strictly fail-open: no provider result (None / any error)
        # leaves the recording unnamed but NEVER blocks terminal completion,
        # and never triggers an upload (AE1 — the LOCAL branch has no upload
        # seam and the tasks store is excluded from upload by rule).
        _run_local_segmentation(recording_dir, ledger, result)
        # Retention is UNIVERSAL (R11): a local recording with a size/time cap
        # also evicts its LOCAL_DONE chunks. keep_forever (the default) is a
        # no-op. No remote precondition for local eviction.
        _apply_retention(
            recording_dir, policy, ledger, result,
            remote_exists=remote_exists, retention_override=retention_override,
        )
        return result

    # --- cloud / both routing ---
    return _route_cloud(
        recording_dir,
        ledger=ledger,
        console=console,
        force=force,
        result=result,
        remote_exists=remote_exists,
        policy=policy,
        retention_override=retention_override,
        on_progress=on_progress,
        stop_event=stop_event,
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


_LOCAL_TASKS_FILE = "tasks.json"


def run_incremental_segmentation(
    recording_dir: Path,
    *,
    non_blocking: bool = True,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
) -> TerminalResult:
    """Incrementally segment a LIVE (still-recording) ambient day (U6, R6/R7/KTD4).

    The daemon calls this on a timer for the active ambient recording so today's
    Journal fills as the day progresses, not only when the stream stops (R7). It
    runs the SAME segment→persist body the finalize path runs
    (:func:`_run_local_segmentation`) over the recording's COMPLETED on-disk chunk
    manifests + the live ``recording.db``, so the segmentation rules cannot drift
    between the live and finalize paths. The currently-open chunk has no manifest
    yet, so only completed spans are segmented — exactly R7's "fill for completed
    spans without waiting for the stream to stop".

    Concurrency (KTD4/AE12): acquires the per-recording terminal flock
    NON-BLOCKING by default — if a concurrent terminal-stage FINALIZE (or a manual
    upload) holds it, this pass raises :class:`TerminalStageBusy` and writes
    nothing rather than racing the finalize's own segmentation. The daemon worker
    treats that as "skip this tick".

    Privacy (KTD4/R11): the shared body builds the activity summary with
    ``blocked_source=recording_dir`` so the blocked-interval strip is re-derived
    FAIL-CLOSED over the live, concurrently-written DB — a partial/ambiguous read
    over-skips rather than under-blocks, so a masked interval can never reach the
    provider or a surfaced task name.

    Fail-open on the PROVIDER (R6): a provider miss/error leaves existing tasks
    intact; the fail-closed posture applies ONLY to the R11 blocked-interval
    derivation (privacy), never wiping tasks on a provider problem.

    Returns a :class:`TerminalResult` (``destination='local'``); ``tasks_persisted``
    is the agent-task count written this pass. Raises :class:`TerminalStageBusy`
    when the flock is contended (per ``non_blocking`` / ``lock_timeout``).
    """
    recording_dir = Path(recording_dir)
    name = recording_dir.name
    result = TerminalResult(destination="local")
    # Flock FIRST (AE12), exactly like run_terminal_stage — a concurrent finalize
    # that holds it means this best-effort pass SKIPS (non_blocking) rather than
    # double-writing the agent task set.
    with terminal_lock(name, non_blocking=non_blocking, timeout=lock_timeout):
        ledger = _open_ledger(recording_dir)
        if ledger is not None:
            result.n_expected = ledger.chunks_expected()
        _run_local_segmentation(recording_dir, ledger, result)
        result.routed = True
    return result


def _run_local_segmentation(
    recording_dir: Path,
    ledger: "PipelineLedger | None",
    result: TerminalResult,
) -> None:
    """Segment a LOCAL recording into named tasks; persist to the local store (U4/U7).

    Runs on the Mac inside the terminal flock (R3/R4). Builds the activity
    summary from the recording's LOCAL on-disk chunk artifacts, passing the
    recording dir as ``blocked_source`` so the U3 privacy strip drops
    masked/blocked content BEFORE it reaches any provider (R11 / AE1). Resolves
    the configured provider (``config.get_llm_provider`` → ``get_provider``) and
    calls ``segment``. A returned tasks dict is written to BOTH the recording's
    ``tasks.json`` and the ``pipeline_task_segments`` ledger table (idempotent —
    re-entry REPLACES, never duplicates).

    **Graceful degradation ladder (U7/U5, R5 / R6 / KTD6).** The day-split
    provider's outcome is routed through
    :func:`screencap.segmentation.degrade.resolve_day_split`:

    * a real tasks dict → persisted unchanged;
    * ``PROVIDER_UNAVAILABLE`` (could not run — e.g. no on-device model on a
      CLI-only / pre-macOS-26 install) → the day-split **boundaries** fall back to
      the **local idle-gap heuristic** (``task_manifest._segment_tasks`` over the
      recording's local events). Day-split is on-device/heuristic only — cloud is
      **never** a day-split fallback (R7 over R5, KTD6). BUT the on-device-
      unavailable state ALSO unlocks the consented **SUMMARY cloud fallback** (U5,
      R6/R8): if ``summary_cloud_consent`` is on and a cloud provider is
      configured, the recording is first named/summarized by that BYO cloud
      provider over the SAME already-stripped summary; only if it declines do we
      fall to the mechanically-named idle-gap heuristic;
    * ``None`` (ran, produced nothing) → left unnamed (fail-open); neither cloud
      nor the heuristic is run for a genuine empty result.

    **Strictly fail-open.** An empty/invalid summary or any unexpected error
    leaves the recording unnamed but NEVER blocks terminal completion and NEVER
    uploads — the LOCAL branch has no upload seam at all.
    """
    from screencap.segmentation.consent import ConsentPolicy
    from screencap.segmentation.degrade import DegradeAction, resolve_day_split

    try:
        summary, provider_result = _segment_local_tasks(recording_dir)
    except Exception as exc:  # noqa: BLE001 — segmentation must never block terminal
        logger.debug(
            "terminal_stage: local segmentation failed open for %s (%s)",
            recording_dir.name, exc,
        )
        return

    try:
        decision = resolve_day_split(provider_result, ConsentPolicy.from_config())
    except Exception as exc:  # noqa: BLE001 — the ladder must never block terminal
        logger.debug(
            "terminal_stage: degradation resolve failed open for %s (%s)",
            recording_dir.name, exc,
        )
        return

    if decision.action is DegradeAction.USE_PROVIDER:
        tasks = decision.tasks
    elif decision.action is DegradeAction.HEURISTIC:
        # On-device day-split unavailable. Two independent fallbacks, in order:
        #   1. The consented SUMMARY cloud fallback (U5, R6/R8) — a BYO cloud
        #      provider names the session over the SAME already-stripped summary.
        #      This is the SUMMARY task, resolved independently of DAY_SPLIT; the
        #      never-cloud day-split guard (KTD6) is untouched — the input is the
        #      ALLOW-only text summary, never frames.
        #   2. The local idle-gap heuristic (KTD6) — mechanically-named boundaries
        #      when there is no consented cloud path (or it declines).
        # Both are strictly fail-open and never block terminal / never upload.
        tasks = None
        try:
            tasks = _summary_cloud_fallback(summary)
        except Exception as exc:  # noqa: BLE001 — the cloud fallback must never block
            logger.debug(
                "terminal_stage: summary cloud fallback failed open for %s (%s)",
                recording_dir.name, exc,
            )
            tasks = None
        if not tasks:
            try:
                tasks = _heuristic_local_tasks(recording_dir)
            except Exception as exc:  # noqa: BLE001 — heuristic must never block terminal
                logger.debug(
                    "terminal_stage: idle-gap heuristic failed open for %s (%s)",
                    recording_dir.name, exc,
                )
                return
    else:
        # NONE (provider ran, no tasks) or CLOUD (never reachable for day-split)
        # → fail open, nothing to persist.
        return

    if not tasks:
        return
    # KTD3 carve-out — drop any fresh AGENT task overlapping a PROTECTED span
    # (source='user' OR a user-edited agent row) so that, after the scoped agent
    # replace, no source='agent' span overlaps a protected one (the "agent
    # auto-splits, you curate" invariant, R8). A no-op when there are no protected
    # rows (the common finalize case). This lives in the shared body so the
    # finalize and incremental paths cannot drift on the invariant.
    tasks = _carve_out_protected_spans(ledger, tasks)
    try:
        n = _persist_local_tasks(recording_dir, ledger, tasks)
    except Exception as exc:  # noqa: BLE001 — a persistence failure must not block
        logger.warning(
            "terminal_stage: persisting local tasks failed for %s (%s)",
            recording_dir.name, exc,
        )
        return
    result.tasks_persisted = n


def _carve_out_protected_spans(
    ledger: "PipelineLedger | None",
    tasks: dict,
) -> dict:
    """Drop fresh AGENT tasks overlapping a protected user/edited span (KTD3, R8).

    Reads the protected spans (``source='user'`` OR ``edited=1`` — the disjoint
    HIGH-range rows the scoped agent replace preserves) from the ledger and removes
    any agent task whose ``[start_ts, end_ts)`` overlaps one, so that after the
    scoped replace NO ``source='agent'`` span overlaps a protected span. Dropping
    (rather than trimming) keeps the agent set a clean partition and lets
    :func:`_persist_local_tasks` re-index the survivors contiguously from 0.

    Returns:

    * ``tasks`` unchanged when the ledger is ``None`` or there are no protected
      spans (the common finalize case — byte-identical to the pre-carve-out path);
    * a copy with a filtered ``tasks`` list otherwise. The list may be EMPTY: an
      empty agent set is still persisted (via the scoped replace) so a stale agent
      row left by a prior pass — over a span the user has SINCE marked — is cleared
      rather than surviving to overlap the protected span. Only when the input had
      no agent task list at all is ``tasks`` returned unchanged.

    Never raises: a ledger read error fails open (returns ``tasks`` unchanged) —
    the carve-out is a curation-preservation refinement, not a privacy gate.
    """
    if ledger is None or not isinstance(tasks, dict):
        return tasks
    task_list = tasks.get("tasks")
    if not isinstance(task_list, list) or not task_list:
        return tasks

    from screencap.pipeline_state import spans_overlap, task_row_is_protected

    try:
        rows = ledger.read_task_segments()
    except Exception as exc:  # noqa: BLE001 — carve-out must never block segmentation
        logger.debug(
            "terminal_stage: reading protected spans failed (%s); skipping carve-out",
            exc,
        )
        return tasks
    protected = [
        (r.start_ts, r.end_ts)
        for r in rows
        if task_row_is_protected(r)
    ]
    if not protected:
        return tasks

    def _overlaps_protected(t: dict) -> bool:
        try:
            start = float(t.get("start_ts", 0.0))
            end = float(t.get("end_ts", 0.0))
        except (TypeError, ValueError):
            return False
        # Half-open overlap: [start, end) intersects [p_start, p_end).
        return any(spans_overlap(start, end, p_start, p_end) for p_start, p_end in protected)

    kept = [
        t for t in task_list if isinstance(t, dict) and not _overlaps_protected(t)
    ]
    return {**tasks, "tasks": kept}


def _segment_local_tasks(
    recording_dir: Path,
) -> "tuple[dict | None, SegmentResult]":
    """Build the stripped activity summary and run the configured day-split provider.

    Returns ``(stripped_summary, provider_result)``:

    * ``stripped_summary`` — the ALLOW-only, ``stripped=True``-marked activity
      summary dict (or ``None`` when there is no local activity to summarize). It
      is returned so the caller can reuse the SAME already-stripped input for the
      net-new consented SUMMARY cloud fallback (U5) without rebuilding/re-stripping
      it — the BYO cloud backends fail-close on any summary not marked stripped.
    * ``provider_result`` — the day-split provider's raw
      :class:`~screencap.segmentation.provider.SegmentResult`: a validated tasks
      dict, ``None`` (ran, no usable tasks / no activity), or ``PROVIDER_UNAVAILABLE``
      (could not run). The sentinel is preserved (not collapsed to a falsy ``None``)
      so the caller's degradation ladder can route on the None-vs-unavailable
      distinction.

    All heavy imports are deferred so the terminal-stage import surface stays light.
    """
    from screencap.segmentation.activity_summary import build_activity_summary
    from screencap.segmentation.local_source import (
        LocalActivitySource,
        load_local_manifests,
    )
    from screencap.segmentation.routing import build_day_split_provider

    manifests = load_local_manifests(recording_dir)
    if not manifests:
        return None, None

    summary = build_activity_summary(
        recording_dir.name,
        manifests,
        LocalActivitySource(recording_dir, manifests),
        # U3 privacy strip: pass the recording dir so blocked/masked intervals
        # are re-derived over its local recording.db and stripped ALLOW-only,
        # fail-closed — BEFORE the summary reaches any provider (R11 / AE1).
        blocked_source=recording_dir,
    )
    if summary is None:
        return None, None

    # The summary was built with blocked_source, so build_activity_summary has
    # run the R11 strip and marked the summary stripped AUTHORITATIVELY — the
    # on-device provider's fail-closed gate relies on that builder-set marker, so
    # the caller does not (and must not) forge it here.

    # Build the day-split provider for the configured active provider (U8): an
    # on-device chain (AFM → downloaded), the downloaded backend, or a LOCAL BYO
    # endpoint. A REMOTE BYO endpoint or a cloud provider routes to an
    # always-unavailable provider so day-split degrades to the heuristic and never
    # touches the network (R5). segment() returns a validated tasks dict, None
    # (ran, no usable tasks), or PROVIDER_UNAVAILABLE (could not run — routed to
    # the idle-gap heuristic by the caller's ladder, KTD6).
    provider = build_day_split_provider()
    return summary, provider.segment(summary)


def _summary_cloud_fallback(
    summary: dict | None,
) -> dict | None:
    """Consented SUMMARY cloud fallback over the ALREADY-stripped summary (U5, R6/R8).

    Net-new dispatch: when the on-device day-split provider could not run, a
    recording may still be named/summarized by the user's configured **cloud**
    provider — but ONLY as the consented, on-device-unavailable SUMMARY fallback.
    This is the SUMMARY task (naming the session), resolved independently of the
    DAY_SPLIT task, whose boundaries stay on-device/heuristic (R7 over R5 — a
    guard this path never touches).

    Resolution mirrors ``recall._cloud_fallback`` — the ONLY sanctioned
    consented-cloud dispatch pattern: resolve ``TaskKind.SUMMARY`` with
    ``on_device_available=False``; a target other than ``CLOUD`` (consent off, no
    provider configured) returns ``None`` (leave unnamed). On ``CLOUD``, hand the
    resolved cloud provider the SAME ``stripped=True``-marked summary the
    on-device path built (never frames — the backends fail-close on unmarked
    input and only ever receive the ALLOW-only text summary, R7/R8/KTD4).

    Strictly fail-open (R5): missing provider, unknown name, a provider that
    doesn't implement ``segment``, ``None`` / ``PROVIDER_UNAVAILABLE``, or any
    error → ``None`` (leave the recording unnamed). NEVER raises, NEVER uploads.

    Returns a validated tasks dict on a successful cloud segmentation, else
    ``None``. The DAY_SPLIT idle-gap heuristic remains the caller's fallback for
    boundaries when this returns ``None``.
    """
    if summary is None:
        return None

    from screencap import config
    from screencap.segmentation.consent import (
        ConsentPolicy,
        ExecutionTarget,
        TaskKind,
    )
    from screencap.segmentation.provider import LLMProvider, get_provider

    target = ConsentPolicy.from_config().resolve(
        TaskKind.SUMMARY, on_device_available=False
    )
    if target is not ExecutionTarget.CLOUD:
        return None  # consent off / no provider → leave unnamed (never cloud).

    cloud_name = config.get_llm_cloud_provider()
    if not cloud_name:
        return None

    try:
        provider = get_provider(cloud_name)
    except ValueError:
        logger.warning(
            "terminal_stage: configured cloud provider %r is unknown; leaving unnamed",
            cloud_name,
        )
        return None

    # A cloud provider name that does not implement segment() must degrade, not
    # raise. (LLMProvider is runtime_checkable, so this duck-types safely.)
    if not isinstance(provider, LLMProvider):
        return None

    # segment() returns a validated tasks dict, None (ran, no usable tasks), or
    # PROVIDER_UNAVAILABLE (could not run). Only a real dict is usable; the
    # sentinel is a distinct non-dict class, so the isinstance check excludes it —
    # every other outcome leaves the recording to the caller's heuristic fallback.
    result = provider.segment(summary)
    return result if isinstance(result, dict) else None


def _heuristic_local_tasks(recording_dir: Path) -> dict | None:
    """Idle-gap heuristic fallback for a LOCAL recording (U7, R5 / AE4).

    Reused when the on-device provider is UNAVAILABLE (never for a genuine empty
    provider result). Reads the recording's LOCAL ``action_event`` rows from
    ``recording.db``, splits them on inactivity gaps via the shared
    ``task_manifest._segment_tasks`` (the same idle-gap logic the v1 manifest
    uses), and returns a ``{"tasks": [...]}`` dict shaped like a provider result
    so ``_persist_local_tasks`` writes it identically. Tasks are
    mechanically-named (``task_1`` …) — the heuristic has no model to name them,
    and cloud is never consulted (R7 over R5).

    Returns ``None`` when there are no events to segment; strictly local (no
    network, no upload). All imports deferred to keep the import surface light.
    """
    from screencap import config
    from screencap.recording_db import Row, open_recording_db
    from screencap.task_manifest import _segment_tasks

    db_path = recording_dir / "recording.db"
    if not db_path.exists():
        return None

    with open_recording_db(db_path, row_factory=Row) as conn:
        events = conn.execute(
            """SELECT timestamp, name FROM action_event
               WHERE name != 'move'
               ORDER BY timestamp""",
        ).fetchall()

    segments = _segment_tasks(events, config.get_rest_threshold())
    if not segments:
        return None

    tasks = [
        {
            "start_ts": float(start),
            "end_ts": float(end),
            "name": f"task_{i + 1}",
            "derived_name": f"task-{i + 1}",
            "event_count": count,
            "source": "idle_gap_heuristic",
        }
        for i, (start, end, count) in enumerate(segments)
    ]
    return {"tasks": tasks, "summary": {"source": "idle_gap_heuristic"}, "tags": []}


def _persist_local_tasks(
    recording_dir: Path,
    ledger: "PipelineLedger | None",
    tasks: dict,
) -> int:
    """Write the validated tasks to the LOCAL-only store; return the task count.

    Two co-located sinks, both LOCAL-only (never uploaded — R4):

    * ``tasks.json`` in the recording dir — the human/app-readable artifact
      (excluded from ``upload.list_recording_files`` + rejected by
      ``assert_uploadable`` exactly like ``recording.db``). Written atomically
      (tmp + ``os.replace``) so a crash never leaves a torn file.
    * the ``pipeline_task_segments`` ledger table inside ``recording.db`` —
      queryable per-task rows (task_index, start/end, name, category,
      confidence, metadata, source, edited). This is an AGENT re-segmentation
      pass: ``replace_task_segments`` is SCOPED (U5, KTD3) — it refreshes only
      unedited agent rows and PRESERVES ``source='user'`` / user-edited rows, so
      a re-run never clobbers user work (R8).

    Both sinks are source-aware: agent entries are written ``source='agent'`` and
    any pre-existing user / edited entries in ``tasks.json`` are carried forward
    (mirroring the ledger's scoped replace), so an agent pass never overwrites a
    user-curated task in either store.
    """
    import json

    from screencap.pipeline_state import (
        TASK_SOURCE_AGENT,
        TASK_SOURCE_USER,
        TaskSegmentRow,
    )

    task_list = [
        t for t in (tasks.get("tasks", []) if isinstance(tasks, dict) else [])
        if isinstance(t, dict)
    ]
    final_path = recording_dir / _LOCAL_TASKS_FILE

    # Preserve user-authored / user-edited entries from any existing tasks.json
    # so this AGENT pass never clobbers user work (R8, KTD3) — the tasks.json
    # mirror of the ledger's scoped replace. Best-effort: a torn / legacy file is
    # ignored (the ledger remains the authoritative store).
    preserved: list[dict] = []
    if final_path.exists():
        try:
            prior = json.loads(final_path.read_text())
            prior_tasks = prior.get("tasks", []) if isinstance(prior, dict) else []
            preserved = [
                t for t in prior_tasks
                if isinstance(t, dict)
                and (t.get("source") == TASK_SOURCE_USER or t.get("edited"))
            ]
        except (OSError, ValueError):
            preserved = []

    # 1. tasks.json — atomic write (tmp + replace), local-only by upload rule.
    #    Agent entries carry an explicit source/edited marker so a later pass (or
    #    the app) can tell them apart from preserved user / edited entries.
    agent_json_tasks = [
        {**t, "source": TASK_SOURCE_AGENT, "edited": False} for t in task_list
    ]
    merged = dict(tasks) if isinstance(tasks, dict) else {}
    merged["tasks"] = agent_json_tasks + preserved
    payload = json.dumps(merged, indent=2)
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    tmp_path.write_text(payload)
    os.replace(tmp_path, final_path)

    # 2. pipeline_task_segments ledger table (SCOPED agent replace — preserves
    # user / edited rows). Skipped for a legacy / no-ledger recording (no
    # recording.db row to key on) — tasks.json still carries the tasks then.
    if ledger is not None:
        segments = [
            TaskSegmentRow(
                task_index=i,
                start_ts=float(t.get("start_ts", 0.0)),
                end_ts=float(t.get("end_ts", 0.0)),
                name=str(t.get("name", "")),
                category=t.get("category"),
                confidence=t.get("confidence"),
                metadata=json.dumps({
                    k: t[k]
                    for k in ("description", "apps_used", "derived_name")
                    if k in t
                }) or None,
                source=TASK_SOURCE_AGENT,
                edited=False,
            )
            for i, t in enumerate(task_list)
        ]
        ledger.replace_task_segments(segments)

    return len(task_list)


def _route_cloud(
    recording_dir: Path,
    *,
    ledger: "PipelineLedger | None",
    console: "Console | None",
    force: bool,
    result: TerminalResult,
    remote_exists: Callable[[int], bool] | None,
    policy: "ResolvedPolicy | None" = None,
    retention_override: "RetentionPolicy | str | None" = None,
    on_progress: Callable[[str], None] | None = None,
    stop_event: "threading.Event | None" = None,
) -> TerminalResult:
    """Produce the cloud copy, reconcile, upload, gate the sentinel.

    Order (all inside the held flock):

    0. **AE8 hole-refusal FIRST** — refuse (``PromotionRefused``) if any chunk
       required for a complete upload has its local media gone AND is
       unconfirmable in GCS, BEFORE producing any cloud copy. This moves the
       standalone ``assert_promotable_to_cloud`` guard INTO the terminal stage
       (SCR-125 U3) so retiring the CLI's standalone call does not lose AE8.
    1. **Reconcile from disk** — re-stat not-yet-UPLOADED chunks so a
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

    # 0a. SCR-116 ACCOUNT-OWNERSHIP GATE. A cloud recording is pinned at start to
    # the uid that owned it. If THIS process would act as a different account (the
    # user switched accounts since), refuse every cloud op — no produce, no
    # upload, no sentinel, no eviction — and leave the recording intact locally,
    # rather than fragmenting it into a second user's namespace (or writing the
    # sentinel under the wrong namespace). This guards the terminal-stage entry
    # points the daemon re-mint guard cannot — the daemon resume and `screencap
    # upload`, which read the Keychain (the current account), not the engine
    # token. Unknown ownership (legacy/local recording) or an undeterminable
    # current uid (not signed in / transient) does NOT refuse here: the former
    # has no pin to honor, the latter is left to the normal fail-closed upload
    # path so we never raise a false mismatch.
    from screencap.catalog import read_owner_uid

    owner_uid = read_owner_uid(recording_dir)
    if owner_uid is not None:
        from screencap import auth

        try:
            current_uid = auth.id_token_uid(auth.get_id_token())
        except Exception:  # noqa: BLE001 — NotSignedIn/AuthError/etc: don't refuse
            current_uid = None
        if current_uid is not None and current_uid != owner_uid:
            logger.warning(
                "terminal_stage: account mismatch for %s (recording owner uid "
                "!= signed-in uid) — refusing cloud convergence; kept local", name,
            )
            result.upload_warning = (
                "account mismatch — this recording belongs to a different account "
                "than the one now signed in; cloud convergence refused (kept local)"
            )
            # SCR-171: typed signal the daemon lifts onto /v0/events. Both uids
            # are authoritative for the comparison that fired this refusal.
            result.account_mismatch = AccountMismatch(
                owner_uid=owner_uid, signed_in_uid=current_uid
            )
            return result

    # 0. AE8 — refuse a promotion with HOLES before producing any cloud copy
    # (fail-closed, never a partial cloud copy). A legacy / no-ledger recording
    # has no closed chunk set, so this is a no-op (R14 whole-dir path). SCR-175:
    # thread on_progress so the per-chunk hole-probe loop pings the interactive
    # watchdog (it scales with evicted/hole chunks and was otherwise silent).
    assert_promotable_to_cloud(
        recording_dir, remote_exists=remote_exists, on_progress=on_progress
    )

    # 1. Reconcile from disk before doing work (R9). Confirmed-in-GCS chunks
    # flip to UPLOADED so we never re-upload them.
    if ledger is not None:
        result.reconciled = _reconcile_ledger_against_gcs(
            recording_dir, ledger, remote_exists=remote_exists,
            on_progress=on_progress,
        )
        # 1a. SCR-127 — the SYMMETRIC re-validation: a stale/false UPLOADED (the
        # live mirror wrote it from in-memory EMITTED, no independent re-confirm)
        # is downgraded on a confirmed-absent BEFORE the gate / fast path below,
        # so neither can trust it. Bounded sample, downgrade-on-positive-absent
        # only — a downgraded chunk re-converges through produce + upload.
        result.downgraded = _revalidate_uploaded_chunks(
            recording_dir, ledger, remote_exists=remote_exists,
            on_progress=on_progress,
        )

    # 1b. ALREADY-CONVERGED FAST PATH (SCR-125 U4 "finalize is cheap"). If the
    # reconcile shows the FROZEN closed set is all UPLOADED in GCS, the recording
    # is already complete — its scrubbed events/manifest were uploaded by the
    # live path (a chunk is only UPLOADED once its core files are confirmed
    # remote). Skip the expensive ``produce`` re-scrub + the no-op upload and go
    # straight to the sentinel + retention. This keeps the engine finalize within
    # the stop budget (the live path already did the work) and makes the daemon
    # resume / CLI re-upload of a converged recording a near-no-op. ``force``
    # always rebuilds (re-scrub + re-upload).
    #
    # SCR-126 R8: the fast path skips ``produce`` (hence masking + provenance), so
    # it must NOT declare convergence for a recording whose masked cloud video was
    # produced under OLDER mask logic — ``_masked_convergence_ok`` falls it through
    # to ``produce`` (re-mask) on a MASK_PROVENANCE_VERSION miss. No-op when the
    # frozen masked flag is OFF (today's default — no masked copy is involved).
    if (
        not force
        and ledger is not None
        and ledger.finalize_gate_satisfied()
        and _masked_convergence_ok(recording_dir)
    ):
        result.routed = True
        _refresh_counts(ledger, result)
        result.all_uploaded = True
        result.finalize_gate_satisfied = True
        result.sentinel_uploaded = _write_sentinel(recording_dir, ledger, result)
        _apply_retention(
            recording_dir, policy, ledger, result,
            remote_exists=remote_exists, retention_override=retention_override,
        )
        return result

    # U9: ledger-safe boundary before the long produce/upload phase. The reconcile
    # above only re-stat'd chunks (idempotent, no partial transition), so bailing
    # here leaves the ledger consistent and the recording resumes on unlock — the
    # store can seal WITHOUT waiting for this upload (AE7).
    _check_stop(stop_event)

    # 2. Produce the cloud copy via the scrub seam adapter. SCR-175: ping just
    # before the longest silent phase (recovery + scrub + OCR masking) so the
    # interactive watchdog enters produce() with a freshly-reset budget rather
    # than whatever the lock wait + reconcile already consumed of it.
    _notify_progress(on_progress, "scrub")
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

    # U9: ledger-safe boundary between produce (scrubbed copy on disk, but NO
    # ledger UPLOADED mark yet) and the upload. Bailing here means no chunk is
    # marked UPLOADED that was not confirmed remote, and no sentinel is written —
    # the recording resumes cleanly on unlock.
    _check_stop(stop_event)

    # 3. Upload the scrubbed copy's artifacts. ``upload_recording`` enumerates
    # via ``list_recording_files`` which already excludes recording.db + WAL
    # sidecars + *.scrub_failed (U2) — the raw DB never enters the set (AE4).
    # SCR-126 Fix 1: resolve the FROZEN masked-video decision from the SOURCE
    # recording dir (NOT the scrubbed dir being enumerated) and pass it so the
    # rglob path gates any chunk_*.mp4 not under masked_video/ (fail closed).
    # SCR-220 (KTD-4): same for the frozen E2EE decision — the scrubbed copy
    # carries no intent of its own, so upload_recording's self-resolve would
    # read frozen-off there.
    from screencap.pipeline_chunk_ops import (
        get_frozen_cloud_e2ee,
        get_frozen_masked_video_upload,
    )

    try:
        upload_result = upload_recording(
            copy.scrubbed_dir, force=force,
            masked_video_upload=get_frozen_masked_video_upload(recording_dir),
            cloud_e2ee=get_frozen_cloud_e2ee(recording_dir),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("terminal_stage: upload failed for %s: %s", name, exc)
        result.upload_warning = _append_warning(result.upload_warning, f"upload failed: {exc}")
        return result

    result.routed = True
    uploaded_ok = not upload_result.failed
    if not uploaded_ok:
        # SCR-79: a per-file upload failure (a file's PUT failed after retries —
        # ``upload_recording`` emits the terminal ``upload_failed`` event and
        # returns ``result.failed`` non-empty WITHOUT raising) must be surfaced
        # like every OTHER failure mode in this function. Without it the CLI sees
        # a clean result, prints "Uploaded", and exits 0 despite the event. The
        # sentinel is still withheld below (the finalize gate stays unsatisfied)
        # and local media is preserved for a resumable retry.
        upload_failure_warning = (
            f"{len(upload_result.failed)} file(s) failed to upload — "
            "sentinel withheld, local media preserved"
        )
        result.upload_warning = _append_warning(result.upload_warning, upload_failure_warning)

    # 3b. SCR-129: upload the per-chunk SOURCE media (video + audio). The
    # `<name>-scrubbed` copy uploaded above STRIPS all media
    # (scrubber._SKIP_EXTENSIONS), so on the terminal-stage-only paths
    # (`--no-live-upload`, daemon resume of an all-session-failed live upload,
    # local->cloud promotion) — where the live chunk_processor never uploaded —
    # the video/audio would otherwise NEVER reach the cloud, leaving a
    # permanently media-less copy (the chunks would then stay PENDING and the
    # sentinel withheld). Runs BEFORE the confirm pass so `_chunk_confirmed_remote`
    # can see the freshly-uploaded media. Idempotent (a chunk the live path
    # already shipped returns url=None and is not re-PUT).
    media_warning = _upload_source_media(
        recording_dir, ledger,
        scrubbed_dir=copy.scrubbed_dir,
        failed_chunks=set(copy.failed_chunks),
    )
    if media_warning:
        # Surface alongside (not instead of) any mask/scrub warning already set
        # — a media-upload failure for a non-mask-failed chunk must stay visible
        # (this bug is precisely "the cloud copy is incomplete and nobody is told").
        result.upload_warning = _append_warning(result.upload_warning, media_warning)

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
    _apply_retention(
        recording_dir, policy, ledger, result,
        remote_exists=remote_exists, retention_override=retention_override,
    )

    return result


def _media_upload_indices(
    ledger: "PipelineLedger | None",
    failed_chunks: set[int],
) -> list[int]:
    """Chunk indices whose source media the terminal stage should (re)upload.

    The closed set minus the chunks that must NOT be media-uploaded: already
    ``UPLOADED`` (the live path shipped them — confirmed in GCS), ``LOCAL_DONE``
    / ``SKIPPED`` (no cloud copy by design), ``EVICTED`` (media already gone,
    re-confirmed in GCS before eviction), and fail-closed mask failures (no
    safe copy to ship).

    A legacy / no-ledger dir (R14) has no closed chunk set to gate on — it is
    the whole-dir scrub path, not a per-chunk media upload (a true single-file
    recording has a single ``video.mp4``, not ``chunk_*.mp4``), so this is a
    no-op there, mirroring how reconcile / promotion treat the no-ledger case.
    """
    if ledger is None:
        return []

    from screencap.pipeline_state import Lifecycle, UploadState

    out: list[int] = []
    for row in ledger.all_chunks():
        if row.chunk_index in failed_chunks:
            continue
        if row.upload_state == UploadState.UPLOADED:
            continue
        if row.lifecycle in (Lifecycle.LOCAL_DONE, Lifecycle.SKIPPED, Lifecycle.EVICTED):
            continue
        out.append(row.chunk_index)
    return out


def _upload_source_media(
    recording_dir: Path,
    ledger: "PipelineLedger | None",
    *,
    scrubbed_dir: Path,
    failed_chunks: set[int],
) -> str | None:
    """Upload each not-yet-uploaded chunk's MEDIA (video + audio) — SCR-129.

    The ``<name>-scrubbed`` copy the terminal stage uploads strips all media, so
    the chunk video/audio must be shipped from the source directly here.
    Mirrors the live path's ``_collect_chunk_files`` masked-path-switch
    (R-SCR125-A):

    * video ``chunk_NNNN.mp4`` — from ``<name>-scrubbed/masked_video/`` when the
      FROZEN ``masked_video_upload`` flag is ON (NEVER the rich source), else
      from the source dir;
    * audio ``audio_NNNN.flac`` — always from the source dir (audio is not
      masked, exactly as the live path uploads it).

    Only the MEDIA is shipped — NOT events/manifest/transcript. Those are the
    fully-scrubbed copies in ``<name>-scrubbed`` that the step-3 upload already
    sent; the SOURCE events are window-filtered but NOT PII-scrubbed, so
    uploading them would be an R8 leak. The GCS object NAME stays the plain
    ``chunk_NNNN.mp4`` / ``audio_NNNN.flac`` so the per-chunk
    ``_chunk_confirmed_remote`` probe recognizes them.

    Reuses ``chunk_processor.upload_chunk_files`` (same ``assert_uploadable`` R8
    gate + ``request_signed_urls`` resumability the live path uses). Returns a
    one-line warning naming the chunk(s) whose media upload failed, else
    ``None``.
    """
    from screencap.chunk_processor import upload_chunk_files
    from screencap.pipeline_chunk_ops import get_frozen_masked_video_upload
    from screencap.scrubber import masked_video_dir

    indices = _media_upload_indices(ledger, failed_chunks)
    if not indices:
        return None

    masked_on = get_frozen_masked_video_upload(recording_dir)
    masked_dir = masked_video_dir(scrubbed_dir)
    recording_name = _read_recording_name(recording_dir)

    failures: list[int] = []
    for idx in indices:
        video_name = f"chunk_{idx:04d}.mp4"
        # Flag ON → ship the masked copy; flag OFF → the capture-blocked rich
        # source IS the cloud copy (byte-for-byte the live path's choice).
        # KNOWN (flag ON, default OFF — folded into SCR-126 when the flag flips):
        # the step-3 ``upload_recording(scrubbed_dir)`` rglobs subdirs and ALSO
        # ships ``masked_video/chunk_NNNN.mp4`` under that nested key. The plain
        # key uploaded here is the one ``_chunk_confirmed_remote`` probes; the
        # nested copy is an orphaned duplicate (wasted bytes, not a correctness
        # bug). Suppressing it belongs with enabling masked upload, not here.
        video_path = (masked_dir if masked_on else recording_dir) / video_name
        specs = [
            (video_name, video_path),
            (f"audio_{idx:04d}.flac", recording_dir / f"audio_{idx:04d}.flac"),
        ]
        files = [
            {"name": nm, "path": p}
            for nm, p in specs
            if p.exists() and p.stat().st_size > 0
        ]
        if not files:
            continue
        try:
            ok = upload_chunk_files(recording_name, files, recording_dir)
        except Exception as exc:  # noqa: BLE001 — never crash the terminal stage
            logger.error(
                "terminal_stage: source-media upload raised for chunk %d: %s",
                idx, exc,
            )
            ok = False
        if not ok:
            failures.append(idx)

    if failures:
        return (
            f"source media upload failed for chunk(s) "
            f"{', '.join(str(i) for i in failures)} — sentinel may be withheld"
        )
    return None


def _apply_retention_override(
    recording_dir: Path,
    policy: "ResolvedPolicy | None",
    retention_override: "RetentionPolicy | str | None",
) -> "ResolvedPolicy | None":
    """Return ``policy`` with its retention axis replaced by ``retention_override``.

    ``None`` override → ``policy`` unchanged. When ``policy`` is ``None`` (legacy
    recording with no frozen policy) the override still takes effect via a
    freshly-built policy whose destination is the recording's routed destination,
    so ``--no-delete`` (``keep_forever``) is honored even on a legacy recording.
    """
    if retention_override is None:
        return policy
    from dataclasses import replace

    from screencap.pipeline_policy import RetentionPolicy

    ro = (
        RetentionPolicy(retention_override)
        if isinstance(retention_override, str)
        else retention_override
    )
    if policy is not None:
        return replace(policy, retention_policy=ro)
    from screencap.pipeline_policy import ResolvedPolicy

    return ResolvedPolicy(
        destination=_resolve_destination(recording_dir, None),
        retention_policy=ro,
        params={},
    )


def _apply_retention(
    recording_dir: Path,
    policy: "ResolvedPolicy | None",
    ledger: "PipelineLedger | None",
    result: TerminalResult,
    *,
    remote_exists: Callable[[int], bool] | None,
    retention_override: "RetentionPolicy | str | None" = None,
) -> None:
    """Invoke the U8 eviction executor for this recording, behind our flock.

    A thin seam onto :func:`screencap.retention.evict_recording`. We are already
    inside the per-recording terminal flock, so eviction cannot race a
    concurrent terminal run reading the same chunk (AE12 / the U10 read-vs-evict
    serialization). The executor enforces the never-delete-un-uploaded /
    fresh-remote-confirm floor; we only surface its report onto ``result``. Any
    error is non-fatal — a failed eviction never compromises an already-uploaded
    recording (it just leaves more local files than retention wanted).

    ``retention_override`` (SCR-125 U3) replaces the frozen policy's retention
    for this run — e.g. ``screencap upload --no-delete`` passes ``keep_forever``
    so local media survives the upload. It overrides only the retention axis;
    the destination and params are unchanged.
    """
    if ledger is None:
        return
    policy = _apply_retention_override(recording_dir, policy, retention_override)
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
    on_progress: Callable[[str], None] | None = None,
) -> int:
    """Re-stat not-yet-UPLOADED chunks against the server; flip confirmed ones.

    Mirrors ``ChunkProcessor.reconcile_against_gcs`` but reads the LEDGER, not
    in-memory ``_chunk_results``: for every chunk whose ``upload_state`` is
    PENDING or FAILED we ask the server (via ``request_signed_urls`` —
    ``url=None`` ⇒ already there). When the server confirms a chunk's core
    files are all present, we ``mark_uploaded``. PENDING/FAILED only — this pass
    never re-probes an already-``UPLOADED`` chunk; the inverse direction
    (downgrading a stale/confirmed-absent ``UPLOADED``) is
    :func:`_revalidate_uploaded_chunks` (SCR-127). Returns the flip count.

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
        # SCR-175: ping BEFORE each probe (a per-chunk network round-trip) so a
        # many-chunk reconcile is never one unbounded silent span under the
        # interactive watchdog — each probe resets it.
        _notify_progress(on_progress, "reconcile")
        if _chunk_confirmed_remote(recording_dir, idx, remote_exists=remote_exists):
            with contextlib.suppress(Exception):
                ledger.mark_uploaded(idx)
                flipped += 1
                logger.info("terminal_stage: reconciled chunk %d (already in GCS)", idx)
    return flipped


def _chunk_probe_infos(recording_dir: Path, idx: int) -> "tuple[list[FileInfo], bool]":
    """Build the GCS-probe ``FileInfo`` list for chunk ``idx`` from local files.

    Probe the chunk's core files across ALL the dirs they can live in. The
    scrubbed copy STRIPS media (scrubber._SKIP_EXTENSIONS skips .mp4/.flac), so
    the chunk's VIDEO/AUDIO live in the SOURCE dir (the live path uploads them
    from there) while the scrubbed events/manifest live in <name>-scrubbed (and
    the masked cloud video, when the flag is on, lives in masked_video/). Probing
    only the scrubbed dir would judge "confirmed" on events+manifest alone and
    miss the video — a media-blind false positive (SCR-123). Search
    source -> masked_video -> scrubbed for each name. Keeping this in ONE place
    means both the confirm (:func:`_chunk_confirmed_remote`) and the re-validate
    (:func:`_chunk_confirmed_absent`) probes stay media-aware together.

    Returns ``(infos, video_present)``: the VIDEO must be present locally to prove
    anything about GCS — events+manifest alone is the media-blind confirm.
    """
    from screencap.upload import FileInfo, _content_type

    scrubbed_dir = recording_dir.parent / f"{recording_dir.name}-scrubbed"
    masked_dir = scrubbed_dir / "masked_video"
    search_dirs = [d for d in (recording_dir, masked_dir, scrubbed_dir) if d.exists()]
    video_name = f"chunk_{idx:04d}.mp4"
    core_names = [
        video_name,
        f"audio_{idx:04d}.flac",
        f"events_{idx:04d}.jsonl",
        f"chunk_{idx:04d}_manifest.json",
    ]
    infos: list[FileInfo] = []
    video_present = False
    for nm in core_names:
        for d in search_dirs:
            p = d / nm
            if p.exists() and p.stat().st_size > 0:
                infos.append(FileInfo(nm, p, _content_type(p), p.stat().st_size))
                if nm == video_name:
                    video_present = True
                break
    return infos, video_present


def _probe_recording_key(recording_dir: Path) -> str:
    """The GCS recording key for a probe — the original id, NOT a dir basename.

    Derived EXACTLY as ``upload_recording`` does, from the SOURCE
    ``recording.db`` sibling ``.recording_id`` (the original id, also copied into
    <name>-scrubbed), NOT a dir basename (which would be "<name>-scrubbed" and
    probe the wrong prefix).
    """
    id_file = recording_dir / ".recording_id"
    return id_file.read_text().strip() if id_file.exists() else recording_dir.name


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

    from screencap.upload import request_signed_urls

    infos, video_present = _chunk_probe_infos(recording_dir, idx)
    # The VIDEO must be present locally to probe — events+manifest alone is the
    # media-blind confirm. If the video is gone everywhere locally we cannot
    # prove it is in GCS, so fail closed (conservative False): a reconcile leaves
    # the chunk PENDING, a promotion refuses the hole, an eviction is refused.
    if not video_present or not infos:
        return False
    try:
        urls, _ = request_signed_urls(_probe_recording_key(recording_dir), infos)
    except Exception:  # noqa: BLE001
        return False
    return all(fi.name in urls and urls[fi.name] is None for fi in infos)


def _chunk_confirmed_absent(
    recording_dir: Path,
    idx: int,
    *,
    remote_exists: Callable[[int], bool] | None,
) -> bool:
    """True ONLY when GCS DEFINITIVELY reports chunk ``idx`` is not fully present.

    The fail-SAFE inverse of :func:`_chunk_confirmed_remote`, used by the SCR-127
    re-validation downgrade pass. Returns True only on POSITIVE confirmed-absent
    evidence; a fully-present chunk, an unreachable server, or an un-probeable
    chunk (local video gone) all return False. The asymmetry is deliberate — a
    downgrade forces a re-upload, so it must fire only on proof the object is
    gone, never on a transient blip (which would churn re-uploads).

    Injected ``remote_exists`` (tests / eviction seam): a plain ``False`` is the
    confirmed-absent signal; an exception (unreachable) is NOT. On the real path,
    a core file reported with a NON-None signed URL means GCS does not have it
    (here is where to PUT it) → absent; an all-``url=None`` response is present;
    any exception is not-absent.
    """
    if remote_exists is not None:
        try:
            return not bool(remote_exists(idx))
        except Exception:  # noqa: BLE001 — unreachable is NOT confirmed-absent
            return False

    from screencap.upload import request_signed_urls

    infos, video_present = _chunk_probe_infos(recording_dir, idx)
    # No local video → cannot build the probe → cannot prove absence (e.g. an
    # evicted chunk; those are skipped by the caller regardless). Fail safe.
    if not video_present or not infos:
        return False
    try:
        urls, _ = request_signed_urls(_probe_recording_key(recording_dir), infos)
    except Exception:  # noqa: BLE001 — unreachable is NOT confirmed-absent
        return False
    # A definitive response. Any core file with a NON-None signed URL means GCS
    # does not have it → the chunk is not fully uploaded → confirmed absent.
    return any(fi.name in urls and urls[fi.name] is not None for fi in infos)


# The re-validation downgrade pass re-stats at most this many of the OLDEST
# still-resident UPLOADED chunks per terminal-stage entry. Bounded so a resume of
# an already-converged recording stays a near-no-op (it does NOT re-probe every
# chunk on every entry) — this is the "sample" the SCR-127 issue calls for, NOT
# exhaustive coverage. It targets the eviction FRONTIER: under delete_after_upload
# retention evicts oldest-first, so the lowest-index resident chunks are the ones
# whose stale UPLOADED would matter first, and as they evict the window slides to
# cover more over repeated entries. Under the DEFAULT keep_forever policy nothing
# evicts, so the oldest N stay a fixed sample (chunks past N are not re-validated)
# — acceptable because keep_forever never deletes either, so the residual risk is
# only an optimistic gate/sentinel on a chunk whose cloud copy silently regressed,
# never local data loss. The eviction-time begin_eviction re-confirm stays the
# load-bearing data-loss gate regardless.
_REVALIDATE_SAMPLE_SIZE = 8


def _revalidate_uploaded_chunks(
    recording_dir: Path,
    ledger: "PipelineLedger",
    *,
    remote_exists: Callable[[int], bool] | None,
    on_progress: Callable[[str], None] | None = None,
) -> int:
    """Re-stat a sample of UPLOADED rows; downgrade confirmed-absent ones (SCR-127).

    The symmetric counterpart to :func:`_reconcile_ledger_against_gcs`'s promote
    pass: that flips PENDING/FAILED → UPLOADED on a fresh remote confirm; this
    flips a stale UPLOADED → FAILED when GCS DEFINITIVELY reports the chunk's core
    files are gone. The live mirror writes UPLOADED from in-memory EMITTED without
    an independent re-confirm, so without this a false/stale UPLOADED was trusted
    forever (``finalize_gate_satisfied`` → already-converged fast path → eviction
    candidate). A downgraded chunk re-enters the normal produce + upload path and
    is re-confirmed by ``_mark_uploaded_chunks`` (its local media is still here —
    we only downgrade probeable, non-evicted rows).

    Fail-SAFE: downgrades ONLY on positive confirmed-absent evidence
    (:func:`_chunk_confirmed_absent`). An unreachable server or an un-probeable
    chunk leaves the row UPLOADED. EVICTED rows are skipped — they keep
    ``upload_state == UPLOADED`` by design and have no local media to re-probe
    (the "evicted but in GCS is fine" case, mirroring
    :func:`detect_promotion_holes`); re-probing one would wrongly downgrade every
    legitimately-evicted chunk and permanently block the finalize gate.

    The eviction-time fresh re-confirm (``begin_eviction``) remains the
    load-bearing data-loss gate regardless; this only keeps the ledger honest
    upstream of it. Returns the number of rows downgraded.
    """
    from screencap.pipeline_state import Lifecycle, UploadState

    resident = [
        r.chunk_index
        for r in ledger.all_chunks()  # ordered by chunk_index ascending (oldest first)
        if r.upload_state == UploadState.UPLOADED and r.lifecycle != Lifecycle.EVICTED
    ]
    downgraded = 0
    for idx in resident[:_REVALIDATE_SAMPLE_SIZE]:
        # SCR-175: same per-probe heartbeat as the reconcile pass (bounded to the
        # sample size, but each probe is still a network round-trip).
        _notify_progress(on_progress, "reconcile")
        if _chunk_confirmed_absent(recording_dir, idx, remote_exists=remote_exists):
            with contextlib.suppress(Exception):
                ledger.mark_failed(
                    idx, detail="reconcile: UPLOADED row confirmed absent in GCS",
                )
                downgraded += 1
                logger.warning(
                    "terminal_stage: chunk %d was UPLOADED but is now absent in "
                    "GCS — downgraded to FAILED for re-upload",
                    idx,
                )
    return downgraded


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
    on_progress: Callable[[str], None] | None = None,
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

    # Iterate the actual SEEDED ledger rows (the closed set), not range(expected):
    # the U9 reconciler / a sparse on-disk index set can leave non-contiguous
    # chunk_index values, and an evicted chunk keeps its ledger row, so the seeded
    # rows are the authoritative set to gate on (mirrors finalize_gate_satisfied).
    from screencap.pipeline_state import UploadState

    rows = ledger.all_chunks()
    confirm = _make_promotion_confirm(recording_dir, remote_exists)
    holes: list[int] = []
    for row in rows:
        idx = row.chunk_index
        if row.upload_state == UploadState.UPLOADED:
            # Already in GCS per the ledger (an EVICTED chunk keeps
            # upload_state == UPLOADED). Eviction only reclaims a chunk's local
            # media AFTER a fresh remote re-confirm (the retention floor), so an
            # UPLOADED-then-evicted chunk is exactly the "evicted but in GCS is
            # fine" case this guard explicitly allows — its video is gone locally
            # and cannot be freshly re-probed, so trust the ledger here, just as
            # _reconcile_ledger_against_gcs does (it never re-stats an UPLOADED
            # chunk). The fresh re-confirm below still gates PENDING/FAILED rows.
            continue
        if _chunk_local_media_present(recording_dir, idx):
            continue  # rich local copy survives — upload can re-scrub it.
        # SCR-175: ping BEFORE each per-chunk GCS confirm (a network round-trip)
        # so the AE8 hole scan of a large, partially-evicted recording is never
        # one unbounded silent span under the interactive watchdog — each probe
        # resets it. Mirrors _reconcile_ledger_against_gcs's per-probe ping.
        _notify_progress(on_progress, "reconcile")
        if confirm(idx):
            continue  # cloud copy survives — no hole.
        holes.append(idx)
    return holes


def assert_promotable_to_cloud(
    recording_dir: Path | str,
    *,
    remote_exists: Callable[[int], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
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
    holes = detect_promotion_holes(
        recording_dir, remote_exists=remote_exists, on_progress=on_progress
    )
    if not holes:
        return
    name = recording_dir.name
    missing = ", ".join(str(i) for i in holes)
    raise PromotionRefused(
        f"Cannot promote {name!r} to the cloud: chunk(s) {missing} are missing "
        "locally and could not be confirmed in the cloud (not marked UPLOADED in "
        "the ledger, and a fresh remote check did not find them), so a complete "
        "upload cannot be guaranteed. Refusing to upload a recording with "
        "unconfirmable chunks (it would risk a partial copy with silent gaps). "
        "If you have a local backup of the missing chunk media, restore it "
        "before promoting."
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
