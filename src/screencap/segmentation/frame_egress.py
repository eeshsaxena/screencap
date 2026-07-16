"""Masked-still egress producer (SCR-272 U1).

Given a recording directory and a ``[start_ts, end_ts)`` window, produce the set
of masked frame bytes that are eligible to leave the machine (e.g. as multimodal
evidence attached to a cloud-bound Intelligence task). Downstream units (U3/U5)
call :func:`produce_egress_frames` and attach the returned
:class:`~screencap.segmentation.generation.MaskedFrame` values.

This module is the SOLE blessed constructor of a
:class:`~screencap.segmentation.generation.MaskedFrame` marked ``masked=True`` —
the one type allowed to carry frame bytes to a provider (SCR-272, R12). Because
every frame here has passed the structural ALLOW gate and the best-effort residual
mask, minting them with ``masked=True`` is the provenance stamp the provider seam's
:func:`~screencap.segmentation.generation.verify_masked_frames` fail-closed guard
checks. An AST guard in ``tests/segmentation/test_generation.py`` pins that no
other module mints a ``MaskedFrame(masked=True)`` (mirroring KTD11).

Guarantee model (from the plan)
-------------------------------
* **Blocked-app boundary is STRUCTURAL and fail-closed.** Every candidate frame
  is tested against the recording's re-derived ALLOW-only skip set via
  :func:`screencap.frame_blocked.build_is_blocked` (the same
  ``derive_skip_intervals(require_canonical=True)`` machinery ``frame.nearest``
  uses). ``build_is_blocked`` maps ANY derivation failure — missing/partial
  ``recording.db``, a partial canonical read, a broken privacy config — to an
  all-blocked sentinel, so a frame is emitted ONLY when its ALLOW eligibility was
  positively derived. If eligibility cannot be derived, ZERO frames leave (AE4 /
  R10). A frame overlapping a blocked-app interval is excluded (AE2 / R8).

* **Residual within-frame masking is BEST-EFFORT OCR — NOT a coverage proof.**
  For a surviving ALLOW frame we OCR the still and paint out detected
  secret/PII regions best-effort. This is a defence-in-depth pass on top of the
  structural boundary; it is *not* claimed to provably remove all sensitive
  content. When the OCR pipeline is unavailable the pass is skipped (the frame
  still leaves — the structural boundary is the guarantee).

* **Withhold (fail-closed) conditions.** A frame is dropped — never emitted raw —
  when (a) its ALLOW eligibility cannot be derived (handled structurally above),
  or (b) loading/masking the still raises. A raise anywhere in the per-frame
  produce path drops that one frame; it is never sent unmasked.

* **Never mutate the on-disk original.** The still is read (decrypted to RAM for
  ``*.jpg.enc``) and masked into an in-memory buffer via
  :func:`screencap.redaction.masking.mask_regions_to_bytes`; the
  ``screenshots/*.jpg`` files are never written (R9).

Near-identical consecutive frames are deduped (the repo's ``dhash`` /
``hamming_distance``, mirroring :func:`screencap.index_core.index_range`), and the
survivor count is capped (deterministic) so a pathological burst cannot balloon
the egress payload.

This module is deliberately cloud-free (no ``google.cloud`` / ``genai`` imports)
so it stays importable inside the daemon; heavy imports (scrubber / OCR / PIL) are
deferred into :func:`produce_egress_frames`.
"""

from __future__ import annotations

import bisect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from screencap.segmentation.generation import MaskedFrame

if TYPE_CHECKING:
    from screencap.privacy.mask_primitives import MaskRegion

logger = logging.getLogger(__name__)

# Hard cap on frames produced per window so an action-burst can never balloon the
# egress payload / OCR work. Deterministic (a re-run truncates identically).
_EGRESS_MAX_FRAMES = 240

# dHash near-duplicate threshold, matching the content-index OCR-dedup value
# (``index_core._INDEX_DHASH_THRESHOLD``): a false dup only costs a dropped
# near-identical frame, so the tight value is intentional.
_EGRESS_DHASH_THRESHOLD = 5

# Default half-window (seconds) an ``only_timestamps`` target is matched to the
# nearest on-disk still. Recall passes the retrieved snippets' individual pointer
# timestamps — content-index hits land exactly on a still, but timeline/transcript
# pointers are ``window_event`` / chunk-start times a couple seconds off the nearest
# still. Kept small so a still that is NOT near any retrieved timestamp (an
# un-retrieved moment inside the [min,max] span) is never selected.
_EGRESS_ONLY_TS_TOLERANCE_S = 2.0


