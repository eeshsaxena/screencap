"""Privacy-parity guard for clip export (U7).

Locks the clip privacy invariant with a guard CI actually runs. Clip export
inherits the recording's capture-time video-privacy posture and MUST NOT invent
its own: it reads the SOURCE ``chunk_*.mp4`` (capture-blocked in the default
flag-OFF posture), never the post-hoc ``masked_video/`` copies, and **fails
closed** with :class:`MaskedVideoRequiredError` when the recording's frozen
``.recording_intent`` ``masked_video_upload`` bit is ON — so it can never export
rich, unmasked sensitive-window video to an external-recipient file (KD3, KD4,
KTD3; AE2, AE3).

CI runs ONLY ``pytest -m privacy`` (two lanes, including a Vision-free Ubuntu
lane — see
``docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md``).
Every test here is therefore ``@pytest.mark.privacy`` (module-level ``pytestmark``)
AND stays Vision-free: the exercised path (``screencap.engine.video.export_clip``
→ ``audio_clip.mux_clip_audio``) imports no Apple Vision / OCR surface, and these
fixtures use only synthetic solid-color frames, so no OCR is ever reachable and
no stub is needed.

Fixture shape mirrors ``tests/test_video_clip.py``: tiny multi-chunk recordings
whose per-chunk manifests + ``recording.db`` anchor let the trim place each chunk
at its absolute recording offset, so a frame written at input timestamp
``base + dt`` sits at absolute recording time ``dt`` (seconds); clip range
``[start_ms, end_ms)`` selects frames whose ``dt`` lies in
``[start_ms/1000, end_ms/1000)``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import av
import pytest
from PIL import Image

from screencap.catalog import read_masked_video_upload
from screencap.engine.video import (
    MaskedVideoRequiredError,
    VideoWriter,
    export_clip,
    export_clip_video,
)
from screencap.pipeline_chunk_ops import get_frozen_masked_video_upload

pytestmark = pytest.mark.privacy

_BASE = 1_000_000.0  # arbitrary recording-start wall-clock anchor (seconds)

_RED = (220, 0, 0)
_BLUE = (0, 0, 220)
_GREEN = (0, 220, 0)  # decoy: only ever written into a masked_video/ copy


def _make_recording(
    rec_dir: Path,
    chunks: list[list[tuple[float, tuple[int, int, int]]]],
    *,
    base: float = _BASE,
    size: int = 48,
    fps: int = 24,
) -> None:
    """Write a synthetic multi-chunk source recording under ``rec_dir``.

    ``chunks`` is one list per ``chunk_NNNN.mp4``; each inner entry is
    ``(dt_seconds, (r, g, b))`` — a frame written at input timestamp ``base + dt``
    with that solid color. Each chunk's manifest records
    ``chunk_start = base + (first dt)`` and ``recording.db`` records
    ``video_start_time = base``, so the trim maps a frame's recording time back to
    its ``dt``. Mirrors ``tests/test_video_clip.py``.
    """
    for i, frames in enumerate(chunks):
        path = rec_dir / f"chunk_{i:04d}.mp4"
        writer = VideoWriter(str(path), width=size, height=size, fps=fps)
        for dt, color in frames:
            writer.write_frame(Image.new("RGB", (size, size), color=color), base + dt)
        writer.close()
        (rec_dir / f"chunk_{i:04d}_manifest.json").write_text(
            json.dumps(
                {"chunk_start": base + frames[0][0], "chunk_end": base + frames[-1][0]}
            )
        )
    conn = sqlite3.connect(str(rec_dir / "recording.db"))
    conn.execute("CREATE TABLE recording (video_start_time REAL, timestamp REAL)")
    conn.execute("INSERT INTO recording VALUES (?, ?)", (base, base))
    conn.commit()
    conn.close()


def _write_intent(rec_dir: Path, *, masked_video_upload: bool) -> None:
    (rec_dir / ".recording_intent").write_text(
        json.dumps({"masked_video_upload": masked_video_upload})
    )


def _decode(path: Path) -> list[tuple[float, "Image.Image"]]:
    container = av.open(str(path))
    try:
        return [(float(f.time), f.to_image()) for f in container.decode(video=0)]
    finally:
        container.close()


def _dominant(img: "Image.Image") -> str:
    """'R' / 'G' / 'B' for the largest channel at the image center."""
    r, g, b = img.convert("RGB").getpixel((img.width // 2, img.height // 2))[:3]
    return "RGB"[max(range(3), key=[r, g, b].__getitem__)]


# Two sparse chunks with a ~2.5s idle gap between them (action-gated VFR).
# Absolute frame times: chunk0 -> {0.0, 0.5} red, chunk1 -> {3.0, 3.5} blue.
_SPARSE_TWO_CHUNK = [
    [(0.0, _RED), (0.5, _RED)],
    [(3.0, _BLUE), (3.5, _BLUE)],
]


class _AvOpenRecorder:
    """Records every path handed to ``av.open`` and its mode.

    ``export_clip`` reaches ``av.open`` in both ``engine.video`` (source-chunk
    reads + the output writer) and ``engine.audio_clip`` (only when audio exists);
    both reference the shared ``av.open`` attribute, so patching it once captures
    the whole media-open surface. ``reads`` filters to the input opens (mode
    ``"r"``), which is exactly the set of media the clip pulls FROM.
    """

    def __init__(self) -> None:
        self._real = av.open
        self.opened: list[tuple[Path, str]] = []

    def __call__(self, file, mode="r", *args, **kwargs):
        self.opened.append((Path(str(file)), mode))
        return self._real(file, mode, *args, **kwargs)

    @property
    def reads(self) -> list[Path]:
        return [p for p, mode in self.opened if mode == "r"]


class TestFailClosedFlagOn:
    """AE3 / KTD3: frozen ``masked_video_upload`` ON → fail closed, no file."""

    def test_export_clip_fails_closed_and_writes_no_file(self, tmp_path, monkeypatch):
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        _write_intent(tmp_path, masked_video_upload=True)
        out = tmp_path / "clip.mp4"

        rec = _AvOpenRecorder()
        monkeypatch.setattr(av, "open", rec)

        with pytest.raises(MaskedVideoRequiredError) as exc:
            export_clip(tmp_path, 0, 4000, out)

        # Structured, typed reason the CLI maps to `masked_video_required`.
        assert exc.value.reason == "masked_video_required"
        # Fail CLOSED: nothing is written when refusing.
        assert not out.exists(), "no file may be written on a flag-ON recording"
        # Refuses BEFORE reading a single source chunk — the rich pixels are
        # never even opened, let alone re-encoded into a deliverable.
        assert rec.reads == [], f"read source media while failing closed: {rec.reads}"

    def test_export_clip_video_also_fails_closed(self, tmp_path):
        """The video-only entry point enforces the same gate (parity)."""
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        _write_intent(tmp_path, masked_video_upload=True)
        out = tmp_path / "clip.mp4"

        with pytest.raises(MaskedVideoRequiredError):
            export_clip_video(tmp_path, 0, 4000, out)
        assert not out.exists()


class TestFailClosedUnreadableIntent:
    """Fix 2 (fail-closed on unreadable intent): the CLIP gate is STRICTER than the
    shared upload resolver. It reads the frozen ``masked_video_upload`` bit
    DIRECTLY, so a missing / corrupt / field-absent ``.recording_intent`` REFUSES
    rather than falling back to the mutable global (which
    ``get_frozen_masked_video_upload`` does for the upload seams). A clip exports
    rich source video to an EXTERNAL-recipient file, so an unreadable frozen
    posture must never be resolved as "safe" by a possibly-relaxed live flag.
    """

    def test_missing_intent_fails_closed(self, tmp_path):
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)  # NO _write_intent
        assert not (tmp_path / ".recording_intent").exists()
        out = tmp_path / "clip.mp4"

        with pytest.raises(MaskedVideoRequiredError) as exc:
            export_clip(tmp_path, 0, 4000, out)

        assert exc.value.reason == "masked_video_required"
        assert not out.exists(), "no file may be written on an unreadable intent"

    def test_corrupt_intent_fails_closed(self, tmp_path):
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        (tmp_path / ".recording_intent").write_text("{ this is not valid json")
        out = tmp_path / "clip.mp4"

        with pytest.raises(MaskedVideoRequiredError):
            export_clip(tmp_path, 0, 4000, out)
        assert not out.exists()

    def test_field_absent_intent_fails_closed(self, tmp_path):
        """A valid intent that never froze the ``masked_video_upload`` field is
        AMBIGUOUS for a clip → fail closed, don't consult the global."""
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        (tmp_path / ".recording_intent").write_text(
            json.dumps({"destination": "local"})
        )
        out = tmp_path / "clip.mp4"

        with pytest.raises(MaskedVideoRequiredError):
            export_clip(tmp_path, 0, 4000, out)
        assert not out.exists()

    def test_global_off_does_not_rescue_a_missing_intent(self, tmp_path, monkeypatch):
        """Regression discriminator: with the LIVE global explicitly OFF (the pre-Fix
        fallback would have made a missing intent EXPORT), the clip gate STILL
        refuses — proving the global is never consulted on the clip path."""
        import screencap.config as _config

        monkeypatch.setattr(_config, "get_masked_video_upload_enabled", lambda: False)
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)  # no intent
        out = tmp_path / "clip.mp4"

        with pytest.raises(MaskedVideoRequiredError):
            export_clip(tmp_path, 0, 4000, out)
        assert not out.exists()

    def test_video_only_entry_point_also_fails_closed(self, tmp_path):
        """Parity: the video-only entry enforces the same unreadable-intent gate."""
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)  # no intent
        out = tmp_path / "clip.mp4"

        with pytest.raises(MaskedVideoRequiredError):
            export_clip_video(tmp_path, 0, 4000, out)
        assert not out.exists()


