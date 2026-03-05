"""Post-processing: generate click-target heatmaps from recordings.

Combines OmniParser API segmentation with the recorded UI accessibility tree
to produce ground-truth heatmaps for VLM training. Each heatmap is the
intersection of the clicked UI element's bounding box with a precomputed
circle mask around the click coordinate.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import requests
from PIL import Image

OMNIPARSER_ENDPOINT = "http://34.56.33.4:8000/parse"
BOX_THRESHOLD = 0.05
IOU_THRESHOLD = 0.1
RETRIES = 3
DEDUP_IOU = 0.7
NEAREST_FALLBACK_PX = 50.0

# Action types that receive heatmaps
HEATMAP_ACTION_TYPES = {"mouse.singleclick", "mouse.doubleclick", "mouse.drag"}


# ---------------------------------------------------------------------------
# Circle mask
# ---------------------------------------------------------------------------

def make_circle_mask(radius: int = 20) -> np.ndarray:
    """Precompute a (2*radius+1, 2*radius+1) boolean circle mask."""
    d = 2 * radius + 1
    y, x = np.ogrid[:d, :d]
    return ((x - radius) ** 2 + (y - radius) ** 2) <= radius ** 2


# ---------------------------------------------------------------------------
# AX tree extraction
# ---------------------------------------------------------------------------

def extract_ax_boxes(window_data: dict | None) -> list[dict]:
    """Walk the AX tree JSON and extract bounding boxes in pixel coords.

    Returns list of {"box": [y0, x0, y1, x1], "role": str, "label": str}.
    """
    if not window_data or not isinstance(window_data, dict):
        return []

    results: list[dict] = []

    def _walk(node: dict) -> None:
        pos = node.get("AXPosition")
        size = node.get("AXSize")
        if isinstance(pos, dict) and isinstance(size, dict):
            x = pos.get("x", 0)
            y = pos.get("y", 0)
            w = size.get("width", 0)
            h = size.get("height", 0)
            if w > 0 and h > 0:
                results.append({
                    "box": [int(y), int(x), int(y + h), int(x + w)],
                    "role": node.get("AXRole", ""),
                    "label": node.get("AXDescription") or node.get("AXTitle") or "",
                })
        for child in node.get("AXChildren", []):
            if isinstance(child, dict):
                _walk(child)

    _walk(window_data)
    return results


# ---------------------------------------------------------------------------
# OmniParser API segmentation
# ---------------------------------------------------------------------------

def segment_screenshot(
    image: Image.Image,
    screen_w: int,
    screen_h: int,
    endpoint: str = OMNIPARSER_ENDPOINT,
) -> list[dict]:
    """Send a screenshot to OmniParser API and return element boxes in pixel coords.

    Returns list of {"box": [y0, x0, y1, x1], "label": str}.
    """
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)

    for attempt in range(RETRIES):
        try:
            resp = requests.post(
                endpoint,
                files={"image": ("screenshot.png", buf, "image/png")},
                data={
                    "box_threshold": str(BOX_THRESHOLD),
                    "iou_threshold": str(IOU_THRESHOLD),
                    "include_annotated_image": "false",
                },
                timeout=60,
            )
            if resp.status_code in (429, 500, 503):
                import time
                time.sleep(2 ** attempt)
                buf.seek(0)
                continue
            resp.raise_for_status()
            raw_elements = resp.json().get("elements", [])
            break
        except (requests.RequestException, ValueError):
            import time
            time.sleep(2 ** attempt)
            buf.seek(0)
            if attempt == RETRIES - 1:
                return []

    elements = []
    for el in raw_elements:
        bbox = el.get("bbox_0_1")
        if not bbox or len(bbox) < 4:
            continue
        content = (el.get("content") or "").strip()
        x1, y1, x2, y2 = bbox
        elements.append({
            "box": [
                int(y1 * screen_h),
                int(x1 * screen_w),
                int(y2 * screen_h),
                int(x2 * screen_w),
            ],
            "label": el.get("element_type", "unknown"),
            "text": content,
        })
    return elements


# ---------------------------------------------------------------------------
# Union + dedup
# ---------------------------------------------------------------------------

def _box_iou(a: list[int], b: list[int]) -> float:
    iy0, ix0 = max(a[0], b[0]), max(a[1], b[1])
    iy1, ix1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, iy1 - iy0) * max(0, ix1 - ix0)
    aa = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    ab = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = aa + ab - inter
    return inter / union if union > 0 else 0


def union_elements(omni: list[dict], ax: list[dict]) -> list[dict]:
    """Combine OmniParser and AX tree elements, deduplicating by IoU."""
    combined = list(omni)
    for ax_el in ax:
        if not any(_box_iou(ax_el["box"], o["box"]) > DEDUP_IOU for o in combined):
            combined.append(ax_el)
    return combined


# ---------------------------------------------------------------------------
# Element lookup
# ---------------------------------------------------------------------------

def find_element_at(elements: list[dict], px: int, py: int) -> dict | None:
    """Find the smallest element whose box contains (px, py).

    Fallback: nearest element center within NEAREST_FALLBACK_PX.
    """
    best, best_area = None, float("inf")
    for el in elements:
        y0, x0, y1, x1 = el["box"]
        if x0 <= px <= x1 and y0 <= py <= y1:
            area = (x1 - x0) * (y1 - y0)
            if area < best_area:
                best, best_area = el, area
    if best is not None:
        return best

    nearest, nearest_dist = None, NEAREST_FALLBACK_PX
    for el in elements:
        y0, x0, y1, x1 = el["box"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
        if d < nearest_dist:
            nearest, nearest_dist = el, d
    return nearest


# ---------------------------------------------------------------------------
# Heatmap generation
# ---------------------------------------------------------------------------

def generate_heatmap(
    click_x: int,
    click_y: int,
    element_box: list[int] | None,
    screen_w: int,
    screen_h: int,
    circle_mask: np.ndarray,
) -> np.ndarray:
    """Generate a float32 heatmap (screen_h x screen_w).

    Stamps the precomputed circle_mask at (click_x, click_y), then ANDs with
    the element bounding box if one was found. Values are 0.0 or 1.0.
    """
    hmap = np.zeros((screen_h, screen_w), dtype=np.float32)
    radius = circle_mask.shape[0] // 2

    # Source region in the mask
    src_y0 = max(0, radius - click_y)
    src_x0 = max(0, radius - click_x)
    src_y1 = min(circle_mask.shape[0], radius + (screen_h - click_y))
    src_x1 = min(circle_mask.shape[1], radius + (screen_w - click_x))

    # Destination region in the heatmap
    dst_y0 = max(0, click_y - radius)
    dst_x0 = max(0, click_x - radius)
    dst_y1 = dst_y0 + (src_y1 - src_y0)
    dst_x1 = dst_x0 + (src_x1 - src_x0)

    hmap[dst_y0:dst_y1, dst_x0:dst_x1] = circle_mask[src_y0:src_y1, src_x0:src_x1]

    if element_box is not None:
        ey0, ex0, ey1, ex1 = element_box
        bbox_mask = np.zeros_like(hmap)
        ey0c = max(0, min(ey0, screen_h))
        ex0c = max(0, min(ex0, screen_w))
        ey1c = max(0, min(ey1, screen_h))
        ex1c = max(0, min(ex1, screen_w))
        bbox_mask[ey0c:ey1c, ex0c:ex1c] = 1.0
        hmap *= bbox_mask

    return hmap


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_recording(
    capture_dir: str | Path,
    endpoint: str = OMNIPARSER_ENDPOINT,
    output_dir: str | Path | None = None,
    radius: int = 20,
) -> int:
    """Generate heatmaps for all click actions in a recording.

    Returns the number of heatmaps generated.
    """
    from sc_engine import Capture
    from sc_engine.events import (
        MouseClickEvent,
        MouseDoubleClickEvent,
        MouseDragEvent,
    )

    capture_dir = Path(capture_dir)
    if output_dir is None:
        output_dir = capture_dir / "heatmaps"
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    circle_mask = make_circle_mask(radius)

    with Capture.load(str(capture_dir)) as cap:
        screen_w, screen_h = cap.screen_size
        if screen_w == 0 or screen_h == 0:
            raise ValueError(f"Invalid screen size {screen_w}x{screen_h}")

        manifest_path = output_dir / "manifest.jsonl"
        manifest = open(manifest_path, "w")
        count = 0

        for action in cap.actions(include_moves=False):
            if action.type not in HEATMAP_ACTION_TYPES:
                continue

            event = action.event
            if isinstance(event, (MouseClickEvent, MouseDoubleClickEvent)):
                click_x, click_y = int(event.x), int(event.y)
            elif isinstance(event, MouseDragEvent):
                click_x, click_y = int(event.x), int(event.y)
            else:
                continue

            # Gather elements from both sources
            ax_elements = extract_ax_boxes(action.window_data)

            omni_elements = []
            screenshot = action.screenshot
            if screenshot is not None:
                omni_elements = segment_screenshot(
                    screenshot, screen_w, screen_h, endpoint,
                )

            elements = union_elements(omni_elements, ax_elements)

            # Find which element was clicked
            hit = find_element_at(elements, click_x, click_y)
            element_box = hit["box"] if hit else None

            hmap = generate_heatmap(
                click_x, click_y, element_box,
                screen_w, screen_h, circle_mask,
            )

            fname = f"{count:04d}.npy"
            np.save(str(output_dir / fname), hmap)

            manifest.write(json.dumps({
                "index": count,
                "file": fname,
                "action_type": action.type,
                "x": click_x,
                "y": click_y,
                "element_box": element_box,
                "timestamp": action.timestamp,
            }) + "\n")
            manifest.flush()
            count += 1

        manifest.close()

    return count
