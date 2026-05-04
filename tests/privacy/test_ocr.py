"""Integration tests for OCR-based screenshot PII detection.

Uses real Apple Vision OCR and real DetectionPipeline on Pillow-rendered
test JPEGs.  Skipped on non-macOS platforms (Vision framework required).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from screencap.scrubber import _pad_bbox, ocr_mask_screenshot

pytestmark = [
    pytest.mark.privacy,
    pytest.mark.skipif(sys.platform != "darwin", reason="Vision framework requires macOS"),
]


def _render_text_jpeg(tmp_path: Path, text: str, filename: str = "test.jpg") -> Path:
    """Render *text* onto a white JPEG and return the file path."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (800, 200), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Use a large font so Vision can reliably recognize the text
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
    except OSError:
        font = ImageFont.load_default(size=36)
    draw.text((40, 60), text, fill=(0, 0, 0), font=font)
    path = tmp_path / filename
    img.save(path, "JPEG", quality=95)
    img.close()
    return path


@pytest.fixture()
def ocr():
    from screencap.privacy.ocr import VisionOcr

    return VisionOcr()


@pytest.fixture()
def pipeline():
    from screencap.privacy import create_default_pipeline

    return create_default_pipeline()


# ---- _pad_bbox ---------------------------------------------------------


class TestPadBbox:
    def test_basic_padding(self):
        assert _pad_bbox((10, 10, 20, 20), 3, 100, 100) == (7, 7, 26, 26)

    def test_clamps_to_image_bounds(self):
        assert _pad_bbox((0, 0, 10, 10), 5, 100, 100) == (0, 0, 15, 15)

    def test_clamps_bottom_right(self):
        assert _pad_bbox((90, 90, 10, 10), 5, 100, 100) == (85, 85, 15, 15)


# ---- ocr_mask_screenshot -----------------------------------------------


class TestOcrMaskScreenshot:
    def test_detects_email_pii(self, tmp_path, ocr, pipeline):
        """JPEG with an email address → non-empty MaskRegion list with EMAIL label."""
        img_path = _render_text_jpeg(tmp_path, "Contact: alice@example.com today")
        regions = ocr_mask_screenshot(img_path, pipeline, ocr)

        assert len(regions) >= 1
        labels = {r.label for r in regions}
        assert "EMAIL" in labels or "EMAIL_ADDRESS" in labels
        # All bboxes within image bounds
        for r in regions:
            assert r.x >= 0 and r.y >= 0
            assert r.x + r.width <= 800
            assert r.y + r.height <= 200

    def test_clean_text_no_regions(self, tmp_path, ocr, pipeline):
        """JPEG with non-PII text → empty MaskRegion list."""
        img_path = _render_text_jpeg(tmp_path, "Hello World 2026")
        regions = ocr_mask_screenshot(img_path, pipeline, ocr)

        assert regions == []

    def test_blank_image_no_regions(self, tmp_path, ocr, pipeline):
        """Blank (no text) JPEG → empty MaskRegion list."""
        from PIL import Image

        img_path = tmp_path / "blank.jpg"
        img = Image.new("RGB", (200, 200), (128, 128, 128))
        img.save(img_path, "JPEG")
        img.close()

        regions = ocr_mask_screenshot(img_path, pipeline, ocr)
        assert regions == []

    def test_detection_error_masks_full_block(self, tmp_path, ocr):
        """When pipeline.detect() raises, the entire text block is masked."""
        img_path = _render_text_jpeg(tmp_path, "Some text with alice@example.com")

        class FailingPipeline:
            def detect(self, text):
                raise RuntimeError("simulated detector failure")

        regions = ocr_mask_screenshot(img_path, FailingPipeline(), ocr)

        # Every recognized text block should have a DETECTION_ERROR region
        assert len(regions) >= 1
        assert all(r.label == "DETECTION_ERROR" for r in regions)


# ---- build_offset_map ---------------------------------------------------


class TestBuildOffsetMap:
    def test_identity_when_equal(self):
        from screencap.privacy.ocr import build_offset_map

        result = build_offset_map("hello", "hello")
        assert result == [0, 1, 2, 3, 4, 5]

    def test_zero_width_char_stripped(self):
        from screencap.privacy.ocr import build_offset_map
        from screencap.privacy import normalize_text

        original = "he\u200bllo"  # zero-width space between e and l
        normalized = normalize_text(original)
        assert len(normalized) == 5  # "hello"
        assert len(original) == 6

        offset_map = build_offset_map(original, normalized)

        # 'h' at norm[0] → orig[0], 'e' at norm[1] → orig[1],
        # 'l' at norm[2] → orig[3] (skipping ZWS at orig[2]),
        # 'l' at norm[3] → orig[4], 'o' at norm[4] → orig[5]
        assert offset_map[0] == 0  # h
        assert offset_map[1] == 1  # e
        assert offset_map[2] == 3  # l (after ZWS)
        assert offset_map[3] == 4  # l
        assert offset_map[4] == 5  # o
        assert offset_map[5] == 6  # end sentinel


# ---- _vision_bbox_to_pixels ROI remapping --------------------------------


class TestVisionBboxToPixels:
    """Pure-math tests for ROI coordinate remapping (no Vision framework)."""

    @staticmethod
    def _mock_bbox(x, y, w, h):
        origin = type("O", (), {"x": x, "y": y})()
        size = type("S", (), {"width": w, "height": h})()
        return type("B", (), {"origin": origin, "size": size})()

    def test_no_roi_identity(self):
        from screencap.privacy.ocr import _vision_bbox_to_pixels

        bbox = self._mock_bbox(0.25, 0.75, 0.5, 0.125)
        x, y, w, h = _vision_bbox_to_pixels(bbox, 2000, 1000)
        assert x == 500   # 0.25 * 2000
        assert w == 1000  # 0.5 * 2000
        assert h == 125   # 0.125 * 1000
        # y flipped: (1.0 - 0.75 - 0.125) * 1000 = 0.125 * 1000 = 125
        assert y == 125

    def test_roi_remaps_to_full_image(self):
        from screencap.privacy.ocr import _vision_bbox_to_pixels

        # ROI covers the right half of the image
        roi = (0.5, 0.0, 0.5, 1.0)
        # Text block at (0.25, 0.75) within the ROI
        bbox = self._mock_bbox(0.25, 0.75, 0.5, 0.125)

        x, y, w, h = _vision_bbox_to_pixels(bbox, 2000, 1000, roi)
        # norm_x = 0.5 + 0.25*0.5 = 0.625 → pixel 1250
        assert x == 1250
        # norm_w = 0.5 * 0.5 = 0.25 → pixel 500
        assert w == 500
        # norm_y = 0.0 + 0.75*1.0 = 0.75, norm_h = 0.125*1.0 = 0.125
        # y = (1.0 - 0.75 - 0.125) * 1000 = 125
        assert y == 125

    def test_roi_none_same_as_no_roi(self):
        from screencap.privacy.ocr import _vision_bbox_to_pixels

        bbox = self._mock_bbox(0.25, 0.5, 0.4, 0.1)
        result_none = _vision_bbox_to_pixels(bbox, 1920, 1080, None)
        result_no_arg = _vision_bbox_to_pixels(bbox, 1920, 1080)
        assert result_none == result_no_arg