class TestFlagOffControl:
    """The gate is the FLAG, not something incidental to the fixture."""

    def test_same_recording_exports_with_flag_off(self, tmp_path):
        """Identical recording shape, flag OFF → export succeeds.

        Paired with :class:`TestFailClosedFlagOn`: the ONLY difference between the
        refused and the exported case is the frozen ``masked_video_upload`` bit,
        so the refusal is attributable to the flag alone.
        """
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        _write_intent(tmp_path, masked_video_upload=False)
        out = tmp_path / "clip.mp4"

        export_clip(tmp_path, 0, 4000, out)

        assert out.is_file()
        # A flag-OFF export never flips the recording's frozen posture ON.
        assert read_masked_video_upload(tmp_path) is False
        assert get_frozen_masked_video_upload(tmp_path) is False


class TestSourceOnlyNeverMaskedVideo:
    """The clip reads SOURCE chunks only — never a ``masked_video/`` copy.

    ``scrubber.masked_video_dir`` is ``<scrubbed>/masked_video``; the clip must
    never read from any such directory. We plant a decoy ``masked_video/`` copy
    with visibly different content and prove it is neither opened nor delivered.
    """

    def test_masked_video_copy_is_never_opened_or_delivered(
        self, tmp_path, monkeypatch
    ):
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)  # source: red + blue
        _write_intent(tmp_path, masked_video_upload=False)

        # Plant a decoy post-hoc masked copy (all GREEN). If the clip pipeline
        # ever preferred masked_video/, the decoy would be opened and its green
        # frames would surface in the output.
        masked_dir = tmp_path / "masked_video"
        masked_dir.mkdir()
        _make_recording(masked_dir, [[(0.0, _GREEN), (0.5, _GREEN)]])

        out = tmp_path / "clip.mp4"
        rec = _AvOpenRecorder()
        monkeypatch.setattr(av, "open", rec)

        export_clip(tmp_path, 0, 4000, out)

        assert out.is_file()
        # No input media is opened from under a `masked_video` directory...
        for p in rec.reads:
            assert "masked_video" not in p.parts, f"read a masked_video path: {p}"
        # ...and every input read is a top-level source `chunk_*.mp4` in rec_dir.
        for p in rec.reads:
            assert p.parent == tmp_path, f"read outside the source dir: {p}"
            assert p.name.startswith("chunk_") and p.suffix == ".mp4", p
        # Behavioral proof: the decoy's color never reaches the deliverable.
        colors = {_dominant(img) for _, img in _decode(out)}
        assert colors <= {"R", "B"}, f"decoy leaked into the clip: {colors}"
        assert "G" not in colors
        # The clip pipeline never enabled masked_video_upload as a side effect.
        assert read_masked_video_upload(tmp_path) is False


