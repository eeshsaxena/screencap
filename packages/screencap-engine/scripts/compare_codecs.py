#!/usr/bin/env python3
"""Benchmark H.264 encoding settings for screen capture.

Tests a matrix of encoding parameters to find the best trade-off between
file size, encode speed, CPU overhead, and visual quality for screen content.

Matrix dimensions:
- CRF: 0 (lossless baseline), 23, 25, 28
- Pixel format: yuv444p (full chroma), yuv420p (chroma subsampled)
- Preset: ultrafast, faster, fast

Usage:
    # Synthetic frames (default)
    uv run python scripts/compare_codecs.py

    # Real frames from a recording directory
    uv run python scripts/compare_codecs.py --frames-dir ~/.screencap/recordings/my-session/

    # Quick test with fewer frames
    uv run python scripts/compare_codecs.py -n 30

    # Keep video files for manual inspection
    uv run python scripts/compare_codecs.py --keep-videos -o /tmp/codec_results
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class CodecResult:
    """Results from testing one encoding configuration."""

    crf: int
    pix_fmt: str
    preset: str
    file_size_bytes: int
    total_encode_time_s: float
    decode_time_s: float
    mean_diff: float
    max_diff: float
    psnr: float
    num_frames: int
    per_frame_times_ms: list[float] = field(default_factory=list)
    # Burst stats (filled separately)
    burst_cpu_time_s: float | None = None
    burst_wall_time_s: float | None = None
    burst_frames: int = 0

    @property
    def label(self) -> str:
        return f"CRF={self.crf} {self.pix_fmt} {self.preset}"

    @property
    def size_mb(self) -> float:
        return self.file_size_bytes / (1024 * 1024)

    @property
    def mean_frame_time_ms(self) -> float:
        if not self.per_frame_times_ms:
            return 0.0
        return sum(self.per_frame_times_ms) / len(self.per_frame_times_ms)

    @property
    def p95_frame_time_ms(self) -> float:
        if not self.per_frame_times_ms:
            return 0.0
        return float(np.percentile(self.per_frame_times_ms, 95))


# ── Frame generators ─────────────────────────────────────────────────────────


def generate_synthetic_frames(
    num_frames: int, width: int = 1920, height: int = 1080
) -> list[Image.Image]:
    """Generate synthetic frames simulating screen content.

    Creates frames with text-like patterns, editor windows, terminals,
    and temporal variation (cursor movement, typing simulation).
    """
    print(f"Generating {num_frames} synthetic frames ({width}x{height})...")
    frames = []
    np.random.seed(42)

    # Dark background with gradient
    base = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        base[y, :, 0] = int(30 + 20 * (y / height))
        base[y, :, 1] = int(40 + 30 * (y / height))
        base[y, :, 2] = int(60 + 40 * (y / height))

    # Window regions
    windows = [
        (100, 100, 800, 600, (40, 44, 52)),  # Dark editor
        (850, 150, 1000, 500, (255, 255, 255)),  # Light browser
        (200, 650, 600, 350, (30, 30, 30)),  # Terminal
    ]
    for x, y, w, h, color in windows:
        base[y : y + h, x : x + w] = color

    # Add simulated "code" lines -- colored syntax on dark bg
    for line_idx in range(25):
        ly = 120 + line_idx * 20
        if ly + 12 > 700:
            break
        indent = np.random.randint(0, 6) * 20
        kw_len = np.random.randint(30, 120)
        base[ly : ly + 12, 120 + indent : 120 + indent + kw_len, :] = [
            80 + np.random.randint(0, 100),
            140 + np.random.randint(0, 80),
            200 + np.random.randint(0, 55),
        ]

    for i in range(num_frames):
        frame = base.copy()

        # Cursor movement
        cursor_x = 100 + int(400 * np.sin(i * 0.1))
        cursor_y = 300 + int(200 * np.cos(i * 0.15))
        frame[cursor_y : cursor_y + 20, cursor_x : cursor_x + 15] = (255, 0, 0)

        # Typing simulation in terminal
        text_y = 680 + (i % 10) * 20
        if text_y < 980:
            noise = np.random.randint(150, 255, (15, 300, 3), dtype=np.uint8)
            frame[text_y : text_y + 15, 220:520] = noise

        # Timestamp area
        frame[50:70, 1700:1900] = np.random.randint(
            200, 255, (20, 200, 3), dtype=np.uint8
        )

        frames.append(Image.fromarray(frame))
        if (i + 1) % 50 == 0:
            print(f"  Generated {i + 1}/{num_frames} frames")

    return frames


def load_real_frames(
    frames_dir: Path, max_frames: int | None = None
) -> list[Image.Image]:
    """Load real screenshot frames from a recording directory.

    Looks for PNG/JPEG files in the directory (and screenshots/ subdirectory).
    Returns frames sorted by filename (assumed chronological).
    """
    search_dirs = [frames_dir]
    screenshots_subdir = frames_dir / "screenshots"
    if screenshots_subdir.is_dir():
        search_dirs.append(screenshots_subdir)

    paths: list[Path] = []
    for d in search_dirs:
        paths.extend(sorted(d.glob("*.png")))
        paths.extend(sorted(d.glob("*.jpg")))
        paths.extend(sorted(d.glob("*.jpeg")))

    # Deduplicate and sort
    paths = sorted(set(paths), key=lambda p: p.name)

    if not paths:
        raise FileNotFoundError(f"No PNG/JPEG files found in {frames_dir}")

    if max_frames and len(paths) > max_frames:
        # Sample evenly
        indices = np.linspace(0, len(paths) - 1, max_frames, dtype=int)
        paths = [paths[i] for i in indices]

    print(f"Loading {len(paths)} real frames from {frames_dir}...")
    frames = []
    for p in paths:
        img = Image.open(p).convert("RGB")
        frames.append(img)
        if len(frames) % 20 == 0:
            print(f"  Loaded {len(frames)}/{len(paths)}")

    print(f"  Frame size: {frames[0].size[0]}x{frames[0].size[1]}")
    return frames


# ── Encoding benchmark ───────────────────────────────────────────────────────


def _extract_all_frames(video_path: Path) -> list[Image.Image]:
    """Decode all frames from a video file, avoiding timestamp tolerance issues."""
    import av

    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        return [frame.to_image() for frame in container.decode(stream)]


def benchmark_config(
    frames: list[Image.Image],
    output_dir: Path,
    crf: int,
    pix_fmt: str,
    preset: str,
) -> CodecResult:
    """Benchmark one encoding configuration.

    Returns per-frame timing, file size, decode time, and PSNR.
    """
    from sc_engine.comparison import compute_psnr
    from sc_engine.video import VideoWriter

    label = f"CRF={crf} {pix_fmt} {preset}"
    video_path = output_dir / f"test_crf{crf}_{pix_fmt}_{preset}.mp4"
    width, height = frames[0].size

    print(f"\n  [{label}]")

    # ── Encode with per-frame timing ──
    per_frame_ms: list[float] = []
    encode_start = time.perf_counter()

    writer = VideoWriter(
        video_path,
        width=width,
        height=height,
        fps=24,
        codec="libx264",
        pix_fmt=pix_fmt,
        crf=crf,
        preset=preset,
    )

    for i, frame in enumerate(frames):
        timestamp = i / 24.0
        t0 = time.perf_counter()
        writer.write_frame(frame, timestamp)
        t1 = time.perf_counter()
        per_frame_ms.append((t1 - t0) * 1000)

    writer.close()
    total_encode = time.perf_counter() - encode_start

    file_size = video_path.stat().st_size
    print(f"    Size: {file_size / 1024 / 1024:.2f} MB | Encode: {total_encode:.2f}s")
    print(
        f"    Frame time: mean={sum(per_frame_ms) / len(per_frame_ms):.1f}ms "
        f"p95={float(np.percentile(per_frame_ms, 95)):.1f}ms"
    )

    # ── Decode all frames (avoids tolerance issues with lossy configs) ──
    decode_start = time.perf_counter()
    extracted = _extract_all_frames(video_path)
    decode_time = time.perf_counter() - decode_start
    # Match count: min of source frames and extracted frames
    num_compare = min(len(frames), len(extracted))
    frames_cmp = frames[:num_compare]
    extracted = extracted[:num_compare]

    # ── Quality metrics ──
    psnrs = []
    diffs = []
    max_diffs = []
    for orig, extr in zip(frames_cmp, extracted):
        if orig.size != extr.size:
            extr = extr.resize(orig.size, Image.Resampling.LANCZOS)
        orig_arr = np.array(orig.convert("RGB"), dtype=np.float64)
        extr_arr = np.array(extr.convert("RGB"), dtype=np.float64)
        diff = np.abs(orig_arr - extr_arr)
        diffs.append(np.mean(diff))
        max_diffs.append(np.max(diff))
        psnrs.append(
            compute_psnr(
                np.array(orig.convert("RGB")), np.array(extr.convert("RGB"))
            )
        )

    finite_psnrs = [p for p in psnrs if p != float("inf")]
    mean_psnr = float(np.mean(finite_psnrs)) if finite_psnrs else float("inf")
    print(f"    PSNR: {mean_psnr:.1f} dB | Mean diff: {np.mean(diffs):.2f}")

    return CodecResult(
        crf=crf,
        pix_fmt=pix_fmt,
        preset=preset,
        file_size_bytes=file_size,
        total_encode_time_s=total_encode,
        decode_time_s=decode_time,
        mean_diff=float(np.mean(diffs)),
        max_diff=float(np.max(max_diffs)),
        psnr=mean_psnr,
        num_frames=len(frames_cmp),
        per_frame_times_ms=per_frame_ms,
    )


# ── Burst CPU benchmark ──────────────────────────────────────────────────────


def burst_benchmark(
    frames: list[Image.Image],
    output_dir: Path,
    crf: int,
    pix_fmt: str,
    preset: str,
    fps: int = 10,
    duration_s: int = 5,
) -> tuple[float, float, int]:
    """Simulate burst encoding at a given fps for duration_s.

    Returns (cpu_time_s, wall_time_s, num_frames).
    Uses time.process_time() for CPU time measurement.
    """
    from sc_engine.video import VideoWriter

    num_burst = fps * duration_s
    # Cycle through available frames
    burst_frames = [frames[i % len(frames)] for i in range(num_burst)]
    width, height = burst_frames[0].size

    video_path = output_dir / f"burst_crf{crf}_{pix_fmt}_{preset}.mp4"

    writer = VideoWriter(
        video_path,
        width=width,
        height=height,
        fps=fps,
        codec="libx264",
        pix_fmt=pix_fmt,
        crf=crf,
        preset=preset,
    )

    interval = 1.0 / fps
    cpu_start = time.process_time()
    wall_start = time.perf_counter()

    for i, frame in enumerate(burst_frames):
        timestamp = i * interval
        writer.write_frame(frame, timestamp)

    writer.close()
    cpu_time = time.process_time() - cpu_start
    wall_time = time.perf_counter() - wall_start

    # Clean up burst file
    video_path.unlink(missing_ok=True)

    return cpu_time, wall_time, num_burst


# ── Output ────────────────────────────────────────────────────────────────────


def print_results_table(
    results: list[CodecResult], baseline: CodecResult | None
) -> None:
    """Print formatted comparison table."""
    print("\n" + "=" * 110)
    print("ENCODING MATRIX RESULTS")
    print("=" * 110)

    header = (
        f"{'Config':<30} {'Size (MB)':>10} {'vs base':>8} "
        f"{'Encode(s)':>10} {'Frame(ms)':>10} {'p95(ms)':>8} "
        f"{'PSNR(dB)':>9} {'MeanDiff':>9}"
    )
    print(header)
    print("-" * 110)

    for r in results:
        if baseline and baseline.file_size_bytes > 0:
            ratio = f"{r.file_size_bytes / baseline.file_size_bytes:.1%}"
        else:
            ratio = "-"

        line = (
            f"{r.label:<30} {r.size_mb:>10.2f} {ratio:>8} "
            f"{r.total_encode_time_s:>10.2f} {r.mean_frame_time_ms:>10.1f} "
            f"{r.p95_frame_time_ms:>8.1f} "
            f"{r.psnr:>9.1f} {r.mean_diff:>9.2f}"
        )
        print(line)

    print("-" * 110)

    # Burst table
    burst_results = [r for r in results if r.burst_cpu_time_s is not None]
    if burst_results:
        print("\nBURST ENCODING (10fps x 5s = 50 frames)")
        print("-" * 80)
        print(
            f"{'Config':<30} {'CPU(s)':>8} {'Wall(s)':>8} "
            f"{'CPU/frame(ms)':>14} {'Headroom':>10}"
        )
        print("-" * 80)
        for r in burst_results:
            cpu_per_frame = (
                (r.burst_cpu_time_s / r.burst_frames * 1000)
                if r.burst_frames
                else 0
            )
            # At 10fps, budget is 100ms per frame
            headroom = 100.0 - cpu_per_frame
            print(
                f"{r.label:<30} {r.burst_cpu_time_s:>8.2f} "
                f"{r.burst_wall_time_s:>8.2f} "
                f"{cpu_per_frame:>14.1f} {headroom:>9.1f}ms"
            )
        print("-" * 80)


def save_side_by_side(
    frames: list[Image.Image],
    results: list[CodecResult],
    output_dir: Path,
) -> Path | None:
    """Generate side-by-side visual comparison of a cropped text region.

    Picks a region from the first frame that likely contains text/code content,
    encodes it with each config, extracts the frame back, and saves a comparison
    image with labels.
    """
    if not frames:
        return None

    src = frames[0]
    w, h = src.size

    # Crop a region likely to contain text (code editor area)
    crop_x = min(100, w - 400)
    crop_y = min(100, h - 200)
    crop_box = (crop_x, crop_y, min(crop_x + 400, w), min(crop_y + 200, h))
    original_crop = src.crop(crop_box)

    crops: list[tuple[str, Image.Image]] = [("Original", original_crop)]

    for r in results:
        video_path = output_dir / f"test_crf{r.crf}_{r.pix_fmt}_{r.preset}.mp4"
        if not video_path.exists():
            continue
        try:
            extracted = _extract_all_frames(video_path)
            if extracted:
                crop = extracted[0].crop(crop_box)
                crops.append((r.label, crop))
        except Exception as exc:
            print(f"  Warning: could not extract frame for {r.label}: {exc}")
            continue

    if len(crops) < 2:
        return None

    # Build grid image
    crop_w, crop_h = original_crop.size
    label_height = 25
    tile_h = crop_h + label_height
    cols = min(len(crops), 4)
    rows = (len(crops) + cols - 1) // cols
    canvas_w = cols * crop_w
    canvas_h = rows * tile_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(canvas)

    for idx, (label, crop_img) in enumerate(crops):
        col = idx % cols
        row = idx // cols
        x_off = col * crop_w
        y_off = row * tile_h
        draw.text((x_off + 4, y_off + 4), label, fill=(0, 0, 0))
        canvas.paste(crop_img, (x_off, y_off + label_height))

    output_path = output_dir / "side_by_side.png"
    canvas.save(output_path)
    print(f"\nSide-by-side comparison saved: {output_path}")
    return output_path


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark H.264 encoding settings for screen capture"
    )
    parser.add_argument(
        "--num-frames",
        "-n",
        type=int,
        default=100,
        help="Number of frames to encode (default: 100)",
    )
    parser.add_argument(
        "--frames-dir",
        type=str,
        default=None,
        help="Directory with real PNG/JPEG screenshots instead of synthetic frames",
    )
    parser.add_argument(
        "--crf",
        type=str,
        default="0,23,25,28",
        help="Comma-separated CRF values to test (default: 0,23,25,28)",
    )
    parser.add_argument(
        "--pix-fmt",
        type=str,
        default="yuv444p,yuv420p",
        help="Comma-separated pixel formats (default: yuv444p,yuv420p)",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default="ultrafast,faster,fast",
        help="Comma-separated presets (default: ultrafast,faster,fast)",
    )
    parser.add_argument(
        "--skip-burst",
        action="store_true",
        help="Skip burst encoding benchmark",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=None,
        help="Output directory for results (default: temp dir)",
    )
    parser.add_argument(
        "--keep-videos",
        action="store_true",
        help="Keep generated video files",
    )
    args = parser.parse_args()

    crfs = [int(c) for c in args.crf.split(",")]
    pix_fmts = [p.strip() for p in args.pix_fmt.split(",")]
    presets = [p.strip() for p in args.preset.split(",")]

    total_configs = len(crfs) * len(pix_fmts) * len(presets)

    # Output directory
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        output_dir = Path(tempfile.mkdtemp(prefix="codec_benchmark_"))
        cleanup = not args.keep_videos

    print("=" * 110)
    print("H.264 Encoding Settings Benchmark")
    print("=" * 110)
    print(
        f"Matrix: {len(crfs)} CRFs x {len(pix_fmts)} pix_fmts "
        f"x {len(presets)} presets = {total_configs} configs"
    )
    print(f"Output: {output_dir}")

    try:
        # ── Load frames ──
        if args.frames_dir:
            frames = load_real_frames(
                Path(args.frames_dir), max_frames=args.num_frames
            )
        else:
            frames = generate_synthetic_frames(args.num_frames)

        # ── Run matrix ──
        results: list[CodecResult] = []
        baseline: CodecResult | None = None

        for crf in crfs:
            for pix_fmt in pix_fmts:
                for preset in presets:
                    result = benchmark_config(
                        frames, output_dir, crf, pix_fmt, preset
                    )
                    results.append(result)
                    # First result is the baseline
                    if baseline is None:
                        baseline = result

        # ── Burst benchmark ──
        if not args.skip_burst:
            print("\n" + "=" * 80)
            print("BURST ENCODING BENCHMARK (10fps x 5s)")
            print("=" * 80)
            for r in results:
                cpu_t, wall_t, n = burst_benchmark(
                    frames, output_dir, r.crf, r.pix_fmt, r.preset
                )
                r.burst_cpu_time_s = cpu_t
                r.burst_wall_time_s = wall_t
                r.burst_frames = n
                cpu_per_frame = cpu_t / n * 1000
                print(
                    f"  [{r.label}] CPU: {cpu_t:.2f}s, "
                    f"Wall: {wall_t:.2f}s, {cpu_per_frame:.1f}ms/frame"
                )

        # ── Print results ──
        print_results_table(results, baseline)

        # ── Side-by-side visual ──
        save_side_by_side(frames, results, output_dir)

    finally:
        if cleanup:
            print(f"\nCleaning up {output_dir}...")
            shutil.rmtree(output_dir, ignore_errors=True)
        else:
            print(f"\nResults saved in: {output_dir}")


if __name__ == "__main__":
    main()
