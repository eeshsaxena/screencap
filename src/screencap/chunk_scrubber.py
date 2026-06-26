"""Per-chunk scrubbing seam (SCR-35).

``ChunkScrubber`` owns "scrub this chunk's outputs": the per-chunk
:class:`~screencap.scrubber.Scrubber` construction, the ``run_chunk`` call,
the audit-entry logging, and the "is scrubbing on?" predicate (the old
``_scrub_enabled and _pipeline is not None`` call-site guard). ``ChunkProcessor``
asks the seam (``is_enabled`` / ``scrub``) instead of inspecting scrub
internals.

In U2 the masking config (pipeline / anonymizer / evaluator / classifier /
pixel_ratio) is passed in by ``ChunkProcessor``, which still builds it. U3
moves that construction into this class behind a factory and replaces the
init-failure → upload-disable side effect with an explicit result.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from screencap.scrubber import ScrubResult

logger = logging.getLogger(__name__)

__all__ = ["ChunkScrubber"]


class ChunkScrubber:
    """Scrub a single chunk's outputs, or report that scrubbing is off."""

    def __init__(
        self,
        capture_dir: Path | str,
        *,
        enabled: bool,
        pipeline,
        anonymizer,
        evaluator,
        classifier,
        pixel_ratio: float,
    ) -> None:
        self._capture_dir = Path(capture_dir)
        self._enabled = enabled
        self._pipeline = pipeline
        self._anonymizer = anonymizer
        self._evaluator = evaluator
        self._classifier = classifier
        self._pixel_ratio = pixel_ratio

    @property
    def is_enabled(self) -> bool:
        """True when scrubbing should run for this recording.

        Encapsulates the old ``_scrub_enabled and _pipeline is not None``
        call-site guard: a recording that opted in but whose pipeline failed
        to construct is not enabled.
        """
        return self._enabled and self._pipeline is not None

    def scrub(
        self,
        idx: int,
        start_ts: float,
        end_ts: float,
        transcript_path: Path | None,
    ) -> "ScrubResult | None":
        """Scrub one chunk's text surfaces + mask its screenshots.

        Returns the ``ScrubResult`` when scrubbing ran, or ``None`` when
        scrubbing is off (no ``Scrubber`` is constructed). The caller gates
        the SCR-118 content-index pass on a non-None result — never on the
        attempt — so a disabled recording indexes nothing.

        Delegates the load-bearing step order to ``Scrubber.run_chunk()``;
        this seam only owns lifecycle concerns (which chunks to scrub, when,
        with what masking config).
        """
        if not self.is_enabled:
            return None

        from screencap.scrubber import Scrubber

        scrubber = Scrubber(
            self._capture_dir,
            pipeline=self._pipeline,
            anonymizer=self._anonymizer,
            evaluator=self._evaluator,
            classifier=self._classifier,
            pixel_ratio=self._pixel_ratio,
        )
        scrub_result = scrubber.run_chunk(
            idx=idx,
            start_ts=start_ts,
            end_ts=end_ts,
            transcript_path=transcript_path,
        )

        if scrub_result.audit_entries:
            logger.info(
                f"Chunk {idx}: scrubbed with "
                f"{len(scrub_result.audit_entries)} audit entries"
            )
        return scrub_result
