#!/usr/bin/env python3
"""Extract per-block OCR + accessibility text from a real ScreenCap recording.

Builds the raw side of the Tier-2 testbed: the noisy, fragmentary OCR /
accessibility-tree text that is the spike's *decisive* distribution. Output is
per **block** (one OCR text block or one ``AXValue`` string per line) — never a
concatenated screen — because the production scrubber detects per block, and
feeding a 128k-context model whole documents would inflate privacy-filter above
production behavior.

Reads ``~/.screencap/recordings/<name>/recording.db`` via raw ``sqlite3``
(read-only), mirroring the two production PII-bearing surfaces:

* **OCR** — Apple Vision over the screenshot files (``screenshot.image_path``,
  falling back to the ``png_data`` blob), one ``OcrTextBlock`` per line.
* **AX** — ``AXValue`` strings inside ``action_event.element_state`` JSON
  (top-level and nested), matching ``scrubber.py``'s read path.

Output ``inputs.jsonl`` line:
    {"id": "...", "text": "...", "modality": "ocr"|"ax", "source": "..."}

⚠️  Output contains real PII and is gitignored. Hand-label ``gold.jsonl`` from it
per ``ANNOTATION_GUIDELINES.md``. Regenerate locally; never commit the extract.

Usage::

    PYTHONPATH=src python benchmarks/scr28/tier2_testbed/extract.py \
        --recording v15-pii-positive --max-screenshots 25 \
        --out benchmarks/scr28/tier2_testbed/inputs.jsonl
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _p in (_PROJECT_ROOT / "src", _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

RECORDINGS_DIR = Path.home() / ".screencap" / "recordings"


# ---------------------------------------------------------------------------
# DB helpers (raw sqlite3, read-only, with table/column guards)
# ---------------------------------------------------------------------------


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(str(db_path))


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


# ---------------------------------------------------------------------------
# AX text
# ---------------------------------------------------------------------------


def _collect_ax_values(obj: object, out: list[str], depth: int = 0) -> None:
    """Recursively collect non-empty ``AXValue`` strings (top-level + nested)."""
    if depth > 50:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "AXValue" and isinstance(value, str) and value.strip():
                out.append(value)
            else:
                _collect_ax_values(value, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _collect_ax_values(value, out, depth + 1)


def extract_ax_blocks(conn: sqlite3.Connection, limit: int = 0) -> list[tuple[str, str]]:
    """Return ``(source_ref, text)`` for each AXValue string in action_event."""
    if not _has_column(conn, "action_event", "element_state"):
        return []
    rows = conn.execute(
        "SELECT id, element_state FROM action_event "
        "WHERE element_state IS NOT NULL ORDER BY timestamp"
    ).fetchall()
    blocks: list[tuple[str, str]] = []
    for row_id, es_raw in rows:
        if not es_raw:
            continue
        try:
            es = json.loads(es_raw) if isinstance(es_raw, str) else es_raw
        except (json.JSONDecodeError, TypeError):
            continue
        values: list[str] = []
        _collect_ax_values(es, values)
        for value in values:
            blocks.append((f"action_event:{row_id}", value))
        if limit and len(blocks) >= limit:
            break
    return blocks


# ---------------------------------------------------------------------------
# OCR text
# ---------------------------------------------------------------------------


def _screenshot_files(
    conn: sqlite3.Connection, rec_dir: Path, max_screenshots: int
) -> list[tuple[str, Path]]:
    """Return ``(source_ref, image_path)`` for up to ``max_screenshots`` shots.

    Prefers the on-disk ``image_path``; falls back to writing the ``png_data``
    blob to a temp file. Temp files are caller-cleaned via the returned list.
    """
    if not _has_table(conn, "screenshot"):
        return []
    has_path = _has_column(conn, "screenshot", "image_path")
    has_blob = _has_column(conn, "screenshot", "png_data")
    cols = ["id"]
    if has_path:
        cols.append("image_path")
    if has_blob:
        cols.append("png_data")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM screenshot ORDER BY timestamp"
    ).fetchall()

    out: list[tuple[str, Path]] = []
    for row in rows:
        if len(out) >= max_screenshots:
            break
        row_id = row[0]
        rec = dict(zip(cols, row))
        rel = rec.get("image_path")
        if rel:
            candidate = rec_dir / rel
            if candidate.exists():
                out.append((f"screenshot:{row_id}:{rel}", candidate))
                continue
        blob = rec.get("png_data")
        if blob:
            tmp = Path(tempfile.mkstemp(suffix=".png", prefix="scr28-ocr-")[1])
            tmp.write_bytes(blob)
            out.append((f"screenshot:{row_id}:blob", tmp))
    return out


def extract_ocr_blocks(
    conn: sqlite3.Connection, rec_dir: Path, max_screenshots: int
) -> list[tuple[str, str]]:
    """Return ``(source_ref, text)`` for each OcrTextBlock across screenshots."""
    try:
        from screencap.privacy.ocr import VisionOcr
    except Exception as exc:  # pragma: no cover - platform/Vision unavailable
        print(f"[warn] OCR unavailable ({type(exc).__name__}); skipping OCR.", file=sys.stderr)
        return []

    files = _screenshot_files(conn, rec_dir, max_screenshots)
    if not files:
        return []

    ocr = VisionOcr()
    blocks: list[tuple[str, str]] = []
    for source_ref, img_path in files:
        try:
            result = ocr.recognize(img_path)
        except Exception as exc:  # pragma: no cover - corrupt/missing image
            print(f"[warn] OCR failed for {source_ref}: {type(exc).__name__}", file=sys.stderr)
            continue
        finally:
            if source_ref.endswith(":blob"):
                img_path.unlink(missing_ok=True)
        for i, block in enumerate(result.text_blocks):
            blocks.append((f"{source_ref}#block{i}", block.text))
    return blocks


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _resolve_recording(recording: str) -> Path:
    candidate = Path(recording).expanduser()
    if candidate.is_dir():
        return candidate
    rec_dir = RECORDINGS_DIR / recording
    if rec_dir.is_dir():
        return rec_dir
    raise SystemExit(f"recording not found: {recording!r} (looked in {RECORDINGS_DIR})")


def extract_recording(
    rec_dir: Path, *, max_screenshots: int, max_ax: int, min_len: int, dedup: bool
) -> list[dict]:
    db_path = rec_dir / "recording.db"
    if not db_path.exists():
        raise SystemExit(f"no recording.db in {rec_dir}")
    rec_name = rec_dir.name

    conn = _connect_ro(db_path)
    try:
        ax = extract_ax_blocks(conn, limit=max_ax)
        ocr = extract_ocr_blocks(conn, rec_dir, max_screenshots)
    finally:
        conn.close()

    records: list[dict] = []
    seen: set[str] = set()
    counters = {"ocr": 0, "ax": 0}
    for modality, blocks in (("ocr", ocr), ("ax", ax)):
        for source_ref, text in blocks:
            text = text.strip()
            if len(text) < min_len:
                continue
            if dedup:
                key = f"{modality}\x00{text}"
                if key in seen:
                    continue
                seen.add(key)
            idx = counters[modality]
            counters[modality] += 1
            records.append(
                {
                    "id": f"{rec_name}-{modality}-{idx:04d}",
                    "text": text,
                    "modality": modality,
                    "source": f"{rec_name}#{source_ref}",
                }
            )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Tier-2 OCR + AX text blocks")
    parser.add_argument(
        "--recording", required=True, help="Recording name under ~/.screencap/recordings/ or a path"
    )
    parser.add_argument("--max-screenshots", type=int, default=25, help="Max screenshots to OCR")
    parser.add_argument("--max-ax", type=int, default=0, help="Max AX blocks (0 = all)")
    parser.add_argument("--min-len", type=int, default=3, help="Skip blocks shorter than this")
    parser.add_argument("--no-dedup", action="store_true", help="Keep duplicate blocks")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "inputs.jsonl"),
        help="Output JSONL path (default: tier2_testbed/inputs.jsonl)",
    )
    args = parser.parse_args()

    rec_dir = _resolve_recording(args.recording)
    records = extract_recording(
        rec_dir,
        max_screenshots=args.max_screenshots,
        max_ax=args.max_ax,
        min_len=args.min_len,
        dedup=not args.no_dedup,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    n_ocr = sum(1 for r in records if r["modality"] == "ocr")
    n_ax = sum(1 for r in records if r["modality"] == "ax")
    print(f"Extracted {len(records)} blocks ({n_ocr} ocr, {n_ax} ax) -> {out_path}")
    print("⚠️  Contains real PII — gitignored. Hand-label gold.jsonl per ANNOTATION_GUIDELINES.md.")


if __name__ == "__main__":
    main()
