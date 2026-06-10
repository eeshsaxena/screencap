"""Background chunk processing during continuous recording.

Runs as a non-daemon thread: at each video auto-cut, processes the
completed chunk (transcribe → export events → generate manifest →
upload → delete old chunks).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from screencap._flush import wait_for_writer_flush
from screencap.pipeline_stages import StageArtifacts as _StageArtifacts
from screencap.recording_db import open_recording_db

if TYPE_CHECKING:
    from screencap.network.export_pipeline import NetworkScrubPipeline

logger = logging.getLogger(__name__)


class _StageAbort(Exception):
    """Internal signal: a ``_stop_event`` abort fired inside an agnostic
    stage step. Caught in ``_run_agnostic_stages`` so the runner never
    reaches ``mark_staged`` — the chunk stays PENDING (force-stop
    survivorship-bias fix).
    """


class _UploadOutcome(str, Enum):
    """Outcome of the live per-chunk cloud upload op (SCR-125 U1).

    ``DEFERRED`` is the lock-contention skip: another terminal-stage entry point
    (manual ``screencap upload`` / daemon resume) holds the per-recording flock,
    so this chunk is left ``PENDING`` for the convergence pass — never lost, never
    force-uploaded under contention. ``UPLOADED`` / ``FAILED`` settle the chunk.
    """

    UPLOADED = "uploaded"
    FAILED = "failed"
    DEFERRED = "deferred"


class ChunkStatus(str, Enum):
    """Per-chunk terminal status (V1.75).

    Replaces the V1 ``dict[int, bool]`` map. The boolean form conflated
    "uploaded" with "intentionally skipped (no encryption available)"
    and "incompletely captured (proxy died)" -- exactly the
    survivorship-bias and "disabled-treated-as-success" patterns
    documented in
    ``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``
    (four prior production incidents).

    All sentinel-upload, stub_recording, reconcile, and delete_old_chunks
    decisions key off ``EMITTED`` explicitly. Every other state blocks
    the sentinel gate -- "disabled ≠ succeeded" per the prior-incident
    fix #4. Iterating over ``s != EMITTED`` is intentionally NOT the
    contract: reconcile/delete each have their own specific status set
    they act on, never "everything that isn't EMITTED".

    State transitions:

    - ``PENDING`` is set at rotation-message receipt, BEFORE
      ``_process_chunk`` runs. Force-stop now leaves a ``PENDING``
      entry that the gate explicitly rejects (Bug 2 in the prior-
      incident doc -- survivorship-bias fix).
    - ``EMITTED`` on successful upload (or on intentionally-disabled
      upload with no ``_upload_disabled_reason``).
    - ``FAILED`` on upload failure or exception in ``_process_chunk``.
    - ``NETWORK_SKIPPED`` when the chunk had network rows but the KEK
      was unavailable at first body-bearing chunk (U4 fail-soft).
    - ``NETWORK_INCOMPLETE`` when the chunk window overlaps a
      ``NetworkHealth(event="proxy_crashed" | "network_writer_failed")``
      row (U5 overlap).
    """

    PENDING = "pending"
    EMITTED = "emitted"
    FAILED = "failed"
    NETWORK_SKIPPED = "network_skipped"
    NETWORK_INCOMPLETE = "network_incomplete"


class ChunkProcessor:
    """Process completed recording chunks in the background.

    Per chunk: wait for audio ack → transcribe → export events JSONL →
    generate task manifest → upload → delete old chunks on success.
    Never raises — all errors caught and logged.
    """

    def __init__(
        self,
        capture_dir: Path,
        chunk_process_q,  # multiprocessing.Queue — receives rotation events from fan-out
        audio_ack_q,      # multiprocessing.Queue — receives audio rotation/final acks
        *,
        recording_name: str | None = None,
        upload_enabled: bool = True,
        auto_delete: bool = True,
        rest_threshold: float = 120.0,
        flush_requested=None,      # multiprocessing.Event — triggers writer buffer flush
        flush_ack_counter=None,    # multiprocessing.Value('i') — counts writer acks
        flush_lock=None,           # threading.Lock — serializes flushes with scrub_worker
        cloud_intent: bool = False,
        privacy_mode: str = "internal",
        screen_filter=None,
        segmentation_mode: str = "llm",
        scrub_enabled: bool = False,
        show_on_website: bool = True,
        masked_video_upload: bool | None = None,
    ) -> None:
        self._capture_dir = Path(capture_dir)
        self._db_path = self._capture_dir / "recording.db"
        # SCR-125 U1: the sibling <name>-scrubbed dir holds the masked cloud
        # video copies (masked_video/) when masked_video_upload is ON.
        self._scrubbed_dir = self._capture_dir.parent / f"{self._capture_dir.name}-scrubbed"
        # The FROZEN per-recording masked-video-upload decision (R-SCR125-A):
        # resolved ONCE here from .recording_intent (falling back to the global
        # only for a legacy intent), so a mid-recording global flip can never
        # change which video path this live upload ships. Default (flag OFF) =
        # byte-for-byte today: the capture-blocked source .mp4 is uploaded.
        if masked_video_upload is None:
            from screencap.pipeline_chunk_ops import get_frozen_masked_video_upload

            masked_video_upload = get_frozen_masked_video_upload(self._capture_dir)
        self._masked_video_upload = bool(masked_video_upload)
        self._q = chunk_process_q
        self._audio_ack_q = audio_ack_q
        self._recording_name = recording_name or self._capture_dir.name
        self._upload_enabled = upload_enabled
        self._auto_delete = auto_delete
        self._rest_threshold = rest_threshold
        self._flush_requested = flush_requested
        self._flush_ack_counter = flush_ack_counter
        self._flush_lock = flush_lock
        self._cloud_intent = cloud_intent
        self._privacy_mode = privacy_mode
        self._screen_filter = screen_filter
        self._segmentation_mode = segmentation_mode
        self._scrub_enabled = scrub_enabled
        self._show_on_website = show_on_website

        # Initialize scrubbing pipeline when scrubbing is enabled
        # (cloud-intent always scrubs; local recordings scrub when user opts in)
        _should_init_scrub = (cloud_intent and upload_enabled) or scrub_enabled
        self._pipeline = None
        self._anonymizer = None
        self._masking_classifier = None
        self._masking_evaluator = None
        self._masking_pixel_ratio = 2.0  # safe Retina default
        self._upload_disabled_reason: str | None = None
        if _should_init_scrub:
            try:
                from screencap.privacy import Anonymizer, create_default_pipeline
                self._pipeline = create_default_pipeline(require_pii=True)
                self._anonymizer = Anonymizer()
                logger.info("Scrubbing pipeline initialized")
            except Exception as e:
                if cloud_intent and upload_enabled:
                    logger.error(
                        f"Privacy deps not available — disabling uploads for safety: {e}"
                    )
                    self._upload_enabled = False
                    self._upload_disabled_reason = f"Privacy deps not available: {e}"
                    logger.warning(
                        "Privacy dependencies are missing. "
                        "Reinstall or update screencap."
                    )
                else:
                    # Local recording: scrubbing is best-effort, don't block recording
                    logger.warning(f"Scrubbing pipeline unavailable (non-fatal): {e}")
                    self._scrub_enabled = False

            # Initialize classifier/evaluator for screenshot masking
            try:
                from screencap.config import get_privacy_config
                from screencap.privacy.context import DefaultContextClassifier
                from screencap.privacy.policy import DefaultPolicyEvaluator, PrivacyMode

                _pc = get_privacy_config()
                # Cloud uploads must use public mode so that CHAT/EMAIL/etc.
                # apps get MASK_WINDOW (blurred in screenshots) instead of
                # TEXT_REDACT (which only scrubs text, not visuals).
                if self._cloud_intent:
                    from dataclasses import replace as _dc_replace
                    _pc = _dc_replace(_pc, mode=PrivacyMode.PUBLIC)
                self._masking_evaluator = DefaultPolicyEvaluator(_pc)
                self._masking_classifier = DefaultContextClassifier(
                    app_classes=_pc.app_classes,
                )
            except Exception as e:
                logger.warning(f"Could not init masking classifier: {e}")
                if cloud_intent and upload_enabled:
                    self._upload_enabled = False
                    if self._upload_disabled_reason is None:
                        self._upload_disabled_reason = f"Masking classifier init failed: {e}"

        # Safety invariant: never delete local files unless uploads are enabled.
        # This covers: (1) caller passes upload_enabled=False (e.g. --no-live-upload),
        # (2) privacy pipeline init failure sets _upload_enabled=False.
        if not self._upload_enabled and self._auto_delete:
            self._auto_delete = False
            logger.info("Forced auto_delete=False because uploads are disabled")

        # See the ChunkStatus docstring +
        # chunk-upload-sentinel-gating-and-data-loss.md for the four
        # prior incidents the enum closes off.
        self._chunk_results: dict[int, ChunkStatus] = {}

        # Staging seam: the network-decrypt path stages NETWORK_SKIPPED
        # via ``setdefault`` and the proxy-health overlap path stages
        # NETWORK_INCOMPLETE via unconditional ``=`` (NETWORK_INCOMPLETE
        # > NETWORK_SKIPPED, enforced in code rather than by call
        # order). Consumed by ``_process_chunk``'s try/finally
        # final-assignment. Populated by U4/U5; always empty in this branch.
        self._pending_network_status: dict[int, ChunkStatus] = {}

        # Cached network-decrypt + scrub pipeline, constructed lazily
        # on the first body-bearing chunk. ``_network_scrub_attempted``
        # is a one-shot latch: a single failed construction does not
        # retry, and does not poison subsequent chunks (those fall
        # through to the metadata-only path).
        self._network_scrub_pipeline: NetworkScrubPipeline | None = None
        self._network_scrub_attempted: bool = False

        # Recording id, used to scope network_event and network_health
        # queries. Best-effort at construction — production callers
        # construct the ChunkProcessor AFTER crud.insert_recording, but
        # tests may pass a capture_dir without a real recording.db.
        self._recording_id: int | None = self._lookup_recording_id()

        self._status_lock = threading.Lock()
        self._status: str = ""
        self._total_freed: int = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # U5 ledger handle for the destination-agnostic stage runner, built
        # lazily on first use. ``None`` (no recording.db / no recording row,
        # e.g. test fixtures) disables ledger bookkeeping — the agnostic
        # stages still run and produce artifacts, they just aren't recorded
        # as STAGED. The on-disk artifact-existence idempotency still holds.
        self._ledger = None
        self._ledger_unavailable = False

    def _lookup_recording_id(self) -> int | None:
        """One-shot query for the recording.id at construction time.

        Returns ``None`` when the recording.db is absent (test fixtures
        with no real recording) or when the ``recording`` table is
        missing/empty. U4/U5 treat ``None`` as "skip network queries"
        rather than raising — recording survival > network signal.
        """
        try:
            with open_recording_db(self._db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM recording LIMIT 1"
                ).fetchone()
                return int(row[0]) if row is not None else None
        except (FileNotFoundError, sqlite3.DatabaseError):
            # DatabaseError is the parent of OperationalError; also covers
            # corrupt / zero-byte recording.db (raises DatabaseError directly).
            return None

    @property
    def status(self) -> str:
        """Thread-safe status string for Rich Live display."""
        with self._status_lock:
            return self._status

    @property
    def upload_warning(self) -> str | None:
        """Reason uploads were disabled, or None if uploads are healthy."""
        return self._upload_disabled_reason

    def _set_status(self, s: str) -> None:
        with self._status_lock:
            self._status = s

    def all_chunks_uploaded(self) -> bool:
        """True if every processed chunk has terminal status ``EMITTED``.

        Returns False if no chunks were processed at all — that means
        the video writer likely failed and no rotation events arrived.

        Explicit EMITTED-only — ``NETWORK_SKIPPED``,
        ``NETWORK_INCOMPLETE``, ``FAILED``, ``PENDING`` all block the
        gate. Per
        ``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``
        fix #4: disabled ≠ succeeded.
        """
        if not self._chunk_results:
            return False
        return all(s == ChunkStatus.EMITTED for s in self._chunk_results.values())

    def upload_summary(self) -> tuple[int, int]:
        """Return (n_emitted, n_total) from chunk results.

        Must only be called after stop() — _chunk_results is not
        thread-safe for concurrent reads.

        ``n_emitted`` counts ``EMITTED`` only — chunks that intentionally
        skipped network bodies (``NETWORK_SKIPPED``) or carried an
        incomplete window (``NETWORK_INCOMPLETE``) are NOT counted as
        uploaded. ``n_total`` counts every chunk regardless of status,
        including ``PENDING`` entries left by force-stop (Bug 2 fix).
        """
        n_total = len(self._chunk_results)
        n_emitted = sum(
            1 for s in self._chunk_results.values()
            if s == ChunkStatus.EMITTED
        )
        return n_emitted, n_total

    def reconcile_against_gcs(self) -> int:
        """Re-check GCS for chunks currently marked as ``FAILED``.

        _upload_chunk() returns False whenever any core file's PUT raises
        — but files that PUT before the failure remain in GCS. We
        re-request signed URLs for every failed chunk in a single batch;
        the server returns ``url=None`` for files it already has. When
        every core file on disk comes back ``None``, we flip
        ``_chunk_results[idx]`` from ``FAILED`` to ``EMITTED``.

        Iterates over ``FAILED`` chunks ONLY. ``NETWORK_SKIPPED`` and
        ``NETWORK_INCOMPLETE`` are terminal-non-EMITTED states that
        reconcile MUST NOT touch: their core files (video, audio,
        events.jsonl, manifest) DID upload successfully, so a
        ``!= EMITTED`` iteration would call ``request_signed_urls``,
        every core file would come back ``url=None``, the all() check
        would pass, and the chunk would silently relabel ``EMITTED`` —
        erasing the network-skip / network-incomplete signal. Same
        shape as
        ``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``
        Bug 4 routed through reconcile.

        ``PENDING`` entries are also excluded: a force-stopped chunk
        mid-upload may have partially landed in GCS, but signed-URL
        probes alone cannot distinguish a partial upload from a
        never-attempted one, so reconcile leaves PENDING as-is rather
        than risk a false EMITTED promotion.

        Must only be called after stop(). Returns the number of
        entries flipped from FAILED to EMITTED.
        """
        if not self._upload_enabled:
            return 0

        from screencap.upload import (
            FileInfo,
            _content_type,
            assert_uploadable,
            assert_video_masked,
            request_signed_urls,
        )

        all_file_infos: list[FileInfo] = []
        per_chunk_names: dict[int, list[str]] = {}
        for idx, status in self._chunk_results.items():
            # FAILED-only iteration — see docstring for why
            # NETWORK_SKIPPED / NETWORK_INCOMPLETE / PENDING are excluded.
            if status != ChunkStatus.FAILED:
                continue
            names: list[str] = []
            for f in self._collect_chunk_files(idx, None):
                if f["name"] in _NON_CORE_NAMES:
                    continue
                path: Path = f["path"]
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    continue
                fi = assert_uploadable(FileInfo(
                    name=f["name"], path=path,
                    content_type=_content_type(path), size=size,
                ))
                # SCR-126 Fix 1: gate the reconcile re-enumeration too (it applies
                # the masked-path switch via _collect_chunk_files); the frozen
                # decision is resolved once in __init__ as self._masked_video_upload.
                all_file_infos.append(
                    assert_video_masked(fi, masked_upload_on=self._masked_video_upload)
                )
                names.append(f["name"])
            if names:
                per_chunk_names[idx] = names

        if not all_file_infos:
            return 0

        try:
            urls, _ = request_signed_urls(self._recording_name, all_file_infos)
        except Exception as e:
            logger.debug(f"Reconcile: request_signed_urls failed: {e}")
            return 0

        flipped = 0
        for idx, names in per_chunk_names.items():
            # Server returns url=None for already-uploaded files; a missing
            # key means the server didn't confirm, so we stay conservative.
            if all(name in urls and urls[name] is None for name in names):
                self._chunk_results[idx] = ChunkStatus.EMITTED
                # U7: mirror the reconcile flip onto the on-disk ledger so the
                # confirmed-in-GCS state survives a crash and is visible to the
                # terminal-stage process. Best-effort (see _mirror_status_to_ledger).
                self._mirror_status_to_ledger(idx, ChunkStatus.EMITTED)
                flipped += 1
                logger.info(f"Reconciled chunk {idx}: all core files already in GCS")
        return flipped

    @property
    def was_force_stopped(self) -> bool:
        """True if stop() timed out and had to force-stop the thread.

        When True, _chunk_results may contain ChunkStatus.PENDING entries for
        chunks that were mid-processing when _stop_event fired. The sentinel
        gate predicate rejects PENDING explicitly, so those chunks are never
        counted as successfully uploaded.
        """
        return self._stop_event.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=False, name="chunk_processor",
        )
        self._thread.start()

    def stop(self, timeout: float = 300.0) -> None:
        """Send poison pill, wait for drain, then force-stop if needed."""
        if self._thread is None:
            return
        try:
            self._q.put({"type": "poison_pill"}, timeout=5)
        except Exception:
            logger.debug("ChunkProcessor already stopped, skipping shutdown signal")
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                f"ChunkProcessor did not finish in {timeout}s, signaling stop"
            )
            # Signal the thread to abort current work and exit
            self._stop_event.set()
            # Give it a short grace period to notice the stop event
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("ChunkProcessor thread still alive after stop signal")

    def _run(self) -> None:
        import queue as _queue_mod

        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                msg = self._q.get(timeout=2.0)
                consecutive_errors = 0  # reset on successful get
            except _queue_mod.Empty:
                # Normal timeout — queue has no messages yet, just keep waiting
                continue
            except (OSError, EOFError, BrokenPipeError):
                # Queue pipe is broken (child processes died) — exit loop
                logger.debug("ChunkProcessor queue broken, exiting")
                break
            except Exception:
                consecutive_errors += 1
                # If queue is consistently failing, it's dead — exit
                if consecutive_errors >= 5:
                    logger.debug("ChunkProcessor queue unresponsive, exiting")
                    break
                continue
            if msg.get("type") == "poison_pill":
                break
            # PENDING initialization contract: write the entry BEFORE
            # _process_chunk runs. Force-stop and exception paths now
            # both leave a non-missing entry that the EMITTED-only
            # gate predicate explicitly rejects — Bug 2 survivorship-
            # bias fix from chunk-upload-sentinel-gating-and-data-loss.md.
            idx = msg.get("completed_index")
            if idx is not None:
                self._chunk_results.setdefault(idx, ChunkStatus.PENDING)
            try:
                self._process_chunk(msg)
            except Exception:
                logger.exception(f"Chunk {idx} processing failed")
                # Defense in depth: if _process_chunk's finally block
                # already settled the status, respect it. Only fall
                # back to FAILED (or staged status) when the entry is
                # still PENDING — i.e., the exception fired before the
                # finally could write. ``idx`` here is the same value
                # set just above; idx is None means no PENDING entry was
                # created above; skip status settlement.
                if idx is None:
                    continue
                current = self._chunk_results.get(idx, ChunkStatus.PENDING)
                if current == ChunkStatus.PENDING:
                    # Written by U4 (network row export) / U5 (NetworkHealth overlap); always None in this branch.
                    pending = self._pending_network_status.pop(idx, None)
                    self._chunk_results[idx] = (
                        pending if pending is not None else ChunkStatus.FAILED
                    )

    def _get_ledger(self):
        """Lazily resolve the U5 ledger for this recording.db, or ``None``.

        Returns ``None`` (and latches ``_ledger_unavailable``) when the
        recording.db is absent or has no recording row — the same fixtures
        for which ``_lookup_recording_id`` returns ``None``. The agnostic
        stages run regardless; only the STAGED bookkeeping is skipped.
        """
        if self._ledger is not None:
            return self._ledger
        if self._ledger_unavailable:
            return None
        try:
            from screencap.pipeline_state import (
                PipelineLedger,
                ensure_pipeline_state_schema,
            )

            ensure_pipeline_state_schema(self._db_path)
            self._ledger = PipelineLedger(self._db_path)
        except Exception as e:
            # No recording.db / no recording row / schema failure — record
            # the absence once and fall back to artifact-only idempotency.
            logger.debug(f"Pipeline ledger unavailable, skipping STAGED bookkeeping: {e}")
            self._ledger_unavailable = True
            return None
        return self._ledger

    def _mirror_status_to_ledger(self, idx: int, status: "ChunkStatus") -> None:
        """Project a settled ``ChunkStatus`` onto the U1 ledger upload-state.

        Maps via :func:`pipeline_state.from_chunk_status`:
        ``EMITTED -> mark_uploaded``, ``NETWORK_SKIPPED -> mark_skipped``,
        ``FAILED``/``NETWORK_INCOMPLETE -> mark_failed``. Best-effort and
        non-fatal: a ledger write failure (no recording.db in a test fixture,
        a transient lock) must never crash chunk processing — the in-memory
        ``_chunk_results`` remains the live gate, and a missed ledger mirror is
        re-derived by the terminal stage's reconcile-from-GCS on (re)entry.
        """
        ledger = self._get_ledger()
        if ledger is None:
            return
        from screencap.pipeline_state import UploadState, from_chunk_status

        try:
            upload_state = from_chunk_status(status)
            if upload_state == UploadState.UPLOADED:
                ledger.mark_uploaded(idx)
            elif upload_state == UploadState.SKIPPED:
                ledger.mark_skipped(idx)
            elif upload_state == UploadState.FAILED:
                ledger.mark_failed(idx, detail=status.value)
            # UploadState.PENDING is never reached (caller filters PENDING).
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Chunk {idx}: ledger status mirror failed (non-fatal): {e}")

    def freeze_expected_chunks(self, count: int) -> None:
        """Freeze the ledger's ``chunks_expected`` to ``count`` at finalize.

        Called once at recording finalize (``collaborators.finalize_uploads``),
        when the chunk set is closed, so the U1 ledger's
        ``finalize_gate_satisfied`` and the U9 AE8 promotion guard
        (``detect_promotion_holes``) have a real frozen count to gate on.
        WITHOUT this, ``chunks_expected`` is never frozen on any production path
        and both gates become inert (the promotion guard silently passes every
        recording — the exact partial-upload data loss AE8 exists to refuse).

        Best-effort and non-fatal (mirrors ``_mirror_status_to_ledger``): a
        no-ledger fixture or a transient failure must never break finalize.
        ``freeze_chunks_expected`` is idempotent on the same count and refuses a
        conflicting refreeze (``LedgerError``) — both are swallowed here.
        """
        if count <= 0:
            return  # nothing to gate on (0-chunk / legacy single-file).
        ledger = self._get_ledger()
        if ledger is None:
            return
        try:
            ledger.freeze_chunks_expected(count)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"freeze_chunks_expected({count}) failed (non-fatal): {e}")

    def _run_agnostic_stages(self, idx, start_ts, end_ts):
        """Run the destination-agnostic stages (transcribe/export/manifest).

        Delegates ordering + ledger idempotency to the U5
        :class:`~screencap.pipeline_stages.PipelineStageRunner`. The injected
        steps wrap this processor's existing methods so the cloud-window-
        filtered ``_export_events`` (statically audited by
        ``test_privacy_filter_call_graph.py``) is unchanged — the runner's
        own code never sees the cloud filter (R4).

        Returns the produced ``StageArtifacts`` (re-derived on an already-
        STAGED chunk, so the caller always has the transcript path), or
        ``None`` if a ``_stop_event`` abort fired mid-stage (force-stop:
        leave the chunk PENDING). Stop-aborts surface as a private
        ``_StageAbort`` raised inside a step so the runner does not reach
        ``mark_staged`` — the chunk stays PENDING, never falsely STAGED.
        """
        from screencap.pipeline_stages import PipelineStageRunner

        # Seed a closed-set PENDING row before staging so the ledger reflects
        # this chunk even if a later stage fails (survivorship-bias fix).
        ledger = self._get_ledger()
        if ledger is not None:
            try:
                ledger.seed_chunk(idx)
            except Exception as e:
                logger.debug(f"Chunk {idx}: ledger seed failed (non-fatal): {e}")

        def _abort_if_stopping():
            if self._stop_event.is_set():
                raise _StageAbort()

        def _transcribe_step(i):
            self._set_status("Transcribing audio...")
            result = self._transcribe(i)
            _abort_if_stopping()
            return result

        def _export_step(i, s, e):
            # Flush writer buffers before reading events from the DB — the
            # export must see all rows the writers have produced for this
            # chunk window.
            self._set_status("Flushing buffers...")
            self._trigger_flush()
            _abort_if_stopping()
            self._set_status("Exporting events...")
            # Cloud-window-filter posture lives inside _export_events
            # (unchanged); the runner never sees it (R4).
            result = self._export_events(i, s, e)
            _abort_if_stopping()
            return result

        def _manifest_step(i, s, e):
            self._set_status("Generating manifest...")
            blocked_intervals = None
            if self._screen_filter is not None and hasattr(
                self._screen_filter, "get_blocked_intervals"
            ):
                try:
                    blocked_intervals = (
                        self._screen_filter.get_blocked_intervals(s, e) or None
                    )
                except Exception:
                    logger.warning(
                        f"Failed to get blocked_intervals for chunk {i}",
                        exc_info=True,
                    )
            try:
                return self._generate_manifest(
                    i, s, e, blocked_intervals=blocked_intervals
                )
            except Exception:
                logger.exception(f"Chunk {i}: manifest generation failed")
                # Remove any partially-written manifest so a later retry
                # (or screencap upload) doesn't ship a truncated file.
                (self._capture_dir / f"chunk_{i:04d}_manifest.json").unlink(
                    missing_ok=True
                )
                raise

        runner = PipelineStageRunner(
            self._capture_dir,
            transcribe=_transcribe_step,
            export_events=_export_step,
            manifest=_manifest_step,
            ledger=ledger,
        )

        # Already-STAGED short-circuit happens inside run_chunk (returns
        # None); re-derive the transcript path so the caller still has it.
        already_staged = runner.is_staged(idx)
        try:
            artifacts = runner.run_chunk(idx, start_ts, end_ts)
        except _StageAbort:
            # Force-stop mid-stage: leave the chunk PENDING (the runner did
            # not reach mark_staged).
            return None

        if artifacts is not None:
            return artifacts
        # run_chunk returned None → chunk was already STAGED. Re-derive the
        # transcript path (no stage re-run) so scrub/upload can proceed.
        if already_staged:
            transcript = self._capture_dir / f"transcript_{idx:04d}.txt"
            return _StageArtifacts(
                transcript=transcript if transcript.exists() else None,
                events=self._capture_dir / f"events_{idx:04d}.jsonl",
                manifest=self._capture_dir / f"chunk_{idx:04d}_manifest.json",
            )
        return None

    def _process_chunk(self, msg: dict) -> None:
        if self._auto_delete and not self._upload_enabled:
            raise RuntimeError(
                "invariant violated: auto_delete=True with uploads disabled — "
                "this would cause silent data loss"
            )

        idx = msg.get("completed_index")
        start_ts = msg["chunk_start_time"]
        end_ts = msg["rotation_time"]
        is_final = msg.get("type") == "final_chunk"

        # Final-assignment seam. The try/finally guarantees every exit
        # path lands on a coherent status: success → EMITTED, upload
        # failure → FAILED, staged network status → NETWORK_SKIPPED /
        # NETWORK_INCOMPLETE, early return from a _stop_event check →
        # leaves the eagerly-set PENDING entry alone (Bug 2
        # survivorship-bias fix — PENDING in the map blocks the gate
        # even when the chunk never reached upload).
        success = False
        reached_upload = False
        try:
            self._set_status("Waiting for audio...")

            # 1. Wait for audio ack
            self._wait_for_audio(idx, is_final=is_final)
            if self._stop_event.is_set():
                return

            # 2-4. Destination-agnostic stages (transcribe → export events →
            # manifest), delegated to the U5 ``PipelineStageRunner``. The
            # runner owns ordering + ledger idempotency (skip-if-STAGED);
            # this caller injects the concrete steps so the cloud-window-
            # filtered ``_export_events`` (and its inline
            # ``build_cloud_window_filter`` — the privacy posture the static
            # guard audits) stays exactly where it is. The runner's own code
            # carries no cloud knowledge (R4).
            artifacts = self._run_agnostic_stages(idx, start_ts, end_ts)
            if artifacts is None:
                # _run_agnostic_stages returns None only on a _stop_event
                # abort mid-stage — leave the eagerly-set PENDING entry.
                return
            transcript_path = artifacts.transcript

            # 5. Scrub text surfaces + mask screenshots when user opted in.
            if self._scrub_enabled and self._pipeline is not None:
                self._set_status("Redacting sensitive data...")
                self._scrub_chunk_files(idx, start_ts, end_ts, transcript_path)
                if self._stop_event.is_set():
                    return

            # 6. Cloud per-chunk op (mask → upload) behind the per-recording
            # terminal flock (SCR-125 U1). The flock is acquired per chunk and
            # released between chunks (non_blocking) so a concurrent manual
            # `screencap upload` / daemon resume interleaves: a lock-contended
            # chunk is DEFERRED (left PENDING) for the convergence pass, never
            # lost and never force-uploaded under contention.
            if self._upload_enabled:
                self._set_status("Uploading...")
                outcome = self._cloud_upload_chunk(
                    idx, start_ts, end_ts, transcript_path,
                )
                # DEFERRED (lock contended) leaves reached_upload False → the
                # chunk stays PENDING (the eagerly-set value) for the convergence
                # pass. Only a settled UPLOADED/FAILED outcome settles the status.
                if outcome is not _UploadOutcome.DEFERRED:
                    reached_upload = True
                    success = outcome is _UploadOutcome.UPLOADED
            else:
                # "Disabled" must not be conflated with "succeeded": when
                # privacy init failed _upload_disabled_reason is set, so
                # success stays False → FAILED → gate blocks → no stub.
                # Per chunk-upload-sentinel-gating-and-data-loss.md fix #4.
                reached_upload = True
                success = self._upload_disabled_reason is None
        finally:
            # Settle the final status on every exit path. Staging from
            # U4 (NETWORK_SKIPPED) and U5 (NETWORK_INCOMPLETE) takes
            # precedence over EMITTED so the network signal stays
            # visible at the sentinel gate.
            if idx is not None:
                # Written by U4 (network row export) / U5 (NetworkHealth overlap); always None in this branch.
                pending = self._pending_network_status.pop(idx, None)
                if pending is not None:
                    self._chunk_results[idx] = pending
                elif reached_upload:
                    self._chunk_results[idx] = (
                        ChunkStatus.EMITTED if success else ChunkStatus.FAILED
                    )
                # else: leave PENDING (eagerly set on rotation receipt).
                # This is the survivorship-bias fix — force-stop early
                # return now leaves a non-missing entry that the
                # EMITTED-only gate explicitly rejects.

                # U7: mirror the settled terminal status onto the on-disk U1
                # ledger so the terminal stage (a SEPARATE process) can
                # reconstruct correct upload state after a crash. The ledger is
                # the cross-process SOURCE OF TRUTH; ``_chunk_results`` remains
                # the in-process cache the live gating reads. Only settled
                # statuses are mirrored — a chunk left PENDING (force-stop)
                # stays PENDING in the ledger too (the closed-set gate rejects
                # it identically on disk and in RAM).
                settled = self._chunk_results.get(idx)
                if settled is not None and settled != ChunkStatus.PENDING:
                    self._mirror_status_to_ledger(idx, settled)

        # 7. Reclaim older confirmed chunks through the UNIFIED retention floor
        # (SCR-125 U2). Gated on this chunk being EMITTED and the frozen policy
        # being delete_after_upload (``self._auto_delete``). The floor enforces
        # the never-delete-without-a-fresh-remote-confirm rule per chunk
        # (prevention rule #3) — replacing the old delete-on-EMITTED-alone
        # bookkeeping — and keeps the 2 most-recent confirmed chunks on disk
        # (the same during-recording window as before).
        if (
            self._chunk_results.get(idx) == ChunkStatus.EMITTED
            and self._auto_delete
        ):
            freed = self._evict_old_chunks_through_floor(idx)
            self._total_freed += freed

        if self._total_freed > 0:
            n_done = sum(
                1 for s in self._chunk_results.values()
                if s == ChunkStatus.EMITTED
            )
            freed_str = _fmt_bytes(self._total_freed)
            self._set_status(f"{n_done} chunks done, {freed_str} freed")
        else:
            self._set_status("")

    def _trigger_flush(self) -> None:
        """Trigger writer processes to flush DB buffers before event export."""
        wait_for_writer_flush(
            flush_requested=self._flush_requested,
            flush_ack_counter=self._flush_ack_counter,
            flush_lock=self._flush_lock,
            stop_event=self._stop_event,
            logger=logger,
            caller="chunk_processor",
        )

    def _wait_for_audio(self, idx: int, *, is_final: bool = False) -> None:
        """Wait for audio process to confirm rotation/finalization."""
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._stop_event.is_set():
                return
            try:
                msg = self._audio_ack_q.get(timeout=2.0)
                completed = msg.get("completed_index")
                msg_type = msg.get("type")
                if completed == idx:
                    return
                if is_final and msg_type == "audio_final":
                    return
            except (OSError, EOFError, BrokenPipeError):
                # Audio process died, queue broken — proceed without ack
                logger.debug(f"Audio ack queue broken for chunk {idx}, proceeding")
                return
            except Exception:
                continue
        logger.warning(f"Audio ack for chunk {idx} not received in 60s, proceeding")
        self._set_status("Audio ack timeout — proceeding")

    def _transcribe(self, idx: int) -> Path | None:
        """Transcribe audio chunk quietly (no print output). Returns transcript path or None."""
        audio_path = self._capture_dir / f"audio_{idx:04d}.flac"
        if not audio_path.exists():
            logger.info(f"No audio file for chunk {idx}, skipping transcription")
            return None

        # Skip 0-byte audio files (empty final chunks)
        if audio_path.stat().st_size == 0:
            logger.info(f"Audio file for chunk {idx} is empty, skipping transcription")
            return None

        transcript_path = self._capture_dir / f"transcript_{idx:04d}.txt"
        transcript_json_path = self._capture_dir / f"transcript_{idx:04d}.json"

        if transcript_path.exists():
            return transcript_path

        if self._stop_event.is_set():
            return None

        # Try faster-whisper (quiet — no print output)
        try:
            from faster_whisper import WhisperModel

            logger.info(f"Transcribing chunk {idx} with faster-whisper...")
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments_iter, _info = model.transcribe(
                str(audio_path), word_timestamps=True,
            )
            segments = []
            full_text_parts = []
            for segment in segments_iter:
                if self._stop_event.is_set():
                    return None
                segments.append({
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text.strip(),
                })
                full_text_parts.append(segment.text)

            transcript = "".join(full_text_parts).strip()
            _save_transcript_quiet(
                transcript, segments, transcript_path, transcript_json_path,
            )
            logger.info(f"Saved transcript for chunk {idx} ({len(segments)} segments)")
            return transcript_path
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"faster-whisper failed for chunk {idx}: {e}")

        if self._stop_event.is_set():
            return None

        # Try openai-whisper (quiet)
        try:
            import whisper

            logger.info(f"Transcribing chunk {idx} with openai-whisper...")
            whisper_model = whisper.load_model("base")
            result = whisper_model.transcribe(
                str(audio_path), fp16=False, word_timestamps=True,
            )
            transcript = result.get("text", "").strip()
            segments = []
            for seg in result.get("segments", []):
                segments.append({
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"].strip(),
                })
            _save_transcript_quiet(
                transcript, segments, transcript_path, transcript_json_path,
            )
            logger.info(f"Saved transcript for chunk {idx} ({len(segments)} segments)")
            return transcript_path
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"openai-whisper failed for chunk {idx}: {e}")

        if self._stop_event.is_set():
            return None

        # Try OpenAI API
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            try:
                self._transcribe_api(
                    api_key, audio_path, transcript_path, transcript_json_path,
                )
                return transcript_path
            except Exception as e:
                logger.warning(f"OpenAI API transcription failed for chunk {idx}: {e}")

        logger.info(f"No transcription backend available for chunk {idx}")
        return None

    def _transcribe_api(
        self, api_key: str, audio_path: Path,
        transcript_path: Path, transcript_json_path: Path,
    ) -> None:
        """Transcribe using OpenAI Whisper API."""
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
            )
        transcript_path.write_text(resp.text)
        transcript_json_path.write_text(json.dumps(resp.model_dump(), indent=2))

    def _export_events(self, idx: int, start_ts: float, end_ts: float) -> Path:
        """Export events from recording.db as JSONL for this chunk's time range.

        Thin wrapper around :func:`screencap.export.export_chunk_events`
        — the single seam every export caller (CLI, chunk processor,
        recovery) goes through. The chunk processor's only concerns
        here are: build the cloud window filter, run the export,
        atomically write JSONL with a ``format_version: 2`` ``_meta``
        header.

        Per R11 the chunk processor no longer drops ``mouse.move`` events
        — the scrubber is the sole point at which moves are dropped for
        sensitive intervals (Unit 2).

        Cloud privacy posture: the window filter is constructed
        unconditionally via ``build_cloud_window_filter`` (returns
        ``None`` for non-cloud, the cloud-mode filter for cloud).

        Chunk boundary note: the processing pipeline is stateful (click
        merging, typing aggregation). A mouse.down at chunk end may stay
        unmerged — accepted limitation (orphan events at boundaries).
        """
        from screencap.export import export_chunk_events
        from screencap.exporter import build_export_metadata, write_events_jsonl
        from screencap.privacy.filter import build_cloud_window_filter

        jsonl_path = self._capture_dir / f"events_{idx:04d}.jsonl"
        if jsonl_path.exists():
            return jsonl_path

        try:
            # Build the cloud filter inline at the call site so the
            # privacy posture is visible — and statically auditable
            # (see test_privacy_filter_call_graph.py) — at every
            # cloud-capable export_chunk_events caller.
            events = export_chunk_events(
                self._capture_dir,
                start_ts,
                end_ts,
                window_filter=build_cloud_window_filter(
                    self._cloud_intent,
                    privacy_mode=self._privacy_mode,
                    capture_dir=self._capture_dir,
                ),
            )
            meta = build_export_metadata(exclude_moves=False)
            count = write_events_jsonl(jsonl_path, events, meta)
            logger.info(f"Exported {count} events to {jsonl_path.name}")
        except Exception:
            # Defense-in-depth: write_events_jsonl already cleans up its
            # own .tmp; this covers the case where the iterator setup
            # raises before the helper opens its .tmp.
            tmp_path = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
            tmp_path.unlink(missing_ok=True)
            raise

        return jsonl_path

    def _generate_manifest(
        self, idx: int, start_ts: float, end_ts: float,
        blocked_intervals: list[dict] | None = None,
    ) -> Path:
        """Generate task manifest for this chunk."""
        from screencap.task_manifest import generate_manifest

        return generate_manifest(
            self._capture_dir, idx, start_ts, end_ts,
            rest_threshold=self._rest_threshold,
            blocked_intervals=blocked_intervals,
            segmentation_mode=self._segmentation_mode,
        )

    def _scrub_chunk_files(
        self, idx: int, start_ts: float, end_ts: float,
        transcript_path: Path | None,
    ) -> None:
        """Scrub text surfaces + mask screenshots for a single chunk.

        Delegates to ``Scrubber.run_chunk()`` so the load-bearing step order
        lives in one place; the chunk processor only owns lifecycle concerns
        (which chunks to scrub, when, with what masking config).
        """
        from screencap.scrubber import Scrubber

        scrubber = Scrubber(
            self._capture_dir,
            pipeline=self._pipeline,
            anonymizer=self._anonymizer,
            evaluator=self._masking_evaluator,
            classifier=self._masking_classifier,
            pixel_ratio=self._masking_pixel_ratio,
        )
        scrub_result = scrubber.run_chunk(
            idx=idx,
            start_ts=start_ts,
            end_ts=end_ts,
            transcript_path=transcript_path,
        )

        if scrub_result.audit_entries:
            logger.info(
                f"Chunk {idx}: scrubbed with {len(scrub_result.audit_entries)} audit entries"
            )

    def _collect_chunk_files(self, idx: int, transcript_path: Path | None) -> list[dict]:
        """Collect files belonging to this chunk for upload.

        Cloud-intent recordings include video (with placeholder frames for
        blocked intervals) and audio alongside text files.
        Skips 0-byte files and files renamed to .scrub_failed.

        SCR-125 masked-path-switch (R-SCR125-A): when the FROZEN
        ``masked_video_upload`` flag is ON for a cloud recording, the ``.mp4``
        slot resolves to the MASKED copy under ``<name>-scrubbed/masked_video/``
        and the rich SOURCE ``.mp4`` is NEVER added to the upload set — so the
        live path can never ship unmasked rich video. The GCS object NAME stays
        ``chunk_{idx:04d}.mp4`` (only the local source path changes), so the
        cloud key is unchanged. Flag OFF (this milestone): the source ``.mp4`` is
        uploaded exactly as today.
        """
        files = []
        video_name = f"chunk_{idx:04d}.mp4"
        if self._cloud_intent and self._masked_video_upload:
            from screencap.scrubber import masked_video_dir

            video_path = masked_video_dir(self._scrubbed_dir) / video_name
        else:
            video_path = self._capture_dir / video_name
        # (name -> local path) pairs. The video slot may point outside
        # capture_dir (the masked copy); every other slot is a source file.
        specs: list[tuple[str, Path]] = [
            (video_name, video_path),
            (f"audio_{idx:04d}.flac", self._capture_dir / f"audio_{idx:04d}.flac"),
            (f"events_{idx:04d}.jsonl", self._capture_dir / f"events_{idx:04d}.jsonl"),
            (f"chunk_{idx:04d}_manifest.json", self._capture_dir / f"chunk_{idx:04d}_manifest.json"),
        ]
        if transcript_path and transcript_path.exists():
            specs.append((transcript_path.name, transcript_path))

        for name, path in specs:
            if path.exists() and path.stat().st_size > 0:
                files.append({"name": name, "path": path})
            elif path.exists() and path.stat().st_size == 0:
                logger.info(f"Skipping 0-byte file: {name}")

        # Upload _unlisted marker on first chunk when recording is hidden
        if idx == 0 and not self._show_on_website:
            marker_path = self._capture_dir / "_unlisted"
            marker_path.touch(exist_ok=True)
            files.append({"name": "_unlisted", "path": marker_path})

        return files

    def _cloud_upload_chunk(
        self, idx: int, start_ts: float, end_ts: float,
        transcript_path: Path | None,
    ) -> "_UploadOutcome":
        """Mask (frozen-flag gated) → collect → upload one chunk, behind the flock.

        SCR-125 U1: the live per-chunk cloud op acquires the per-recording
        ``terminal_lock`` ``non_blocking`` and releases it between chunks, so it
        cannot stall the capture-adjacent thread and a concurrent manual
        ``screencap upload`` / daemon resume can interleave. On contention the
        chunk is ``DEFERRED`` (left PENDING for the convergence pass — the
        intended outcome, NOT a lost upload). When the FROZEN
        ``masked_video_upload`` flag is ON the chunk's video is masked through the
        SHARED seam first and the upload slot is switched to the masked copy
        (R-SCR125-A); a mask FAILED is fail-closed (no upload).
        """
        from screencap.terminal_stage import TerminalStageBusy, terminal_lock

        try:
            with terminal_lock(self._recording_name, non_blocking=True):
                if self._cloud_intent and self._masked_video_upload:
                    from screencap.pipeline_chunk_ops import mask_chunk_for_cloud

                    cls = mask_chunk_for_cloud(
                        self._capture_dir, self._scrubbed_dir, idx,
                        start_ts=start_ts, end_ts=end_ts, enabled=True,
                        ledger=self._get_ledger(), db_path=self._db_path,
                    )
                    if cls.failed:
                        # Fail-closed: never upload an un-maskable chunk.
                        return _UploadOutcome.FAILED
                files = self._collect_chunk_files(idx, transcript_path)
                if not files:
                    logger.warning(f"No files found for chunk {idx}")
                    return _UploadOutcome.FAILED
                return (
                    _UploadOutcome.UPLOADED
                    if self._upload_chunk(idx, files)
                    else _UploadOutcome.FAILED
                )
        except TerminalStageBusy:
            logger.info(
                "chunk %d: terminal lock contended (manual upload / daemon "
                "resume in progress); deferring to the convergence pass", idx,
            )
            return _UploadOutcome.DEFERRED

    def _upload_chunk(self, idx: int, files: list[dict]) -> bool:
        """Upload chunk files silently. Returns True if core files succeeded.

        Transcript upload failure is non-fatal — the chunk is still
        considered uploaded if video/audio/events succeed.
        Retries once after a 5-second backoff on failure.
        """
        for attempt in range(2):
            try:
                return upload_chunk_files(
                    self._recording_name, files, self._capture_dir,
                )
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"Chunk {idx} upload attempt 1 failed: {e}, retrying in 5s...")
                    time.sleep(5)
                else:
                    logger.error(f"Chunk {idx} upload failed after retry: {e}")
        return False

    def _evict_old_chunks_through_floor(self, idx: int) -> int:
        """Reclaim older confirmed chunks via the unified retention floor (U2).

        Routes the during-recording reclaim through
        :func:`screencap.retention.evict_recording` so a chunk's rich local copy
        is deleted ONLY when it is ledger-``UPLOADED`` AND a FRESH remote
        re-confirm succeeds NOW (prevention rule #3) — replacing the old
        delete-on-``EMITTED``-alone bookkeeping. ``keep_recent=2`` preserves the
        same during-recording window the live path always kept; the floor's
        candidate predicate already excludes ``SKIPPED`` / ``FAILED`` / in-flight
        chunks (only ``UPLOADED`` is evictable), so the network-skipped /
        incomplete survival invariant holds via the ledger.

        Acquired under the per-recording terminal flock (``non_blocking``,
        released between chunks — the same contract as the U1 upload op) so it
        cannot race a concurrent terminal run (AE12). A lock-contended pass
        simply defers — eviction is idempotent + resumable, so the next pass /
        the finalize convergence reclaims. Never raises into recording: any
        failure leaves more local files than retention wanted, never less.
        """
        from screencap.terminal_stage import TerminalStageBusy, terminal_lock

        ledger = self._get_ledger()
        if ledger is None:
            return 0
        try:
            with terminal_lock(self._recording_name, non_blocking=True):
                from screencap.retention import evict_recording

                report = evict_recording(
                    self._capture_dir,
                    ledger=ledger,
                    keep_recent=2,
                    during_recording=True,
                )
                for ev in report.evicted_indices:
                    logger.info("Evicted chunk %d local media (retention floor)", ev)
                return report.bytes_freed
        except TerminalStageBusy:
            logger.debug(
                "chunk %d: terminal lock contended; deferring eviction to the "
                "next pass / finalize convergence", idx,
            )
            return 0
        except Exception as exc:  # noqa: BLE001 — eviction never breaks recording
            logger.warning(
                "chunk %d: retention eviction failed (non-fatal): %s", idx, exc,
            )
            return 0


def _save_transcript_quiet(
    transcript: str,
    segments: list[dict],
    transcript_path: Path,
    transcript_json_path: Path,
) -> None:
    """Save transcript to files without any print output."""
    transcript_path.write_text(transcript, encoding="utf-8")
    transcript_json_path.write_text(
        json.dumps({"text": transcript, "segments": segments}, indent=2),
        encoding="utf-8",
    )


# Marker files that are not part of the recording payload and must never
# fail the chunk if the server rejects them (e.g. legacy Cloud Functions
# whose filename regex forbade a leading underscore).
_NON_CORE_NAMES = frozenset({"_unlisted"})


def upload_chunk_files(
    recording_name: str, files: list[dict], capture_dir: Path,
) -> bool:
    """Upload chunk files silently (no progress bars).

    Returns True if all core files uploaded. Transcript failures are
    logged but treated as non-fatal.
    """
    from screencap.pipeline_chunk_ops import get_frozen_masked_video_upload
    from screencap.upload import (
        FileInfo,
        _content_type,
        assert_uploadable,
        assert_video_masked,
        request_signed_urls,
    )

    # SCR-126 Fix 1: resolve the FROZEN masked-video decision once from the source
    # recording dir (capture_dir) and gate every chunk video at the enqueue
    # boundary, so a masked-path-switch regression in any caller fails loud here.
    masked_on = get_frozen_masked_video_upload(capture_dir)

    file_infos = []
    for f in files:
        p = Path(f["path"])
        # Defense-in-depth (R8): run the live chunk seam through the same hard
        # gate the single upload seam uses, so a raw local-only artifact
        # (recording.db &c.) reaching this enqueue fails loud instead of
        # shipping silently. No-op for the chunk files this path actually sends.
        fi = assert_uploadable(FileInfo(
            name=f["name"],
            path=p,
            content_type=_content_type(p),
            size=p.stat().st_size,
        ))
        # SCR-126 Fix 1: with masked upload ON, a cloud chunk_*.mp4 must be the
        # masked copy under masked_video/ — never the rich source.
        file_infos.append(assert_video_masked(fi, masked_upload_on=masked_on))

    if not file_infos:
        return True

    try:
        urls, gcs_prefix = request_signed_urls(recording_name, file_infos)
    except Exception as e:
        logger.error(f"Failed to get signed URLs: {e}")
        return False

    core_ok = True
    for fi in file_infos:
        is_core = (
            not fi.name.startswith("transcript")
            and fi.name not in _NON_CORE_NAMES
        )
        if fi.name not in urls:
            # Server didn't return a URL at all — file was rejected/unknown
            if is_core:
                logger.error(f"Server returned no URL for core file {fi.name}")
                core_ok = False
            else:
                logger.warning(f"Server did not accept non-core file {fi.name}")
            continue
        url = urls[fi.name]
        if url is None:
            continue  # server confirms already uploaded
        try:
            _upload_single(fi, url)
        except Exception as e:
            if not is_core:
                logger.warning(f"Non-fatal: failed to upload {fi.name}: {e}")
            else:
                logger.error(f"Failed to upload {fi.name}: {e}")
                core_ok = False

    # Write per-chunk status if core files succeeded
    if core_ok:
        status_file = capture_dir / f".chunk_{files[0]['name'].split('.')[0]}_status.json"
        try:
            status_file.write_text(json.dumps({
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "files": [f["name"] for f in files],
            }))
        except Exception:
            pass

    return core_ok


def _upload_single(fi, signed_url: str) -> None:
    """Upload a single file without progress tracking."""
    import requests

    with open(fi.path, "rb") as fh:
        resp = requests.put(
            signed_url,
            data=fh,
            headers={
                "Content-Type": fi.content_type,
                "Content-Length": str(fi.size),
            },
            timeout=(10, 600),
        )
    resp.raise_for_status()


def checkpoint_and_upload_db(
    capture_dir: Path, recording_name: str, *, cloud_intent: bool = False,
) -> bool:
    """WAL-checkpoint ``recording.db``. Never uploads it — it is local-only (R8).

    U2 retired the upload of the raw DB. ``recording.db`` is the local-only
    artifact by rule: it carries unscrubbed PII *and* (per U1) the
    ``pipeline_chunk_state`` ledger, so it must never leave the machine for ANY
    destination. The R8 exclusion now lives at the single upload seam
    (``upload.list_recording_files`` / ``upload.assert_uploadable``), retiring the
    old ``if cloud_intent: return True`` structural skip that was one of three
    scattered enforcement sites.

    The WAL checkpoint itself is still performed: ``finalize_uploads`` (the sole
    caller) and other code rely on a clean, checkpointed ``recording.db`` on disk
    — only the *upload* of the DB is removed. ``recording_name`` is kept in the
    signature for call-site stability (and the not-yet-rewritten U7 terminal
    stage); it is unused now that nothing is uploaded.

    Returns True (the checkpoint is best-effort and never blocks finalize); a
    missing DB (legacy/migrated recording) is a clean no-op, not an error.
    """
    db_path = capture_dir / "recording.db"
    if not db_path.exists():
        # Legacy / migrated recording with no recording.db — nothing to
        # checkpoint, and there is by definition no raw DB to keep local.
        return True

    # Checkpoint — fold WAL into main DB so the on-disk DB is clean for any
    # local consumer (review, catalog, post-hoc scrubbed-copy upload).
    try:
        with open_recording_db(db_path, read_only=False) as conn:
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0] > 0:
            logger.warning(f"WAL checkpoint: {result[0]} blocked pages")
    except Exception as e:
        logger.warning(f"WAL checkpoint failed: {e}")

    # The raw recording.db is NEVER uploaded (R8) — it stays local for the
    # post-hoc scrubbed-copy upload path (`screencap upload` scrubs a sibling
    # `<name>-scrubbed` dir). Structured cloud data derives only from scrubbed
    # exports (events JSONL, transcript, manifest), never the raw DB.
    logger.debug("recording.db checkpointed and kept local-only (never uploaded; R8)")
    return True


def _build_sentinel_data(
    recording_name: str,
    stop_reason: str,
    chunks_expected: int,
    *,
    show_on_website: bool = True,
) -> dict:
    """Build sentinel dict for recording_complete.json."""
    import uuid
    from datetime import datetime, timezone

    from screencap import __version__

    return {
        "version": 1,
        "recording_name": recording_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "stop_reason": stop_reason,
        "screencap_version": __version__,
        "chunks_expected": chunks_expected,
        "sentinel_id": str(uuid.uuid4()),
        "show_on_website": show_on_website,
    }


def upload_sentinel(
    capture_dir: Path,
    recording_name: str,
    *,
    stop_reason: str = "graceful",
    chunks_expected: int = 0,
    show_on_website: bool = True,
) -> bool:
    """Create and upload recording_complete.json sentinel to trigger stitching.

    Always creates the local file even if upload fails (for recovery via
    ``screencap upload``).  Returns True if upload succeeded.
    """
    from screencap.upload import FileInfo, request_signed_urls

    sentinel_data = _build_sentinel_data(
        recording_name, stop_reason, chunks_expected,
        show_on_website=show_on_website,
    )
    sentinel_path = capture_dir / "recording_complete.json"

    # Write locally (atomic: tmp + rename)
    tmp_path = sentinel_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(sentinel_data, indent=2))
        tmp_path.rename(sentinel_path)
    except Exception as e:
        logger.warning(f"Failed to write local sentinel: {e}")
        # Try non-atomic fallback
        try:
            sentinel_path.write_text(json.dumps(sentinel_data, indent=2))
        except Exception:
            return False

    # Upload
    fi = FileInfo(
        name="recording_complete.json",
        path=sentinel_path,
        content_type="application/json",
        size=sentinel_path.stat().st_size,
    )
    try:
        urls, _ = request_signed_urls(recording_name, [fi])
        if "recording_complete.json" not in urls:
            logger.error("Server returned no URL for recording_complete.json")
            return False
        url = urls["recording_complete.json"]
        if url:
            _upload_single(fi, url)
        return True
    except Exception as e:
        logger.error(f"Failed to upload sentinel: {e}")
        return False


def stub_recording(recording_dir: Path) -> list[str]:
    """Delete media files from a fully-uploaded recording, keeping metadata."""
    keep_patterns = {
        "recording.db", ".upload_status.json",
        "session_summary.json", "profiling.json",
        "recording_complete.json",
    }
    keep_prefixes = (".chunk_", "chunk_")
    deleted = []

    for p in sorted(recording_dir.iterdir()):
        if not p.is_file():
            continue
        if p.name in keep_patterns:
            continue
        if any(p.name.startswith(pf) for pf in keep_prefixes) and p.name.endswith(".json"):
            continue
        if p.name.startswith("transcript") and p.name.endswith(".txt"):
            continue
        if p.suffix in (".mp4", ".flac", ".jsonl", ".png", ".jpg"):
            try:
                p.unlink()
                deleted.append(p.name)
            except OSError as e:
                logger.warning(f"Failed to delete {p.name}: {e}")

    # Delete screenshots subdirectory
    screenshots_dir = recording_dir / "screenshots"
    if screenshots_dir.is_dir():
        for p in screenshots_dir.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                    deleted.append(f"screenshots/{p.name}")
                except OSError:
                    pass
        try:
            screenshots_dir.rmdir()
        except OSError:
            pass

    return deleted


def _fmt_bytes(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"
