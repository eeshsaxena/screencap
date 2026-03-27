#!/usr/bin/env python3
"""Manual test: run OCR-based PII masking on a screenshot.

Usage:
    # On a single screenshot:
    python scripts/test_ocr_masking.py path/to/screenshot.jpg

    # On all screenshots in a recording:
    python scripts/test_ocr_masking.py ~/.screencap/recordings/<name>/screenshots/

Produces a side-by-side original + masked copy in /tmp/ocr_test_output/
so you can visually verify masking accuracy.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    target = Path(sys.argv[1])
    if target.is_dir():
        images = sorted(target.glob("*.jpg"))[:10]  # cap at 10 for speed
    elif target.is_file():
        images = [target]
    else:
        print(f"Not found: {target}")
        sys.exit(1)

    if not images:
        print(f"No .jpg files found in {target}")
        sys.exit(1)

    # Deferred imports (same pattern as production code)
    from screencap.privacy.ocr import VisionOcr
    from screencap.privacy import create_default_pipeline
    from screencap.scrub_pipeline import ocr_mask_screenshot

    print("Initializing OCR + detection pipeline...")
    ocr = VisionOcr()
    pipeline = create_default_pipeline()

    out_dir = Path("/tmp/ocr_test_output")
    out_dir.mkdir(exist_ok=True)

    for img_path in images:
        print(f"\n{'='*60}")
        print(f"Processing: {img_path.name}")

        # Run OCR
        result = ocr.recognize(img_path)
        print(f"  OCR blocks: {len(result.text_blocks)}")
        for i, block in enumerate(result.text_blocks):
            preview = block.text[:80].replace("\n", "\\n")
            print(f"    [{i}] bbox={block.bbox} text={preview!r}")

        # Run detection
        regions = ocr_mask_screenshot(img_path, pipeline, ocr)
        print(f"  PII regions: {len(regions)}")
        for r in regions:
            print(f"    {r.label}: x={r.x} y={r.y} w={r.width} h={r.height}")

        # Create masked copy
        if regions:
            masked_path = out_dir / f"masked_{img_path.name}"
            shutil.copy2(img_path, masked_path)
            from screencap.privacy.masking import mask_screenshot
            from screencap.privacy.policy import ContextClass
            mask_screenshot(masked_path, ContextClass.UNKNOWN, regions=regions)
            print(f"  Masked copy: {masked_path}")
        else:
            print("  No PII detected — no masking needed")

        # Also save original for comparison
        orig_path = out_dir / f"original_{img_path.name}"
        shutil.copy2(img_path, orig_path)

    print(f"\n{'='*60}")
    print(f"Output: {out_dir}/")
    print("Compare original_*.jpg vs masked_*.jpg to verify masking accuracy.")


if __name__ == "__main__":
    main()
