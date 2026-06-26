"""Per-chunk manifest production seam (SCR-35).

``ChunkManifest`` owns "produce a manifest for this chunk": which
segmentation mode (v1 ``idle`` / v2 ``llm``) this recording uses, gathering
the privacy ``blocked_intervals`` for the chunk window, and removing a
partially-written manifest if generation fails (so a retry / upload never
ships a truncated file). It is the single chunk-pipeline owner of the
manifest-version decision — ``ChunkProcessor`` no longer reads
``_segmentation_mode`` itself; it injects ``produce`` as the ``manifest=``
step of the :class:`~screencap.pipeline_stages.PipelineStageRunner`.

The class is constructed cheaply (a ``capture_dir`` + mode + threshold + an
optional screen filter — no recorder, no multiprocessing queues), so it is a
direct unit-test target rather than something only exercised through a full
``ChunkProcessor`` integration run.

The v1/v2 *render* still lives in ``task_manifest.generate_manifest`` (the
format renderer); ``ChunkManifest`` owns the *decision* of which mode the
live chunk pipeline uses.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ChunkManifest"]


class ChunkManifest:
    """Produce the task manifest for one chunk (the injected manifest step)."""

    def __init__(
        self,
        capture_dir: Path | str,
        *,
        segmentation_mode: str = "llm",
        rest_threshold: float = 120.0,
        screen_filter=None,
    ) -> None:
        self._capture_dir = Path(capture_dir)
        self._segmentation_mode = segmentation_mode
        self._rest_threshold = rest_threshold
        self._screen_filter = screen_filter

    def produce(self, idx: int, start_ts: float, end_ts: float) -> Path:
        """Produce the manifest for chunk ``idx`` over ``[start_ts, end_ts)``.

        Gathers ``blocked_intervals`` from the screen filter (best-effort; a
        filter error is logged and treated as "no intervals"), then delegates
        the render to ``task_manifest.generate_manifest`` with this
        recording's segmentation mode. On failure, the partial manifest file
        is unlinked before the exception re-raises, so a later retry or a
        ``screencap upload`` never ships a truncated manifest.

        Signature matches ``PipelineStageRunner``'s ``ManifestStep``:
        ``(idx, start_ts, end_ts) -> Path``.
        """
        from screencap.task_manifest import generate_manifest

        blocked_intervals = self._blocked_intervals(idx, start_ts, end_ts)
        try:
            return generate_manifest(
                self._capture_dir, idx, start_ts, end_ts,
                rest_threshold=self._rest_threshold,
                blocked_intervals=blocked_intervals,
                segmentation_mode=self._segmentation_mode,
            )
        except Exception:
            logger.exception(f"Chunk {idx}: manifest generation failed")
            # Remove any partially-written manifest so a later retry (or
            # screencap upload) doesn't ship a truncated file.
            (self._capture_dir / f"chunk_{idx:04d}_manifest.json").unlink(
                missing_ok=True
            )
            raise

    def _blocked_intervals(
        self, idx: int, start_ts: float, end_ts: float
    ) -> list[dict] | None:
        """Best-effort privacy ``blocked_intervals`` for this chunk window.

        Returns ``None`` when there is no screen filter, the filter does not
        expose ``get_blocked_intervals``, the filter raises, or the filter
        returns an empty list (empty coalesces to ``None``).
        """
        sf = self._screen_filter
        if sf is None or not hasattr(sf, "get_blocked_intervals"):
            return None
        try:
            return sf.get_blocked_intervals(start_ts, end_ts) or None
        except Exception:
            logger.warning(
                f"Failed to get blocked_intervals for chunk {idx}",
                exc_info=True,
            )
            return None
