"""Per-chunk scrub-state marker for the encrypted corpus (search U8 / KTD2).

``frame.read`` (and, later, full-still app views) must REFUSE a frame whose chunk has
been captured — encrypted — but not yet secrets-scrubbed at index time: in the
window between capture and the index pass, a still still contains unredacted
secrets, so serving its decrypted bytes would leak them (the corpus key sits behind
the shared-group entitlement, so an agent cannot otherwise read the still — making
this a real boundary, not just defense-in-depth).

This records, per recording, the ``[start_ms, end_ms)`` ranges whose stills the
index pass has secrets-scrubbed, in a **local-only** ``.scrub_state.json`` sidecar in
the recording dir (never uploaded — excluded from the cloud copytree + the upload
denylist). A failed scrub/index pass simply never records the range, so a frame
stays refused until a later pass scrubs it (fail-closed retry, KTD2).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = ".scrub_state.json"


def _state_path(recording_dir: Path) -> Path:
    return Path(recording_dir) / STATE_FILE


def scrubbed_ranges(recording_dir: Path) -> list[tuple[int, int]]:
    """Return the recorded scrubbed ``(start_ms, end_ms)`` ranges (empty on absence)."""
    path = _state_path(recording_dir)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    ranges = raw.get("scrubbed_ranges") if isinstance(raw, dict) else None
    if not isinstance(ranges, list):
        return []
    out: list[tuple[int, int]] = []
    for item in ranges:
        if isinstance(item, list) and len(item) == 2:
            try:
                out.append((int(item[0]), int(item[1])))
            except (TypeError, ValueError):
                continue
    return out


def is_frame_scrubbed(recording_dir: Path, ts_ms: int) -> bool:
    """True if ``ts_ms`` falls in any recorded scrubbed range (half-open)."""
    return any(start <= ts_ms < end for start, end in scrubbed_ranges(recording_dir))


def mark_chunk_scrubbed(recording_dir: Path, start_ms: int, end_ms: int) -> None:
    """Record ``[start_ms, end_ms)`` as scrubbed (idempotent, atomic).

    Merges the new range with the existing set and atomically replaces the sidecar.
    A single recording has one index writer at a time, so a read-modify-write here
    is safe against itself; the atomic replace keeps a concurrent reader consistent.
    Never raises into the (fail-open) index pass."""
    try:
        existing = scrubbed_ranges(recording_dir)
        merged = _merge_ranges([*existing, (int(start_ms), int(end_ms))])
        path = _state_path(recording_dir)
        payload = json.dumps({"scrubbed_ranges": [[s, e] for s, e in merged]}, separators=(",", ":"))
        _atomic_write(path, payload)
    except Exception:  # noqa: BLE001 — marker write must never break indexing
        logger.warning("scrub_state: failed to record scrubbed range", exc_info=True)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort + coalesce overlapping/adjacent ranges."""
    cleaned = sorted((s, e) for s, e in ranges if e > s)
    if not cleaned:
        return []
    merged = [cleaned[0]]
    for s, e in cleaned[1:]:
        ls, le = merged[-1]
        if s <= le:  # overlap or touch → coalesce
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _atomic_write(path: Path, text: str) -> None:
    directory = str(path.parent) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".scrub_state.tmp")
    closed = False
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        closed = True
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = [
    "STATE_FILE",
    "scrubbed_ranges",
    "is_frame_scrubbed",
    "mark_chunk_scrubbed",
]
