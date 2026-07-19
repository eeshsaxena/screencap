"""Destination-agnostic per-chunk stage runner (U5).

The three stages every recording produces *regardless of destination* —
**transcribe → export events → manifest** — run here, exactly once per
chunk, ledger-driven and idempotent (R4). This module's own code carries
**no local-vs-cloud knowledge**: no ``PrivacyMode.PUBLIC`` forcing and no
cloud window filter. It writes the rich/canonical on-disk artifacts and
marks the chunk ``STAGED`` in the U1 ledger when all three are done.

Why the cloud window filter is NOT here
---------------------------------------
The plan moves cloud window-filtering OUT of the agnostic export and INTO
the terminal stage (U7), with the static-call-graph guard
(``tests/test_privacy_filter_call_graph.py``) replaced atomically in U7.
U5 lands *before* U7, so this unit must NOT open a window where a
cloud-bound recording loses its event-export window filter. The runner
therefore does **not** call ``export_chunk_events`` itself. Instead the
caller injects an ``export_events`` step:

  - ``chunk_processor`` injects its existing ``_export_events`` (which still
    builds ``build_cloud_window_filter(...)`` inline at the call site —
    unchanged, so cloud recordings keep their filtered export exactly as
    today and the guard stays green).
  - Local / test callers inject a plain export with ``window_filter=None``.

The runner only orchestrates ordering + idempotency + the ledger; the
*content* of the export (filtered or not) is the injected step's concern.
This keeps the runner destination-agnostic in its own code (R4) while the
re-homing of the filter to the terminal stage is left for U7.

Idempotency: two gates, belt-and-suspenders
--------------------------------------------
A chunk's stages run at most once across re-runs and crashes via BOTH:

  1. **On-disk artifact existence** (the existing pattern in
     ``chunk_processor._transcribe`` / ``_export_events``): each step
     early-returns if its output file already exists, so a re-run never
     re-pays the expensive cost (e.g. re-transcription).
  2. **The ledger** (U1): if the chunk's row is already ``STAGED`` (its
     ``stages_state`` is ``DONE``), ``run_chunk`` short-circuits before
     touching any step. ``mark_staged`` is written only AFTER all three
     artifacts exist — a partial/failed run leaves the chunk pre-STAGED so
     a later re-run completes it (no false "done").

Partial-failure contract
-------------------------
If any step raises, the chunk is NOT marked ``STAGED``. The ledger row
stays at its prior (pre-STAGED) lifecycle so the terminal stage / a re-run
re-attempts it. The exception propagates so the caller can record a
``FAILED`` upload state if it wishes — the runner does not swallow it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from screencap.pipeline_state import Lifecycle, PipelineLedger, StageState

logger = logging.getLogger(__name__)

__all__ = [
    "StageArtifacts",
    "PipelineStageRunner",
]

# A transcribe step: (chunk_index) -> transcript Path or None.
TranscribeStep = Callable[[int], "Path | None"]
# An export-events step: (chunk_index, start_ts, end_ts) -> events JSONL Path.
# The caller owns the privacy posture of this step (cloud filter or not).
ExportEventsStep = Callable[[int, float, float], "Path"]
# A manifest step: (chunk_index, start_ts, end_ts) -> manifest Path.
ManifestStep = Callable[[int, float, float], "Path"]


@dataclass(frozen=True)
class StageArtifacts:
    """The on-disk artifacts a single chunk's agnostic stages produced.

    ``transcript`` is ``None`` when the chunk had no (or empty) audio — a
    legitimate outcome, not a failure. ``events`` and ``manifest`` are
    always produced for a successful run.
    """

    transcript: Path | None
    events: Path
    manifest: Path


class PipelineStageRunner:
    """Run the destination-agnostic stages for one chunk, once, idempotently.

    Construct with a ``capture_dir`` (whose ``recording.db`` already has the
    U1 ledger schema) and the three injected steps. The steps encapsulate
    *how* each artifact is produced (e.g. which transcription backend, and
    crucially whether the event export applies a cloud window filter); the
    runner only owns ordering, idempotency, and the ledger transition.

    The runner takes a ``PipelineLedger`` so it shares the caller's instance
    (one connection-discipline owner per ``recording.db``); pass ``None`` to
    skip ledger bookkeeping entirely (pure artifact production, used by
    callers that drive the ledger themselves).
    """

    def __init__(
        self,
        capture_dir: Path | str,
        *,
        transcribe: TranscribeStep,
        export_events: ExportEventsStep,
        manifest: ManifestStep,
        ledger: PipelineLedger | None = None,
    ) -> None:
        self._capture_dir = Path(capture_dir)
        self._transcribe = transcribe
        self._export_events = export_events
        self._manifest = manifest
        self._ledger = ledger

    # ------------------------------------------------------------------
    # Idempotency gate.
    # ------------------------------------------------------------------

    def is_staged(self, chunk_index: int) -> bool:
        """True if the ledger already marks this chunk's stages DONE.

        Returns False when there is no ledger, no row for the chunk, or the
        row's ``stages_state`` is not ``DONE`` — any of which means the
        stages still need (re-)running.
        """
        if self._ledger is None:
            return False
        row = self._ledger.get_chunk(chunk_index)
        if row is None:
            return False
        return row.stages_state == StageState.DONE

    def _is_user_deleted(self, chunk_index: int) -> bool:
        """True if the ledger marks this chunk USER_DELETED (U8 range delete).

        A user-deleted chunk's on-disk artifacts are gone; re-running its stages
        would re-transcribe/re-export from unlinked media (and could resurrect
        deleted content). The runner skips it entirely.
        """
        if self._ledger is None:
            return False
        row = self._ledger.get_chunk(chunk_index)
        return row is not None and row.lifecycle == Lifecycle.USER_DELETED

    # ------------------------------------------------------------------
    # The run.
    # ------------------------------------------------------------------

    def run_chunk(
        self,
        chunk_index: int,
        start_ts: float,
        end_ts: float,
    ) -> StageArtifacts | None:
        """Run transcribe → export events → manifest for one chunk.

        Returns the produced :class:`StageArtifacts`, or ``None`` if the
        chunk was already ``STAGED`` (the idempotent no-op path — no step
        is invoked, so the expensive transcription cost is not re-paid).

        On success the chunk is marked ``STAGED`` in the ledger (if a ledger
        was supplied). On any step failure the exception propagates and the
        chunk is left pre-STAGED so a re-run completes it — a
        partial/failed manifest is never recorded as a false "done".
        """
        # Ledger gate first: a fully-staged chunk short-circuits before any
        # step runs (so re-transcription never happens on a re-run).
        if self.is_staged(chunk_index):
            logger.debug(
                "Chunk %d already STAGED — skipping agnostic stages", chunk_index
            )
            return None

        # U8: a USER_DELETED chunk's artifacts are gone — never re-stage it (would
        # re-derive from unlinked media / resurrect deleted content).
        if self._is_user_deleted(chunk_index):
            logger.debug(
                "Chunk %d is USER_DELETED — skipping agnostic stages", chunk_index
            )
            return None

        # 1. Transcribe (idempotent: the step early-returns if the
        #    transcript already exists on disk).
        transcript = self._transcribe(chunk_index)

        # 2. Export events (idempotent: the step early-returns on an
        #    existing JSONL). The injected step owns the privacy posture —
        #    the runner stays destination-agnostic here.
        events = self._export_events(chunk_index, start_ts, end_ts)

        # 3. Manifest (idempotent at the artifact level via the manifest
        #    step; on failure the step is responsible for not leaving a
        #    truncated file — we do not mark STAGED).
        manifest = self._manifest(chunk_index, start_ts, end_ts)

        # All three artifacts exist → record the single STAGED transition.
        # This is the LAST write, gated on the prior three succeeding, so a
        # crash mid-stage leaves the chunk pre-STAGED (re-runnable), never a
        # false "done".
        if self._ledger is not None:
            self._ledger.mark_staged(chunk_index)

        return StageArtifacts(
            transcript=transcript, events=events, manifest=manifest
        )
