"""Apple Vision OCR for screenshot PII detection.

Provides a protocol-based interface for OCR with an Apple Vision concrete
implementation.  All Vision/PyObjC imports are deferred inside method bodies
to keep ``screencap --help`` fast.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class OcrTextBlock:
    """A recognized text block with pixel-coordinate bounding boxes."""

    text: str  # recognized text
    bbox: tuple[int, int, int, int]  # (x, y, w, h) pixels, top-left origin
    char_bboxes: Callable[[int, int], tuple[int, int, int, int] | None]
    # callable(start_idx, length) -> (x, y, w, h) pixel bbox, or None


@dataclass(frozen=True)
class OcrResult:
    """Result of running OCR on a screenshot."""

    text_blocks: list[OcrTextBlock]
    image_width: int
    image_height: int


class ScreenshotOcr(Protocol):
    """Protocol for screenshot OCR engines."""

    def recognize(
        self,
        image_path: Path,
        *,
        roi: tuple[float, float, float, float] | None = None,
    ) -> OcrResult: ...

    def recognize_bytes(
        self,
        data: bytes,
        *,
        roi: tuple[float, float, float, float] | None = None,
    ) -> OcrResult: ...


class VisionOcr:
    """Apple Vision VNRecognizeTextRequest wrapper.

    All Vision/PyObjC imports are deferred inside method bodies
    to keep ``screencap --help`` fast.
    """

    def __init__(self) -> None:
        # Fail fast if Vision framework unavailable.
        import Vision as _Vision  # noqa: N811

        self._Vision = _Vision

    def recognize(
        self,
        image_path: Path,
        *,
        roi: tuple[float, float, float, float] | None = None,
    ) -> OcrResult:
        from Foundation import NSData

        img_data = NSData.dataWithContentsOfFile_(str(image_path))
        if img_data is None:
            raise OSError(f"Failed to load image data: {image_path}")
        im_w, im_h = _image_dimensions(image_path)
        return self._recognize_nsdata(img_data, im_w, im_h, roi)

    def recognize_bytes(
        self,
        data: bytes,
        *,
        roi: tuple[float, float, float, float] | None = None,
    ) -> OcrResult:
        """OCR an in-memory image (search U3): the seam that lets the encrypted
        index pass decrypt a still to RAM and OCR it without ever writing the
        decrypted plaintext to disk (KTD2 — no decrypt-to-temp)."""
        import io

        from Foundation import NSData
        from PIL import Image

        img_data = NSData.dataWithBytes_length_(data, len(data))
        if img_data is None:
            raise OSError("Failed to build NSData from in-memory image bytes")
        with Image.open(io.BytesIO(data)) as im:
            im_w, im_h = im.size
        return self._recognize_nsdata(img_data, im_w, im_h, roi)

    def _recognize_nsdata(
        self,
        img_data: object,
        im_w: int,
        im_h: int,
        roi: tuple[float, float, float, float] | None,
    ) -> OcrResult:
        import objc

        with objc.autorelease_pool():
            handler = (
                self._Vision.VNImageRequestHandler.alloc().initWithData_options_(
                    img_data, None
                )
            )
            req = self._Vision.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(0)  # accurate
            if roi is not None:
                # PyObjC expects CGRect as ((x, y), (width, height)), not
                # a flat (x, y, w, h) tuple.  Vision uses normalized coords
                # with bottom-left origin.
                x, y, w, h = roi
                req.setRegionOfInterest_(((x, y), (w, h)))

            success, error = handler.performRequests_error_([req], None)
            if not success:
                return OcrResult([], im_w, im_h)

            blocks: list[OcrTextBlock] = []
            for obs in req.results() or []:
                candidate = obs.topCandidates_(1)
                if not candidate:
                    continue
                top = candidate[0]
                text = str(top.string())
                bbox = _vision_bbox_to_pixels(obs.boundingBox(), im_w, im_h, roi)

                blocks.append(
                    OcrTextBlock(
                        text=text,
                        bbox=bbox,
                        char_bboxes=_make_char_bboxes(top, im_w, im_h, roi),
                    )
                )

            return OcrResult(
                text_blocks=blocks, image_width=im_w, image_height=im_h
            )


def _make_char_bboxes(
    cand: object,
    w: int,
    h: int,
    roi: tuple[float, float, float, float] | None = None,
) -> Callable[[int, int], tuple[int, int, int, int] | None]:
    """Factory to create a char_bboxes closure for a VNRecognizedText candidate."""

    def char_bboxes(start: int, length: int) -> tuple[int, int, int, int] | None:
        import Foundation

        rng = Foundation.NSRange(start, length)
        obs, err = cand.boundingBoxForRange_error_(rng, None)  # type: ignore[union-attr]
        if obs is None:
            return None
        # boundingBoxForRange returns a VNRectangleObservation;
        # extract the normalized CGRect via .boundingBox().
        return _vision_bbox_to_pixels(obs.boundingBox(), w, h, roi)

    return char_bboxes


def _vision_bbox_to_pixels(
    bbox: object,
    im_w: int,
    im_h: int,
    roi: tuple[float, float, float, float] | None = None,
) -> tuple[int, int, int, int]:
    """Convert Vision normalized coords (bottom-left origin) to PIL pixels (top-left origin).

    When *roi* is provided, Vision returns coordinates relative to the ROI
    region rather than the full image.  This function remaps them back to
    full-image coordinates before converting to pixels.
    """
    norm_x = bbox.origin.x  # type: ignore[union-attr]
    norm_y = bbox.origin.y  # type: ignore[union-attr]
    norm_w = bbox.size.width  # type: ignore[union-attr]
    norm_h = bbox.size.height  # type: ignore[union-attr]

    if roi is not None:
        roi_x, roi_y, roi_w, roi_h = roi
        norm_x = roi_x + norm_x * roi_w
        norm_y = roi_y + norm_y * roi_h
        norm_w = norm_w * roi_w
        norm_h = norm_h * roi_h

    x = int(norm_x * im_w)
    y = int((1.0 - norm_y - norm_h) * im_h)
    w = int(norm_w * im_w)
    h = int(norm_h * im_h)
    return (x, y, w, h)


def _image_dimensions(image_path: Path) -> tuple[int, int]:
    """Get image dimensions without loading full image into memory."""
    from PIL import Image

    with Image.open(image_path) as img:
        return img.size


def build_offset_map(original: str, normalized: str) -> list[int]:
    """Build a mapping from normalized-text offsets to original-text offsets.

    Returns a list where ``map[i]`` is the index in *original* that
    corresponds to index ``i`` in *normalized*.  The list has length
    ``len(normalized) + 1`` so that slice endpoints also map correctly.

    The mapping accounts for two transformations applied by
    ``normalize_text()``:

    1. NFKC normalization — may change character count (e.g. ligatures expand).
    2. Zero-width character stripping — removes characters entirely.

    When the texts are identical (the common case for Vision output) this
    function short-circuits and returns a trivial identity map.
    """
    if original == normalized:
        return list(range(len(normalized) + 1))

    # Step 1: NFKC normalize (may change length), track char-level mapping
    nfkc = unicodedata.normalize("NFKC", original)
    # Build original→nfkc offset map by expanding each original char into its
    # NFKC decomposition
    orig_to_nfkc: list[int] = []  # nfkc_idx for each original char
    nfkc_idx = 0
    for orig_idx, ch in enumerate(original):
        nfkc_ch = unicodedata.normalize("NFKC", ch)
        for _ in nfkc_ch:
            orig_to_nfkc.append(orig_idx)
            nfkc_idx += 1
    orig_to_nfkc.append(len(original))  # sentinel for end position

    # Step 2: zero-width stripping — walk nfkc chars, skip stripped ones
    # Import the same regex used by normalize_text()
    from screencap.redaction.engine import _ZERO_WIDTH

    norm_to_orig: list[int] = []
    nfkc_pos = 0
    for ch in nfkc:
        if _ZERO_WIDTH.match(ch):
            nfkc_pos += 1
            continue
        # This char survived stripping → it's in normalized
        if nfkc_pos < len(orig_to_nfkc):
            norm_to_orig.append(orig_to_nfkc[nfkc_pos])
        else:
            norm_to_orig.append(len(original))
        nfkc_pos += 1

    # Sentinel for end position
    norm_to_orig.append(len(original))

    return norm_to_orig
