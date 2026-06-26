"""Per-chunk scrubbing seam (SCR-35).

``ChunkScrubber`` owns "scrub this chunk's outputs": the per-chunk
:class:`~screencap.scrubber.Scrubber` construction, the ``run_chunk`` call,
the audit-entry logging, and the "is scrubbing on?" predicate (the old
``_scrub_enabled and _pipeline is not None`` call-site guard). ``ChunkProcessor``
asks the seam (``is_enabled`` / ``scrub``) instead of inspecting scrub
internals.

The scrub/masking config (pipeline / anonymizer / evaluator / classifier /
pixel_ratio) is built here behind the :meth:`create` factory; ``ChunkProcessor``
no longer constructs or owns these objects. A scrub/masking-init failure is
reported back as an explicit :class:`ScrubInit` upload-policy fact the
sequencer consumes, rather than reaching back and mutating ``ChunkProcessor``
state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from screencap.privacy.classify import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator
    from screencap.redaction.engine import Anonymizer, DetectionPipeline
    from screencap.scrubber import ScrubResult

logger = logging.getLogger(__name__)

__all__ = ["ChunkScrubber", "ScrubInit"]


@dataclass(frozen=True)
class ScrubInit:
    """The upload-policy fact scrub/masking init reports back to the sequencer.

    ``disable_uploads_reason`` is non-None only when scrub/masking init failed
    for a *cloud-intent* recording — the sequencer must then disable uploads
    (and record the reason) so the failure is fail-closed: a cloud recording
    never ships unscrubbed. ``None`` means no upload-policy change is required
    (init succeeded, or this is a local recording whose best-effort scrubbing
    silently downgraded). This replaces the old side effect where scrub init
    reached up and mutated ``ChunkProcessor._upload_enabled`` directly.
    """

    disable_uploads_reason: str | None = None


class ChunkScrubber:
    """Scrub a single chunk's outputs, or report that scrubbing is off."""

    def __init__(
        self,
        capture_dir: Path | str,
        *,
        enabled: bool,
        pipeline: "DetectionPipeline | None",
        anonymizer: "Anonymizer | None",
        evaluator: "DefaultPolicyEvaluator | None",
        classifier: "DefaultContextClassifier | None",
        pixel_ratio: float,
    ) -> None:
        self._capture_dir = Path(capture_dir)
        self._enabled = enabled
        self._pipeline = pipeline
        self._anonymizer = anonymizer
        self._evaluator = evaluator
        self._classifier = classifier
        self._pixel_ratio = pixel_ratio

    @classmethod
    def create(
        cls,
        capture_dir: Path | str,
        *,
        cloud_intent: bool,
        upload_enabled: bool,
        scrub_enabled: bool,
    ) -> tuple["ChunkScrubber", ScrubInit]:
        """Build the scrub seam and report the upload-policy fact (SCR-35, U3).

        Owns the "is scrubbing on?" decision and all scrub/masking-config
        construction that used to live inline in ``ChunkProcessor.__init__``:
        the redaction pipeline + anonymizer, and the screenshot-masking
        classifier/evaluator (with the cloud-intent ``PrivacyMode.PUBLIC``
        override). The decision matrix:

          * cloud-intent + uploads on  → init; a deps/masking failure returns a
            non-None ``disable_uploads_reason`` (fail-closed).
          * local opt-in               → init; a deps failure silently disables
            scrubbing (``enabled=False``), uploads unaffected.
          * neither                    → inert seam, no heavy imports.

        The init-failure → upload-disable coupling is an explicit ``ScrubInit``
        the sequencer consumes, not a mutation reaching back into
        ``ChunkProcessor`` state. This wrapper is **total**: any unexpected
        construction failure falls back to an inert seam, fail-closed (uploads
        disabled for a cloud-intent recording) so a recording never ships
        unscrubbed because of a scrub-init crash.
        """
        try:
            return cls._build(
                capture_dir,
                cloud_intent=cloud_intent,
                upload_enabled=upload_enabled,
                scrub_enabled=scrub_enabled,
            )
        except Exception as e:
            logger.error(f"Scrub seam construction failed: {e}", exc_info=True)
            inert = cls(
                capture_dir, enabled=False, pipeline=None, anonymizer=None,
                evaluator=None, classifier=None, pixel_ratio=2.0,
            )
            reason = (
                f"Scrub init failed: {e}"
                if (cloud_intent and upload_enabled) else None
            )
            return inert, ScrubInit(disable_uploads_reason=reason)

    @classmethod
    def _build(
        cls,
        capture_dir: Path | str,
        *,
        cloud_intent: bool,
        upload_enabled: bool,
        scrub_enabled: bool,
    ) -> tuple["ChunkScrubber", ScrubInit]:
        """The scrub/masking-init decision matrix (wrapped by :meth:`create`)."""
        pipeline = None
        anonymizer = None
        evaluator = None
        classifier = None
        pixel_ratio = 2.0  # safe Retina default
        enabled = scrub_enabled
        disable_uploads_reason: str | None = None

        should_init = (cloud_intent and upload_enabled) or scrub_enabled
        if should_init:
            try:
                from screencap.redaction import Anonymizer, create_default_pipeline

                pipeline = create_default_pipeline(require_pii=True)
                anonymizer = Anonymizer()
                logger.info("Scrubbing pipeline initialized")
            except Exception as e:
                if cloud_intent and upload_enabled:
                    logger.error(
                        f"Privacy deps not available — disabling uploads for safety: {e}"
                    )
                    disable_uploads_reason = f"Privacy deps not available: {e}"
                    logger.warning(
                        "Privacy dependencies are missing. "
                        "Reinstall or update screencap."
                    )
                else:
                    # Local recording: scrubbing is best-effort, don't block recording
                    logger.warning(f"Scrubbing pipeline unavailable (non-fatal): {e}")
                    enabled = False

            # Initialize classifier/evaluator for screenshot masking
            try:
                from screencap.config import get_privacy_config
                from screencap.privacy.classify import DefaultContextClassifier
                from screencap.privacy.policy import DefaultPolicyEvaluator, PrivacyMode

                _pc = get_privacy_config()
                # Cloud uploads must use public mode so that CHAT/EMAIL/etc.
                # apps get MASK_WINDOW (blurred in screenshots) instead of
                # TEXT_REDACT (which only scrubs text, not visuals).
                if cloud_intent:
                    from dataclasses import replace as _dc_replace

                    _pc = _dc_replace(_pc, mode=PrivacyMode.PUBLIC)
                evaluator = DefaultPolicyEvaluator(_pc)
                classifier = DefaultContextClassifier(app_classes=_pc.app_classes)
            except Exception as e:
                logger.warning(f"Could not init masking classifier: {e}")
                if cloud_intent and upload_enabled and disable_uploads_reason is None:
                    disable_uploads_reason = f"Masking classifier init failed: {e}"

        scrubber = cls(
            capture_dir,
            enabled=enabled,
            pipeline=pipeline,
            anonymizer=anonymizer,
            evaluator=evaluator,
            classifier=classifier,
            pixel_ratio=pixel_ratio,
        )
        return scrubber, ScrubInit(disable_uploads_reason=disable_uploads_reason)

    @property
    def is_enabled(self) -> bool:
        """True when scrubbing should run for this recording.

        Encapsulates the old ``_scrub_enabled and _pipeline is not None``
        call-site guard: a recording that opted in but whose pipeline failed
        to construct is not enabled.
        """
        return self._enabled and self._pipeline is not None

    @property
    def has_masking_context(self) -> bool:
        """True iff both the masking classifier and evaluator were built.

        The SCR-118 content-index fail-closed guard reads this (R7): without
        masking context, ``blocked_intervals`` cannot represent masked-app
        skips, so the index pass must refuse to run (otherwise masked-app
        on-screen text — banking / email / chat — could enter the local
        index). Exposing the predicate here keeps that guard sourced from a
        live seam after the masking fields leave ``ChunkProcessor``.

        NOTE: True here does NOT imply scrubbing can run. On a cloud-intent
        seam where pipeline init failed but masking init succeeded, this is
        True while :attr:`is_enabled` is False (an inert seam). That latent
        trap is harmless because the index guard also gates on
        ``scrub_result is not None`` (in ``ChunkProcessor._process_chunk``),
        which requires :attr:`is_enabled` — so the index pass is never reached
        on an inert seam regardless of this predicate.
        """
        return self._classifier is not None and self._evaluator is not None

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
