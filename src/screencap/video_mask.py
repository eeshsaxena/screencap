"""Post-hoc video-frame masking for cloud-bound chunk copies (U6).

This is the single highest privacy-risk component in the unified pipeline.
The contract is **fail-closed**: a masking failure, or coverage that cannot be
*proven* complete, MUST NOT yield an unscrubbed cloud video. It produces a
masked copy ONLY when the recorded ``window_geometry`` timeline lets us prove
per-interval coverage; otherwise it returns a ``FAILED`` outcome and writes no
masked copy at all.

Why post-hoc masking is dangerous (the "no-op looks like success" trap, see
``docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md``):
a masker that draws nothing produces a *clear* video that looks identical to a
correctly-masked one. ``window_geometry`` is sampled sparsely — only alongside
screenshots (~1 fps while typing, faster on drag/scroll) and the capture-time
insert is best-effort. Video frames are continuous (~20 fps). Between two
geometry samples a sensitive window can open / move / resize / close entirely
unobserved, and a naive "mask the rectangles I have" pass leaves those frames
clear. A positive "regions masked > 0" assertion CANNOT catch this, because
"no geometry sample" is indistinguishable from "no sensitive window". The
three-way coverage gate below is what actually guards safety.

The masker is a pure function over (chunk mp4 + recorded geometry). It does not
touch the ledger or the upload path — the caller (U7 terminal stage) maps the
returned :class:`MaskOutcome` onto the per-chunk ledger
(``mark_scrubbed`` / ``mark_failed``) and decides upload/eviction. It is also
flag-independent: ``scrubber`` only *invokes* it when
``config.get_masked_video_upload_enabled()`` is ON (see
``scrubber.mask_video_chunk_for_cloud``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from uuid import uuid4

import av
from loguru import logger

from screencap.engine.video import _close_container_in_thread, _sweep_stale_temps

# ---------------------------------------------------------------------------
# Coverage policy — THE load-bearing correctness decision (U6 open question).
# ---------------------------------------------------------------------------

# Maximum tolerated gap (seconds) between two consecutive window_geometry
# samples — and between a chunk's span edge and its nearest sample — before
# coverage is treated as UNPROVABLE and the chunk fails closed (case b).
#
# Worst-case-gap reasoning (the open question the plan says not to defer):
#   * Geometry is recorded only alongside screenshots. Capture is action-gated,
#     so the sample rate tracks user activity: ~1 sample/sec while typing,
#     faster on drag/scroll, and SPARSER (or zero) while the user pauses.
#   * Video is continuous (~SCREEN_CAPTURE_FPS = 20 fps). Within any gap with no
#     geometry sample, a sensitive window's bounds are simply unknown — it may
#     open, move, resize, or close, and those video frames would ship clear.
#   * A typing-only session's nominal inter-sample gap is ~1s. We pick 2.0s:
#     2x the nominal cadence, enough headroom to tolerate scheduler jitter and
#     a skipped frame or two WITHOUT silently absorbing a multi-second blind
#     window in which a sensitive surface could appear and vanish unseen.
#   * This is a PRECONDITION, not a tuning knob: a longer real gap is a genuine
#     "we cannot prove what was on screen" signal and MUST fail closed, never
#     be widened to make a chunk pass. Tightening it only ever fails MORE chunks
#     closed (safe); loosening it trades safety for fewer failures and must be a
#     deliberate, reviewed privacy decision.
MAX_GEOMETRY_GAP_SECONDS: float = 2.0

# Conservative spatial over-mask. A sensitive window may move between two
# samples; rather than point-interpolate its rectangle (which would leave a
# partially-uncovered swept region), we HOLD the sensitive rectangle across the
# whole inter-sample interval AND union-dilate it by this many pixels so a small
# drift between samples is over-covered, never under-covered. Over-masking is
# the safe error direction; under-masking leaks pixels.
MASK_DILATE_PX: int = 8

# Encoder settings for the masked copy. libx264 explicitly (deterministic
# cross-machine output, guaranteed bundled) and yuv420p (AVKit-safe), mirroring
# engine/video.remediate_pixfmt_for_review. We re-encode every frame because
# masking mutates pixels, so a stream-copy is impossible.
_MASK_CODEC = "libx264"
_MASK_PIX_FMT = "yuv420p"

# Opaque fill for masked rectangles. Matches privacy.masking._MASK_COLOR
# (near-black) so the visual treatment is consistent with screenshot masking.
_MASK_FILL = (30, 30, 30)


class MaskOutcomeStatus(str, Enum):
    """Result of masking one cloud chunk.

    Three distinct terminal states — the caller MUST distinguish "did nothing
    because provably safe" (UNMASKED_PROVABLY_SAFE) from "did nothing because
    it failed / is unprovable" (FAILED). Conflating them is exactly the
    silent-no-op hazard this unit exists to prevent.
    """

    #: Case (c): sensitive windows present → masked copy produced, regions > 0.
    MASKED = "masked"
    #: Case (a): geometry densely covers the span AND proves NO sensitive
    #: window → a faithful UNMASKED copy is valid and was produced.
    UNMASKED_PROVABLY_SAFE = "unmasked_provably_safe"
    #: Case (b) / any error: coverage unprovable (sparse/missing geometry, a
    #: capture-failure marker) or the masking machinery failed → NO copy
    #: produced; upload + eviction blocked.
    FAILED = "failed"


@dataclass(frozen=True)
class MaskOutcome:
    """Outcome of :func:`mask_video_chunk`.

    ``output_path`` is set only for MASKED / UNMASKED_PROVABLY_SAFE; it is the
    complete, atomically-materialized masked (or faithful) copy. For FAILED it
    is ``None`` and no copy exists on disk — the caller marks the chunk FAILED.
    """

    status: MaskOutcomeStatus
    output_path: Path | None = None
    #: Total mask rectangles drawn across all frames (the positive,
    #: necessary-but-not-sufficient assertion: > 0 for MASKED).
    regions_masked: int = 0
    frames_total: int = 0
    #: Human-readable explanation — distinguishes case (a) "proven safe" from
    #: case (b) "unprovable" from an error, for logs and the caller's detail.
    reason: str = ""

    @property
    def ok(self) -> bool:
        """True when a copy was produced (MASKED or UNMASKED_PROVABLY_SAFE)."""
        return self.status in (
            MaskOutcomeStatus.MASKED,
            MaskOutcomeStatus.UNMASKED_PROVABLY_SAFE,
        )


@dataclass
class _SensitiveInterval:
    """A held-and-dilated sensitive rectangle, valid over [start, end) abs ts.

    Window dicts carry global-coordinate bounds; ``display_origin`` (from the
    geometry snapshot they came from) maps them into the screenshot/frame
    coordinate space, exactly as ``mask_screenshots`` does for screenshots.
    """

    start: float
    end: float
    windows: list[dict] = field(default_factory=list)
    display_origin: tuple[float, float] = (0.0, 0.0)


class _CoverageError(RuntimeError):
    """Coverage could not be proven for the chunk span (case b). Fail closed."""


def _build_coverage(
    db_path: Path,
    start_ts: float,
    end_ts: float,
) -> list[float]:
    """Return the geometry sample timestamps in [start_ts, end_ts], or raise.

    Enforces the three-way gate's case (b) FIRST:
      1. Any geometry-capture-failure marker in the span → unprovable.
      2. No samples at all in the span → unprovable ("no sample" is NOT
         "0 expected, 0 masked").
      3. Any inter-sample gap (including span-edge → first/last sample)
         exceeding ``MAX_GEOMETRY_GAP_SECONDS`` → unprovable.

    Raises :class:`_CoverageError` on any of the above (the caller turns this
    into a FAILED outcome).
    """
    from screencap.privacy.context import (
        geometry_capture_failures_in_span,
        list_geometry_sample_timestamps,
    )

    # (b.1) A known capture-failure marker means a geometry insert is KNOWN to
    # have failed — coverage there is unprovable regardless of samples present.
    failures = geometry_capture_failures_in_span(db_path, start_ts, end_ts)
    if failures:
        raise _CoverageError(
            f"{len(failures)} geometry-capture-failure marker(s) in span "
            f"[{start_ts:.3f}, {end_ts:.3f}] — coverage unprovable"
        )

    samples = list_geometry_sample_timestamps(db_path, start_ts, end_ts)
    # (b.2) No geometry at all across the span — must NOT be read as "no
    # sensitive window". It is "we don't know what was on screen". Fail closed.
    if not samples:
        raise _CoverageError(
            f"no window_geometry samples in span [{start_ts:.3f}, {end_ts:.3f}] "
            f"— coverage unprovable"
        )

    # (b.3) Sparse coverage: any gap > the bounded max is a blind window in
    # which a sensitive surface could have appeared unobserved.
    edges = [start_ts, *samples, end_ts]
    for prev, cur in zip(edges, edges[1:]):
        gap = cur - prev
        if gap > MAX_GEOMETRY_GAP_SECONDS:
            raise _CoverageError(
                f"geometry gap {gap:.3f}s > max {MAX_GEOMETRY_GAP_SECONDS}s in "
                f"span [{start_ts:.3f}, {end_ts:.3f}] — coverage unprovable"
            )

    return samples


def _sensitive_windows_at(
    db_path: Path,
    sample_ts: float,
    classifier: object,
    evaluator: object,
) -> tuple[list[dict], tuple[float, float]]:
    """Return ``(sensitive window dicts, display_origin)`` recorded at ``sample_ts``.

    A window is "sensitive" iff its policy action is in ``VIDEO_BLOCK_ACTIONS``
    (EXCLUDE / MASK_WINDOW) — the same set that drops the frame at capture time
    today, so post-hoc masking covers exactly what capture-blocking would have.
    Returns the raw window dicts (``x``/``y``/``width``/``height``/``bundle_id``)
    plus the snapshot's display origin so the per-frame renderer can convert
    them to pixel rects against that frame's actual dimensions.
    """
    from screencap.privacy.actions import VIDEO_BLOCK_ACTIONS
    from screencap.privacy.context import load_window_geometry
    from screencap.privacy.policy import FrameMetadata

    snap = load_window_geometry(db_path, sample_ts)
    if snap is None or not snap.windows:
        return [], (0.0, 0.0)

    sensitive: list[dict] = []
    for win in snap.windows:
        bundle_id = win.get("bundle_id", "")
        if not bundle_id:
            # A window with no bundle id can't be classified. Treat it as
            # sensitive (over-mask) rather than skip it — unknown provenance
            # is the conservative direction for video.
            sensitive.append(win)
            continue
        meta = FrameMetadata(
            bundle_id=bundle_id,
            window_title=win.get("app_name", ""),
            domain=None,
            timestamp=sample_ts,
        )
        ctx = classifier.classify(meta)
        decision = evaluator.evaluate(ctx, meta)
        if decision.action in VIDEO_BLOCK_ACTIONS:
            sensitive.append(win)
    return sensitive, snap.display_origin


def _build_sensitive_intervals(
    db_path: Path,
    samples: list[float],
    start_ts: float,
    end_ts: float,
    classifier: object,
    evaluator: object,
) -> list[_SensitiveInterval]:
    """Hold-and-pad sensitive rectangles across each inter-sample interval.

    For each sample with sensitive windows, the rectangle is held over
    ``[sample, next_sample)`` (and the last sample's rectangle is held to
    ``end_ts``) so a window that moves between samples is over-masked across
    the whole gap rather than point-interpolated into a partially-uncovered
    swept region (case c conservatism). The first sample's rectangle is also
    held BACK to ``start_ts`` so leading frames before the first sample are
    covered.
    """
    intervals: list[_SensitiveInterval] = []
    for i, ts in enumerate(samples):
        wins, origin = _sensitive_windows_at(db_path, ts, classifier, evaluator)
        if not wins:
            continue
        iv_start = start_ts if i == 0 else ts
        iv_end = samples[i + 1] if i + 1 < len(samples) else end_ts
        # Pad the temporal interval to the previous/next sample boundary so a
        # window seen at one sample stays masked until the NEXT observation
        # contradicts it.
        intervals.append(_SensitiveInterval(
            start=iv_start, end=iv_end, windows=wins, display_origin=origin,
        ))
    return intervals


def _windows_to_rects(
    windows: list[dict],
    img_w: int,
    img_h: int,
    pixel_ratio: float,
    display_origin: tuple[float, float],
) -> list[tuple[int, int, int, int]]:
    """Convert held window dicts to dilated, clamped pixel rects for one frame."""
    from screencap.privacy.masking import _window_to_pixel_rect

    disp_x, disp_y = display_origin
    rects: list[tuple[int, int, int, int]] = []
    for win in windows:
        rect = _window_to_pixel_rect(win, pixel_ratio, disp_x, disp_y, img_w, img_h)
        if rect is None:
            continue
        x1, y1, x2, y2 = rect
        # Union-dilate: grow each rect by MASK_DILATE_PX, clamped to the frame.
        x1 = max(0, x1 - MASK_DILATE_PX)
        y1 = max(0, y1 - MASK_DILATE_PX)
        x2 = min(img_w, x2 + MASK_DILATE_PX)
        y2 = min(img_h, y2 + MASK_DILATE_PX)
        if x2 > x1 and y2 > y1:
            rects.append((x1, y1, x2, y2))
    return rects


def _interval_for(
    intervals: list[_SensitiveInterval], abs_ts: float
) -> _SensitiveInterval | None:
    """Return the sensitive interval covering ``abs_ts``.

    Intervals are half-open ``[start, end)`` so an interior boundary frame is
    attributed to exactly one interval (the one it starts). The single
    exception is the inclusive RIGHT edge: a frame landing exactly on an
    interval's ``end`` (e.g. the final frame at ``end_ts``, or a frame at a
    sample boundary that is not itself the start of another sensitive interval)
    is covered by that interval — over-masking the boundary is the safe error
    direction.
    """
    for iv in intervals:
        if iv.start <= abs_ts < iv.end:
            return iv
    # Inclusive right edge: a frame exactly at some interval's end that no other
    # interval starts. Pick the interval that ends there (over-mask the edge).
    for iv in intervals:
        if abs_ts == iv.end:
            return iv
    return None


def mask_video_chunk(
    chunk_path: Path,
    db_path: Path,
    *,
    start_ts: float,
    end_ts: float,
    chunk_start_abs: float,
    output_path: Path,
    pixel_ratio: float = 2.0,
    classifier: object | None = None,
    evaluator: object | None = None,
) -> MaskOutcome:
    """Mask one cloud-bound video chunk, fail-closed (the U6 public API).

    Decodes ``chunk_path``, applies the three-way coverage gate over the
    chunk's absolute frame span ``[start_ts, end_ts]``, and — only when the
    gate permits — writes a complete copy to ``output_path`` ATOMICALLY
    (temp + ``os.replace``). The output_path should live OUTSIDE the source
    recording dir's upload enumeration (e.g. the ``<name>-scrubbed`` sibling)
    so a partial/abandoned copy can never be picked up by ``upload``.

    Args:
        chunk_path: The rich local chunk mp4 (the source of truth; untouched).
        db_path: The local-only ``recording.db`` holding the geometry timeline.
        start_ts: Inclusive absolute (Unix) start of the chunk's frame span.
        end_ts: Inclusive absolute end of the chunk's frame span.
        chunk_start_abs: Absolute Unix time of the chunk's first frame (PTS 0).
            Per-frame absolute ts = ``chunk_start_abs + frame_pts_seconds``.
        output_path: Where to materialize the masked / faithful copy.
        pixel_ratio: Retina scaling factor for geometry → pixel conversion.
        classifier / evaluator: Privacy policy objects. Built from the default
            config when omitted. A construction failure → FAILED (fail-closed):
            we never ship a clear video because the classifier wouldn't load.

    Returns:
        A :class:`MaskOutcome`. FAILED leaves no copy on disk.
    """
    # ---- (b) FAIL CLOSED FIRST: prove coverage before any masking work. ----
    try:
        samples = _build_coverage(db_path, start_ts, end_ts)
    except _CoverageError as exc:
        logger.warning("video_mask: chunk {} FAILED (case b): {}", chunk_path.name, exc)
        return MaskOutcome(
            status=MaskOutcomeStatus.FAILED, reason=f"coverage_unprovable: {exc}"
        )
    except Exception as exc:  # defensive: any read error is unprovable → closed
        logger.warning(
            "video_mask: chunk {} FAILED (coverage read error): {}",
            chunk_path.name, exc,
        )
        return MaskOutcome(
            status=MaskOutcomeStatus.FAILED, reason=f"coverage_read_error: {exc}"
        )

    # Build the policy objects. A load failure here is the classic fail-closed
    # path (fast-gliner/ONNX is fragile): never pass a clear video through
    # because the classifier couldn't load.
    if classifier is None or evaluator is None:
        try:
            from screencap.config import get_privacy_config
            from screencap.privacy.context import DefaultContextClassifier
            from screencap.privacy.policy import DefaultPolicyEvaluator

            privacy_config = get_privacy_config()
            if evaluator is None:
                evaluator = DefaultPolicyEvaluator(privacy_config)
            if classifier is None:
                classifier = DefaultContextClassifier(
                    app_classes=privacy_config.app_classes,
                )
        except Exception as exc:
            logger.warning(
                "video_mask: chunk {} FAILED (classifier/evaluator unavailable): {}",
                chunk_path.name, exc,
            )
            return MaskOutcome(
                status=MaskOutcomeStatus.FAILED,
                reason=f"classifier_unavailable: {exc}",
            )

    # Determine the sensitive intervals over the (proven-covered) span. An empty
    # list here means case (a): coverage is dense AND no sample denoted a
    # sensitive window → a faithful unmasked copy is valid.
    try:
        intervals = _build_sensitive_intervals(
            db_path, samples, start_ts, end_ts, classifier, evaluator,
        )
    except Exception as exc:
        logger.warning(
            "video_mask: chunk {} FAILED (sensitivity classification error): {}",
            chunk_path.name, exc,
        )
        return MaskOutcome(
            status=MaskOutcomeStatus.FAILED,
            reason=f"sensitivity_error: {exc}",
        )

    case_a = not intervals
    try:
        regions, frames = _transcode_with_masks(
            chunk_path, output_path, intervals, chunk_start_abs, pixel_ratio,
        )
    except Exception as exc:
        logger.warning(
            "video_mask: chunk {} FAILED (decode/re-encode error): {}",
            chunk_path.name, exc,
        )
        return MaskOutcome(
            status=MaskOutcomeStatus.FAILED, reason=f"transcode_error: {exc}"
        )

    if case_a:
        logger.info(
            "video_mask: chunk {} UNMASKED_PROVABLY_SAFE — geometry proves no "
            "sensitive window across [{:.3f}, {:.3f}] ({} frames)",
            chunk_path.name, start_ts, end_ts, frames,
        )
        return MaskOutcome(
            status=MaskOutcomeStatus.UNMASKED_PROVABLY_SAFE,
            output_path=output_path,
            regions_masked=0,
            frames_total=frames,
            reason="geometry proves no sensitive window",
        )

    # Case (c): sensitive windows were present. The positive assertion —
    # regions_masked > 0 — is NECESSARY (it would be a silent no-op otherwise)
    # but NOT SUFFICIENT (it cannot prove per-frame coverage; the gate above
    # did that). If we expected to mask but masked nothing, fail closed: this is
    # the misconfigured-policy / silent-no-op guard.
    if regions == 0:
        logger.warning(
            "video_mask: chunk {} FAILED — sensitive windows expected but 0 "
            "regions masked (silent no-op guard)",
            chunk_path.name,
        )
        # Drop the (clear) copy we just wrote — it must never be uploadable.
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        return MaskOutcome(
            status=MaskOutcomeStatus.FAILED,
            reason="expected_sensitive_but_masked_nothing",
            frames_total=frames,
        )

    logger.info(
        "video_mask: chunk {} MASKED — {} regions over {} frames",
        chunk_path.name, regions, frames,
    )
    return MaskOutcome(
        status=MaskOutcomeStatus.MASKED,
        output_path=output_path,
        regions_masked=regions,
        frames_total=frames,
        reason=f"masked {len(intervals)} sensitive interval(s)",
    )


def _transcode_with_masks(
    chunk_path: Path,
    output_path: Path,
    intervals: list[_SensitiveInterval],
    chunk_start_abs: float,
    pixel_ratio: float,
) -> tuple[int, int]:
    """Decode → draw opaque masks per frame → re-encode atomically.

    Returns ``(regions_masked, frames_total)``. Writes to a dot-prefixed temp
    sibling of ``output_path`` and ``os.replace``s onto it ONLY after the
    container closes cleanly (whole-chunk gate): a crash mid-mask leaves the
    temp (swept by the next run) and NO complete copy at ``output_path``, so a
    partial is never uploadable and a re-run re-detects incompleteness.

    Raises on any decode/encode/close error — the caller maps that to FAILED.
    """
    from PIL import ImageDraw

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Dot-prefixed, pid+uuid temp: never enumerated by upload (dotfile filter)
    # and unique per call so concurrent writers don't clobber. Sweep reclaims
    # orphans from a crashed prior run (mirrors engine/video atomic-write).
    _sweep_stale_temps(output_path.parent, f".{output_path.name}.*.tmp")
    tmp_path = output_path.parent / f".{output_path.name}.{os.getpid()}.{uuid4().hex}.tmp"

    try:
        inp = av.open(str(chunk_path))
    except Exception as exc:
        raise RuntimeError(f"cannot open chunk {chunk_path.name}: {exc}") from exc

    out = None
    regions_masked = 0
    frames_total = 0
    try:
        if not inp.streams.video:
            raise RuntimeError(f"chunk {chunk_path.name} has no video stream")
        in_stream = inp.streams.video[0]
        time_base = in_stream.time_base

        out = av.open(str(tmp_path), mode="w", format="mp4")
        out_stream = out.add_stream(_MASK_CODEC)
        out_stream.width = in_stream.width
        out_stream.height = in_stream.height
        out_stream.pix_fmt = _MASK_PIX_FMT
        # Preserve the source timeline so the masked copy maps frame-for-frame
        # onto the original (recordings are action-gated VFR — never collapse to
        # average_rate; see remediate_pixfmt_for_review).
        out_stream.codec_context.time_base = time_base
        from screencap.engine.config import config as _eng_config

        out_stream.options = {
            "crf": str(_eng_config.VIDEO_CRF),
            "preset": _eng_config.VIDEO_PRESET,
            "g": str(_eng_config.VIDEO_GOP_SIZE),
            "bf": "0",
        }

        for frame in inp.decode(in_stream):
            frames_total += 1
            # Map this frame to absolute recording time to look up its interval.
            frame_pts = frame.pts if frame.pts is not None else 0
            frame_secs = float(frame_pts * time_base) if time_base else 0.0
            abs_ts = chunk_start_abs + frame_secs

            iv = _interval_for(intervals, abs_ts)
            img = frame.to_image().convert("RGB")
            if iv is not None and iv.windows:
                # Use the interval's recorded display origin so global-coordinate
                # window bounds map into the frame exactly as mask_screenshots
                # maps them into a screenshot.
                rects = _windows_to_rects(
                    iv.windows, img.width, img.height, pixel_ratio,
                    display_origin=iv.display_origin,
                )
                if rects:
                    draw = ImageDraw.Draw(img)
                    for (x1, y1, x2, y2) in rects:
                        draw.rectangle([x1, y1, x2, y2], fill=_MASK_FILL)
                        regions_masked += 1

            out_frame = av.VideoFrame.from_image(img)
            out_frame.pts = frame_pts
            out_frame.time_base = time_base
            for packet in out_stream.encode(out_frame):
                out.mux(packet)
            img.close()

        for packet in out_stream.encode():  # flush
            out.mux(packet)

        # Whole-chunk completion gate: if the close times out, the moov atom may
        # be unwritten — the temp could be truncated/unplayable. Treat it as a
        # failed finalize: drop the temp, never promote, never mark complete.
        closed = _close_container_in_thread(out)
        out = None
        if not closed:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"timed out finalizing masked chunk {chunk_path.name}")

        # Atomic promote — the only point at which a COMPLETE copy appears.
        os.replace(tmp_path, output_path)
    except BaseException:
        if out is not None:
            try:
                _close_container_in_thread(out)
            except Exception:
                pass
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        try:
            inp.close()
        except Exception:
            pass

    return regions_masked, frames_total
