"""Shared per-chunk cloud scrub/mask seam (SCR-125 U1).

The single place the per-chunk cloud video mask + its ledger bookkeeping live,
so the LIVE ``chunk_processor`` upload (during recording) and the post-hoc
``terminal_stage`` convergence apply byte-identical masking and ledger marks for
a given chunk. Before this seam each owned its own copy of the mask loop, which
is exactly the divergent-second-uploader risk the unified pipeline exists to
remove (R1/R5).

Two pieces:

* :func:`get_frozen_masked_video_upload` — read the FROZEN per-recording
  masked-video-upload decision from ``.recording_intent`` (R-SCR125-A). Both
  callers gate on this, never the mutable global, so a mid-recording flip can
  never make capture-time blocking and upload-time masking disagree (the
  rich-video leak window). Legacy recordings with no frozen field fall back to
  the global.

* :func:`mask_chunk_for_cloud` — the per-chunk body: produce the masked cloud
  copy for one chunk and map the outcome onto the ledger (ok -> ``mark_scrubbed``;
  fail -> ``mark_failed``; fail-closed on any exception). Gated on the frozen
  ``enabled`` value the caller resolves once.

This module is a leaf consumer (it imports ``scrubber`` / ``pipeline_state`` /
``catalog`` deferred), so neither ``chunk_processor`` nor ``terminal_stage``
gains a new module-level import cycle.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from screencap.pipeline_state import PipelineLedger

logger = logging.getLogger(__name__)

__all__ = [
    "MaskClassification",
    "get_frozen_cloud_e2ee",
    "get_frozen_masked_video_upload",
    "mask_chunk_for_cloud",
]


def get_frozen_masked_video_upload(recording_dir: Path) -> bool:
    """Resolve the FROZEN masked-video-upload decision for a recording.

    Reads the value frozen into ``.recording_intent`` at recording start
    (``engine/lock_policy._write_identity_files``). Returns that bool when
    present; falls back to the mutable global
    (``config.get_masked_video_upload_enabled()``) ONLY for a legacy recording
    whose intent predates SCR-125 (no frozen field). The fallback is a single
    read at resolve time — the caller is expected to resolve once and reuse, so
    a later global flip cannot change a recording's behavior mid-flight.
    """
    from screencap.catalog import read_masked_video_upload

    frozen = read_masked_video_upload(Path(recording_dir))
    if frozen is not None:
        return frozen
    from screencap.config import get_masked_video_upload_enabled

    return get_masked_video_upload_enabled()


def get_frozen_cloud_e2ee(recording_dir: Path) -> bool:
    """Resolve the FROZEN cloud-E2EE decision for a recording (SCR-220 KTD-4).

    Reads the value frozen into ``.recording_intent`` at recording start
    (``engine/lock_policy._write_identity_files``). Every upload seam derives
    its encrypt decision from this bit, never the live flag — frozen-on
    encrypts (failing closed without a key) regardless of the current flag
    state; the flag's only role is seeding the intent at start.

    Unlike :func:`get_frozen_masked_video_upload` there is NO global fallback:
    a missing/old-schema intent (pre-SCR-220) is frozen-off — plaintext per
    today's path, never an error — because those recordings genuinely ran
    without E2EE and must not report (or require) an encrypted upload.
    """
    from screencap.catalog import read_cloud_e2ee

    return bool(read_cloud_e2ee(Path(recording_dir)))


@dataclass
class MaskClassification:
    """Per-chunk outcome of :func:`mask_chunk_for_cloud`.

    ``masked`` — a masked cloud copy was produced (frozen flag ON, mask ok);
    ``masked_path`` points at it. ``failed`` — masking failed (fail-closed: the
    chunk is ``mark_failed`` in the ledger, blocking the sentinel + eviction).
    When neither is set the masker did nothing by design (frozen flag OFF) — the
    capture-blocked source chunk IS the cloud copy.
    """

    masked: bool = False
    failed: bool = False
    masked_path: Path | None = None
    reason: str | None = None


def mask_chunk_for_cloud(
    recording_dir: Path,
    scrubbed_dir: Path,
    chunk_idx: int,
    *,
    start_ts: float,
    end_ts: float,
    enabled: bool,
    ledger: "PipelineLedger | None" = None,
    db_path: Path | None = None,
    pixel_ratio: float = 2.0,
    classifier: object | None = None,
    evaluator: object | None = None,
) -> MaskClassification:
    """Produce the masked cloud copy for one chunk + map it onto the ledger.

    ``enabled`` is the FROZEN per-recording masked-video-upload decision (the
    caller resolves it once via :func:`get_frozen_masked_video_upload`). When
    OFF this is a no-op returning an empty :class:`MaskClassification` — no
    masker invoked, no ledger change, the capture-blocked source chunk is the
    cloud copy (the byte-for-byte-today guarantee).

    When ON it masks ``recording_dir/chunk_{idx:04d}.mp4`` over its absolute
    frame span into ``scrubbed_dir/masked_video/`` and:

    * ok      -> ``masked=True`` + ``mark_scrubbed`` (unless the chunk already
      advanced past it to UPLOADED/EVICTED — never downgrade a confirmed chunk);
    * FAILED  -> ``failed=True`` + ``mark_failed`` (blocks the sentinel);
    * raises  -> ``failed=True`` + ``mark_failed`` (fail-closed on any error).

    The ``chunk_start_abs`` passed to the masker is the chunk's OWN absolute
    start (``start_ts``): each chunk mp4's PTS restarts near 0, so the masker
    maps frames as ``start_ts + frame_pts``; passing the recording base would
    mis-align every chunk idx>=1.
    """
    cls = MaskClassification()
    if not enabled:
        # Frozen flag OFF: the conservative posture. No masked copy; the
        # capture-blocked source chunk is the cloud copy (no ledger change —
        # the agnostic STAGED state + the upload confirmation drive the gate).
        return cls

    from screencap.scrubber import mask_video_chunk_for_cloud, masked_video_dir

    recording_dir = Path(recording_dir)
    scrubbed_dir = Path(scrubbed_dir)
    db_path = db_path if db_path is not None else recording_dir / "recording.db"
    chunk_path = recording_dir / f"chunk_{chunk_idx:04d}.mp4"

    try:
        outcome = mask_video_chunk_for_cloud(
            chunk_path, db_path, scrubbed_dir,
            chunk_index=chunk_idx, start_ts=start_ts, end_ts=end_ts,
            chunk_start_abs=start_ts,
            pixel_ratio=pixel_ratio, classifier=classifier, evaluator=evaluator,
            # Pass the frozen decision so the masker does NOT re-read the mutable
            # global (R-SCR125-A: the frozen value is the single gate).
            enabled=True,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed on any error
        logger.error("video mask raised for chunk %d: %s", chunk_idx, exc)
        cls.failed = True
        cls.reason = f"video_mask error: {exc}"
        _mark_failed(ledger, chunk_idx, cls.reason)
        return cls

    if outcome is None:
        # enabled=True was passed, so this only happens if the masker itself
        # decided no copy was warranted — treat as no-op (no masked copy).
        return cls
    if outcome.ok:
        cls.masked = True
        cls.masked_path = masked_video_dir(scrubbed_dir) / f"chunk_{chunk_idx:04d}.mp4"
        _mark_scrubbed_guarded(ledger, chunk_idx)
    else:
        cls.failed = True
        cls.reason = outcome.reason or "video_mask FAILED"
        _mark_failed(ledger, chunk_idx, cls.reason)
    return cls


def _mark_scrubbed_guarded(ledger: "PipelineLedger | None", idx: int) -> None:
    """``mark_scrubbed`` unless the chunk already advanced to UPLOADED/EVICTED.

    Do NOT downgrade a chunk a prior reconcile already advanced: ``mark_scrubbed``
    sets lifecycle=SCRUBBED, and the later upload pass skips an already-UPLOADED
    ``upload_state``, so the row would be stuck SCRUBBED and never satisfy the
    finalize gate. Best-effort (a ledger write failure must never crash the
    caller — the upload confirmation re-derives state on re-entry).
    """
    if ledger is None:
        return
    with contextlib.suppress(Exception):
        from screencap.pipeline_state import Lifecycle

        row = ledger.get_chunk(idx)
        if row is None or row.lifecycle not in (Lifecycle.UPLOADED, Lifecycle.EVICTED):
            ledger.mark_scrubbed(idx)


def _mark_failed(ledger: "PipelineLedger | None", idx: int, detail: str) -> None:
    if ledger is None:
        return
    with contextlib.suppress(Exception):
        ledger.mark_failed(idx, detail=detail)
