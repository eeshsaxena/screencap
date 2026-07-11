"""Secrets-only local scrub for indexed stills (search U3 / R1 / KTD1).

The index-time redaction profile: reuse the deterministic ``RegexDetector`` + the
``DetectSecretsDetector`` (plus a small NER-free EMAIL regex) over each frame's OCR
text and paint ONLY the detected secret/PII regions, so the persisted still and its
indexed text carry no passwords / API keys / cards / SSNs / emails.

Deliberately **lighter than the upload masker** (``create_default_pipeline``):

- **No PERSON-name NER.** Names are legitimate search keys and the NER has a high
  false-positive cost to recall (KTD1); excluding it also keeps this pipeline
  model-free (regex + entropy only), cheap enough for the post-capture index pass.
- **No FULL_WINDOW / PANE structural masks.** Those are upload-calibrated and would
  black out whole panes, destroying search recall on the LOCAL copy.

``LocalScrubber.scrub`` maps each detection's char-span to its OCR bounding box and
paints a tight solid box over just that span (via the shared
``privacy/mask_primitives`` primitive), returning the painted image bytes plus the
anonymized text to index. When a frame has no detections it returns
``painted_bytes=None`` so the caller leaves the still untouched (idempotent, no
needless re-encrypt).
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from screencap.redaction.engine import DetectionPipeline
    from screencap.redaction.ocr import OcrResult

logger = logging.getLogger(__name__)

# JPEG quality for a re-encoded (painted) still — matches the capture default so a
# redacted frame is visually consistent with the rest of the recording.
_REENCODE_JPEG_QUALITY = 85

# Conservative EMAIL pattern. KTD1 includes EMAIL/SSN/CREDIT_CARD; SSN + CREDIT_CARD
# come from ``RegexDetector``, and EMAIL is added here as its own regex so the local
# profile stays NER-free (no PERSON model load).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class _EmailDetector:
    """NER-free EMAIL detector shaped like the engine's ``TextDetector``."""

    def detect(self, text: str) -> list:
        from screencap.redaction.engine import Detection, EntityType

        return [
            Detection(
                entity_type=EntityType.EMAIL,
                start=m.start(),
                end=m.end(),
                score=0.9,
                source="regex-email",
            )
            for m in _EMAIL_RE.finditer(text)
        ]


def create_local_scrub_pipeline() -> "DetectionPipeline":
    """Build the secrets-only detection pipeline (regex + detect-secrets + email).

    Raises ``ImportError`` when even the minimum detectors are unavailable, so the
    caller can fail closed (index nothing) rather than index unredacted text (R8)."""
    from screencap.redaction.engine import DetectionPipeline
    from screencap.redaction.filters import HeuristicFilter
    from screencap.redaction.regex import RegexDetector
    from screencap.redaction.resolver import DetectionResolver

    detectors: list = [RegexDetector(), _EmailDetector()]
    try:
        from screencap.redaction.secrets import DetectSecretsDetector

        detectors.append(DetectSecretsDetector())
    except ImportError:
        logger.warning(
            "detect-secrets unavailable — local scrub falls back to regex + email only"
        )
    return DetectionPipeline(detectors, filters=[HeuristicFilter()], resolver=DetectionResolver())


@dataclass
class LocalScrubResult:
    """One frame's scrub outcome.

    ``redacted_text`` is the text to index (secret spans replaced with ``<TYPE>``
    tags). ``painted_bytes`` is the re-encoded still with the detected regions
    painted out, or ``None`` when nothing was detected (leave the still as-is).
    """

    redacted_text: str
    painted_bytes: bytes | None
    detections: int


class LocalScrubber:
    """Runs the secrets pipeline over a frame's OCR blocks + paints detected spans."""

    def __init__(self, pipeline: "DetectionPipeline | None" = None) -> None:
        self._pipeline = pipeline if pipeline is not None else create_local_scrub_pipeline()

    def scrub(self, image_bytes: bytes, ocr_result: "OcrResult") -> LocalScrubResult:
        """Detect secrets in each OCR block, paint their pixel regions, and return
        the redacted text + painted still bytes (``painted_bytes=None`` if clean)."""
        from screencap.privacy.mask_primitives import MaskRegion
        from screencap.redaction.engine import Anonymizer
        from screencap.redaction.ocr import build_offset_map

        anonymizer = Anonymizer()
        regions: list[MaskRegion] = []
        redacted_parts: list[str] = []
        total = 0

        for block in ocr_result.text_blocks:
            if not block.text:
                continue
            det = self._pipeline.detect(block.text)
            normalized, detections = det.normalized_text, det.detections
            if not detections:
                redacted_parts.append(block.text)
                continue
            total += len(detections)
            offset_map = build_offset_map(block.text, normalized)
            for d in detections:
                region = self._region_for(block, offset_map, d.start, d.end)
                if region is not None:
                    regions.append(region)
            redacted_parts.append(anonymizer.anonymize(normalized, detections))

        redacted_text = " ".join(p for p in redacted_parts if p).strip()
        painted_bytes = self._paint(image_bytes, regions) if regions else None
        return LocalScrubResult(
            redacted_text=redacted_text, painted_bytes=painted_bytes, detections=total
        )

    @staticmethod
    def _region_for(block, offset_map, norm_start: int, norm_end: int):
        """Map a normalized-text char span to a pixel ``MaskRegion`` via the block's
        char_bboxes. Falls back to the whole-block bbox (fail-closed: over-cover) if
        the per-span box is unavailable."""
        from screencap.privacy.mask_primitives import MaskRegion

        try:
            o_start = offset_map[norm_start]
            o_end = offset_map[norm_end]
        except IndexError:
            o_start, o_end = None, None
        bbox = None
        if o_start is not None and o_end is not None and o_end > o_start:
            bbox = block.char_bboxes(o_start, o_end - o_start)
        if bbox is None:
            bbox = block.bbox
        if bbox is None:
            return None
        x, y, w, h = bbox
        if w <= 0 or h <= 0:
            return None
        return MaskRegion(x=x, y=y, width=w, height=h, label="")

    @staticmethod
    def _paint(image_bytes: bytes, regions: list) -> bytes:
        from PIL import Image

        from screencap.privacy.mask_primitives import _apply_mask_to_image

        with Image.open(io.BytesIO(image_bytes)) as im:
            rgb = im.convert("RGB")
        _apply_mask_to_image(rgb, regions)
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=_REENCODE_JPEG_QUALITY)
        rgb.close()
        return out.getvalue()


__all__ = [
    "create_local_scrub_pipeline",
    "LocalScrubResult",
    "LocalScrubber",
]