class _BytesOcrAdapter:
    """Feed already-decrypted still bytes to the path-based ``ocr_mask_screenshot``.

    ``scrubber.ocr_mask_screenshot`` (the region-detection we reuse) calls
    ``ocr.recognize(image_path, roi=...)``. Wrapping the real OCR engine so
    ``recognize`` ignores the path and delegates to ``recognize_bytes`` lets us
    reuse its full region-mapping logic (offset maps, char bboxes, per-block
    fail-closed) for BOTH plaintext and encrypted stills without ever writing
    decrypted plaintext to disk.
    """

    def __init__(self, ocr: object, img_bytes: bytes) -> None:
        self._ocr = ocr
        self._bytes = img_bytes

    def recognize(self, _image_path: object, *, roi: object = None) -> object:
        return self._ocr.recognize_bytes(self._bytes, roi=roi)


def _build_default_detector() -> "Callable[[bytes], list[MaskRegion]] | None":
    """Build the default residual-region detector (Apple Vision OCR + PII pipeline).

    Returns ``None`` when the OCR pipeline is unavailable (Vision not installed, or
    construction failed) — the caller then skips the best-effort residual pass
    rather than dropping frames (the structural boundary is the guarantee).
    """
    try:
        from screencap.redaction import create_default_pipeline
        from screencap.redaction.ocr import VisionOcr

        ocr = VisionOcr()
        pipeline = create_default_pipeline()
    except Exception:
        logger.warning(
            "frame-egress: OCR pipeline unavailable; residual within-frame masking "
            "skipped (best-effort). The structural blocked-app boundary still holds."
        )
        return None

    from screencap.scrubber import ocr_mask_screenshot

    def _detect(img_bytes: bytes) -> "list[MaskRegion]":
        # The path arg is ignored — the adapter OCRs the in-memory bytes.
        return ocr_mask_screenshot(
            Path("<frame-egress-in-memory>"),
            pipeline,
            _BytesOcrAdapter(ocr, img_bytes),
        )

    return _detect


def _nearest_index(sorted_ts: list[float], target: float) -> "int | None":
    """Index of the value in ascending ``sorted_ts`` closest to ``target``.

    Only the two neighbours bracketing ``target`` can be the minimum, so a bisect
    suffices. Returns ``None`` for an empty list.
    """
    if not sorted_ts:
        return None
    i = bisect.bisect_left(sorted_ts, target)
    best: "int | None" = None
    for j in (i - 1, i):
        if 0 <= j < len(sorted_ts):
            if best is None or abs(sorted_ts[j] - target) < abs(sorted_ts[best] - target):
                best = j
    return best