class TestCaptureBlockedIntervalAbsent:
    """AE2 (structural): a capture-blocked interval is absent from a flag-OFF clip.

    LIMITATION (Vision-free): a *faithful* capture-time-blocked fixture would
    require the recorder's ``RecorderPrivacyFilter`` (``screencap.enforcement``)
    dropping sensitive windows at capture time — out of reach here and adjacent to
    the Vision surface CI's privacy lane forbids. So we assert the weaker but
    load-bearing structural invariant: capture-time blocking means the blocked
    frames were NEVER written to the source chunks, so a clip — which only pulls
    source frames and ADDS nothing — cannot contain them. The blocked window is
    modeled as a gap with no source frames; the clip must hold an ALLOWED frame
    across it (a freeze), never conjure blocked content. The true capture-blocking
    guarantee is exercised in the ``screencap.enforcement`` package tests.
    """

    def test_blocked_gap_holds_allowed_frames_only(self, tmp_path):
        # Allowed red at [0.0, 0.5]; a modeled blocked window [1.0, 3.0) with NO
        # source frames; allowed blue at [3.0, 3.5]. One chunk, so any content in
        # the gap can only be a freeze-hold of a neighbouring ALLOWED frame.
        _make_recording(
            tmp_path,
            [[(0.0, _RED), (0.5, _RED), (3.0, _BLUE), (3.5, _BLUE)]],
        )
        _write_intent(tmp_path, masked_video_upload=False)
        out = tmp_path / "clip.mp4"

        export_clip_video(tmp_path, 0, 4000, out)

        frames = _decode(out)
        colors = {_dominant(img) for _, img in frames}
        # The clip adds nothing: its colors are a subset of the source's allowed
        # colors — no third (blocked) color can appear because none was written.
        assert colors <= {"R", "B"}, f"unexpected content in clip: {colors}"
        # Inside the modeled blocked window (~2.0s) the frame ON SCREEN is the
        # held ALLOWED red — a freeze, not resurrected blocked content. The frame
        # visible at time t is the last one whose PTS is <= t (its display extends
        # across the gap until the next real frame), NOT the nearest by distance.
        visible_at_2s = max((ft for ft in frames if ft[0] <= 2.0), key=lambda ft: ft[0])
        assert _dominant(visible_at_2s[1]) == "R", (
            f"blocked-window frame at {visible_at_2s[0]:.2f}s is not the held "
            f"allowed frame: {_dominant(visible_at_2s[1])}"
        )
