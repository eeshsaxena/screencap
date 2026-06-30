"""Pure nearest-frame resolution core (SCR-186 U1).

Maps a ``(recording, timestamp_ms)`` search pointer to the nearest on-disk
screenshot **stem** under ``~/.screencap/recordings/<name>/screenshots/`` — the
daemon-side Python port of the Swift ``FrameSelection.nearest`` + ``loadFrames``
algorithm (``macos/ScreenCap/Controllers/RecordingFrameIndex.swift``). It returns
a bare stem (e.g. ``"1719400010.000000"``) plus a signed ``delta_ms`` — never a
path and never image bytes, so the daemon query surface stays pointer-only
(priv-R8); the agent builds ``screenshots/<stem>.jpg`` itself with its same-EUID
filesystem access.

This module is intentionally **pure**: filesystem listing + selection only, no
``screencap.daemon`` import and no ``recording.db`` read. The ALLOW-only
blocked-frame filter (SCR-186 U6) is supplied by the caller as an injected
``is_blocked(ts)`` predicate, so the privacy machinery (``derive_skip_intervals``
+ ``find_blocked_interval``) is reused in the daemon adapter rather than forked
here, and the selector stays trivially unit-testable with a fake predicate.

Parity notes vs. the Swift source (port from the *verified* Swift behaviour):

* **Rounding.** Swift's bare ``(ts * 1000).rounded()`` is round-half-**away from
  zero** (``.toNearestOrAwayFromZero``). Python's built-in ``round`` is banker's
  rounding (round-half-to-even) and diverges at an exact half-millisecond, so we
  use :func:`_round_half_away` (``floor(x + 0.5)``, valid for the non-negative
  epoch domain), NOT ``round``.
* **Selection.** Absolute-nearest by ``abs(frame_ms - anchor_ms)`` (not
  nearest-prior — a search anchor can sit just after the last frame). On an exact
  tie the earliest frame wins, which falls out of ``min`` over an
  ascending-sorted list (and matches Swift's strict-``<`` ``min`` predicate).
* **Cap.** Inclusive: a frame resolves iff ``abs(delta) <= staleness_cap_ms``.
* **Listing.** ``*.jpg`` only (matching Swift; the reused
  :func:`~screencap.redaction.geometry.parse_screenshot_timestamp` also accepts
  ``.jpeg``, which we deliberately exclude via the glob).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from screencap.redaction.geometry import parse_screenshot_timestamp

# Default staleness cap (~30s), matching the Swift ``RecordingFrameIndex`` init.
# Content/timeline hits map essentially exactly; the cap guards a sparse
# recording (or a chunk-coarse transcript anchor) from snapping to a misleading
# frame minutes from the matched moment. Transcript resolution overrides this
# with a chunk-scaled cap (SCR-186 R9).
DEFAULT_STALENESS_CAP_MS = 30_000


@dataclass(frozen=True)
class Frame:
    """A captured frame: its epoch timestamp (seconds + milliseconds) and stem.

    ``ts`` (epoch seconds) is retained so the caller's ``is_blocked`` predicate —
    which works in the recording-db's native seconds domain — can be applied
    without re-parsing. ``stem`` is the filename minus ``.jpg``; the agent expands
    it to ``screenshots/<stem>.jpg`` itself (pointer-only, priv-R8).
    """

    ts: float
    ms: int
    stem: str


def _round_half_away(x: float) -> int:
    """Round ``x`` half-away-from-zero, matching Swift ``Double.rounded()``.

    Valid for the non-negative epoch domain (frame timestamps are positive).
    ``floor(x + 0.5)`` rounds a ``.5`` boundary up (away from zero), where Python's
    built-in ``round`` would round to even — the divergence the Swift parity
    requires us to avoid.
    """
    return int(math.floor(x + 0.5))


def load_frames(screenshots_dir: Path) -> list[Frame]:
    """List ``screenshots_dir``'s ``*.jpg`` frames, parsed and sorted ascending.

    Globs ``*.jpg`` only (``.jpeg``/``.png`` excluded), parses each ``{epoch}.jpg``
    stem to a timestamp (skipping non-numeric stems), and sorts ascending by
    millisecond. Returns ``[]`` when the directory is missing — never raises.
    """
    screenshots_dir = Path(screenshots_dir)
    if not screenshots_dir.is_dir():
        return []
    frames: list[Frame] = []
    for img_path in screenshots_dir.glob("*.jpg"):
        ts = parse_screenshot_timestamp(img_path.name)
        if ts is None:
            continue
        stem = img_path.name[: -len(".jpg")]
        frames.append(Frame(ts=ts, ms=_round_half_away(ts * 1000.0), stem=stem))
    frames.sort(key=lambda f: f.ms)
    return frames


def nearest_frame(
    frames: Sequence[Frame],
    timestamp_ms: int,
    staleness_cap_ms: int = DEFAULT_STALENESS_CAP_MS,
) -> tuple[str, int] | None:
    """Nearest frame to ``timestamp_ms`` within the cap, as ``(stem, delta_ms)``.

    Absolute-nearest by ``abs(frame.ms - timestamp_ms)``; earliest-wins on an
    exact tie (``min`` keeps the first of an ascending-sorted list). ``delta_ms``
    is signed (``chosen.ms - timestamp_ms``; negative ⇒ the frame precedes the
    anchor). Returns ``None`` for an empty list or when the nearest frame is
    beyond the inclusive staleness cap.

    Pure selection: callers wanting the ALLOW-only filter pre-filter ``frames``
    (see :func:`resolve_nearest`).
    """
    if not frames:
        return None
    chosen = min(frames, key=lambda f: abs(f.ms - timestamp_ms))
    delta_ms = chosen.ms - timestamp_ms
    if abs(delta_ms) <= staleness_cap_ms:
        return chosen.stem, delta_ms
    return None