def produce_egress_frames(
    recording_dir: Path | str,
    start_ts: float,
    end_ts: float,
    *,
    corpus_key: bytes | None = None,
    detect_regions: "Callable[[bytes], list[MaskRegion]] | None" = None,
    max_frames: int = _EGRESS_MAX_FRAMES,
    dhash_threshold: int = _EGRESS_DHASH_THRESHOLD,
    only_timestamps: "Iterable[float] | None" = None,
    only_timestamps_tolerance_s: float = _EGRESS_ONLY_TS_TOLERANCE_S,
) -> list[MaskedFrame]:
    """Return the masked stills in ``[start_ts, end_ts)`` eligible to leave.

    ``recording_dir`` is a ``~/.screencap/recordings/<name>/`` directory; the flat
    ``screenshots/*.jpg`` (and, when ``corpus_key`` is given, ``*.jpg.enc``) files
    are the candidate frames. ``start_ts`` / ``end_ts`` are epoch seconds (the same
    unit the screenshot filenames encode).

    For every candidate frame:

    1. **Structural ALLOW gate (fail-closed).** The frame is dropped unless
       :func:`screencap.frame_blocked.build_is_blocked` positively clears it. Any
       derivation failure → all frames blocked → ``[]`` returned (AE4 / R10); a
       frame inside a blocked-app interval is excluded (AE2 / R8).
    2. **Dedup.** Near-identical consecutive survivors are collapsed.
    3. **Best-effort residual mask.** The still is loaded (decrypted to RAM for
       ``*.jpg.enc``), OCR-detected residual regions are painted out in memory, and
       the masked bytes are returned. Loading/masking that raises drops that one
       frame (never emitted raw). The on-disk original is never written (R9).

    ``detect_regions`` (test / caller seam): ``(img_bytes) -> list[MaskRegion]``,
    the residual detector. Default: Apple Vision OCR + the PII pipeline via
    :func:`scrubber.ocr_mask_screenshot`; skipped (no residual regions) when that
    pipeline is unavailable. A detector that raises drops the frame (fail-closed).

    ``only_timestamps`` (recall egress scoping): when given, a still is a candidate
    ONLY if it lands within ``only_timestamps_tolerance_s`` seconds of one of these
    epoch-second targets — the *individual* retrieved-snippet timestamps, NOT their
    ``[min, max]`` span. This is applied BEFORE the structural ALLOW gate so a
    moment inside the window that was never retrieved (a still with no nearby
    target) is excluded up front and never even OCR-tested. An empty iterable
    selects nothing (``[]``). ``None`` (the default) keeps every windowed still.
    """
    import io

    from PIL import Image

    from screencap import still_io
    from screencap.engine.dedup import dhash, hamming_distance
    from screencap.frame_blocked import build_is_blocked
    from screencap.redaction.geometry import parse_screenshot_timestamp
    from screencap.redaction.masking import mask_regions_to_bytes

    recording_dir = Path(recording_dir)
    screenshots_dir = recording_dir / "screenshots"
    if not screenshots_dir.is_dir():
        return []

    # Encrypted stills (``*.jpg.enc``) are only readable with the corpus key, so
    # glob them only when one is supplied (mirrors index_core.index_range). Parse
    # the timestamp off the logical name so both forms sort into one timeline.
    globs = ("*.jpg", "*.jpg.enc") if corpus_key is not None else ("*.jpg",)
    parsed: list[tuple[float, Path]] = []
    for pattern in globs:
        for img_path in screenshots_dir.glob(pattern):
            ts = parse_screenshot_timestamp(still_io.logical_still_name(img_path))
            if ts is not None:
                parsed.append((ts, img_path))
    parsed.sort(key=lambda p: p[0])

    candidates = [(ts, p) for ts, p in parsed if start_ts <= ts < end_ts]
    if not candidates:
        return []

    # Explicit-timestamps filter (recall): keep ONLY the single still NEAREST each
    # retrieved target (within tolerance), so a moment inside the span that was never
    # retrieved never ships — and an adjacent still that is not the closest match to
    # any target is dropped too. Applied before the structural gate: a target with no
    # still within tolerance simply contributes no frame. ``candidates`` is already
    # ascending by ts (parsed.sort + the window filter preserve order).
    if only_timestamps is not None:
        targets = [float(t) for t in only_timestamps]
        if not targets:
            return []
        tol = only_timestamps_tolerance_s
        cand_ts = [ts for ts, _ in candidates]
        keep: set[int] = set()
        for t in targets:
            j = _nearest_index(cand_ts, t)
            if j is not None and abs(cand_ts[j] - t) <= tol:
                keep.add(j)
        candidates = [candidates[i] for i in sorted(keep)]
        if not candidates:
            return []

    # Structural, fail-closed ALLOW gate. build_is_blocked re-derives the same
    # ALLOW-only skip set frame.nearest uses (screenshot_residuals=True — these ARE
    # on-disk screenshot files) and maps ANY derivation error to all-blocked.
    frame_tss = [ts for ts, _ in candidates]
    is_blocked = build_is_blocked(recording_dir, frame_tss)
    allow = [(ts, p) for ts, p in candidates if not is_blocked(ts)]
    if not allow:
        return []

    # Deterministic cap: a re-run truncates identically, so this never re-orders or
    # drops a frame a resume would recover.
    if len(allow) > max_frames:
        logger.info(
            "frame-egress: capping at %d of %d eligible frames", max_frames, len(allow)
        )
        allow = allow[:max_frames]

    detector = detect_regions
    if detector is None:
        detector = _build_default_detector() or (lambda _b: [])

    out: list[MaskedFrame] = []
    prev_hash: int | None = None
    for ts, img_path in allow:
        # Load the still (decrypt to RAM for *.jpg.enc). An unreadable/undecryptable
        # still is dropped — fail closed, never emit a frame we could not read.
        try:
            img_bytes = still_io.open_still(img_path, corpus_key)
        except Exception:
            logger.info(
                "frame-egress: dropping unreadable still %s", img_path.name
            )
            continue

        # Dedup near-identical consecutive frames (same primitive + threshold the
        # content index uses); keep prev_hash on a dup so we compare against the
        # last DISTINCT frame.
        cur_hash: int | None = None
        try:
            with Image.open(io.BytesIO(img_bytes)) as im:
                cur_hash = dhash(im)
        except Exception:
            cur_hash = None
        if (
            cur_hash is not None
            and prev_hash is not None
            and hamming_distance(cur_hash, prev_hash) <= dhash_threshold
        ):
            continue
        if cur_hash is not None:
            prev_hash = cur_hash

        # Best-effort residual mask -> in-memory bytes. A raise anywhere here
        # (OCR detection or the paint) drops THIS frame — never emitted raw — and
        # never touches the on-disk original.
        try:
            regions = detector(img_bytes)
            masked = mask_regions_to_bytes(img_bytes, regions)
        except Exception:
            logger.info(
                "frame-egress: dropping frame whose masking raised", exc_info=True
            )
            continue

        # SOLE blessed mint of a masked=True frame — this is the provenance stamp
        # verify_masked_frames checks before any frame reaches a provider (R12).
        out.append(
            MaskedFrame(
                jpeg_bytes=masked,
                timestamp_ms=int(round(ts * 1000)),
                masked=True,
            )
        )

    return out
