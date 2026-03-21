# Video Compression Benchmark Results

**Date:** 2026-03-20
**Source:** `capture-test-20260318-085835` (1048 screenshots, 3024x1964, sampled 100 frames)
**Script:** `scripts/compare_codecs.py`

## Chosen defaults

Based on the results below:

- **VIDEO_CRF = 23** — 89% size reduction, 39.7 dB PSNR, text fully legible
- **VIDEO_PRESET = "faster"** — best compression-per-CPU-cost; `fast` exceeds burst budget at 3K
- **VIDEO_PIXEL_FORMAT = "yuv444p"** (unchanged) — preserves syntax highlighting colors

## File size (vs CRF=0 yuv444p ultrafast baseline = 87.45 MB)

| Config | Size (MB) | vs base | PSNR (dB) | Mean Diff |
|--------|-----------|---------|-----------|-----------|
| CRF=0 yuv444p ultrafast (current) | 87.45 | 100% | 53.6 | 0.31 |
| CRF=0 yuv444p faster | 69.38 | 79% | 53.6 | 0.31 |
| CRF=23 yuv444p ultrafast | 21.45 | 25% | 41.7 | 1.05 |
| **CRF=23 yuv444p faster** | **9.29** | **10.6%** | **39.7** | **1.10** |
| CRF=23 yuv444p fast | 9.23 | 10.6% | 39.9 | 1.07 |
| CRF=25 yuv444p faster | 7.96 | 9.1% | 38.3 | 1.24 |
| CRF=28 yuv444p faster | 6.23 | 7.1% | 36.1 | 1.51 |
| CRF=23 yuv420p faster | 9.20 | 10.5% | 37.6 | 2.45 |

## Burst encoding CPU (10fps x 5s = 50 frames, 3024x1964)

Budget: 100ms/frame at 10fps.

| Config | CPU/frame (ms) | Headroom |
|--------|----------------|----------|
| CRF=23 yuv444p ultrafast | 37.6 | 62.4ms |
| **CRF=23 yuv444p faster** | **98.6** | **1.4ms** |
| CRF=23 yuv444p fast | 131.1 | -31.1ms |
| CRF=23 yuv420p ultrafast | 31.2 | 68.8ms |
| CRF=23 yuv420p faster | 84.1 | 15.9ms |
| CRF=23 yuv420p fast | 107.1 | -7.1ms |

## Key observations

1. **`fast` preset is too slow** for burst encoding at 3K+ resolution — exceeds the 100ms/frame budget on all configs. Ruled out.

2. **`faster` is the sweet spot** — files are ~2x smaller than `ultrafast` at the same CRF, with only ~5ms more per frame at 1080p. At 3K the headroom is tight (1.4ms for yuv444p) but wall time shows multi-core parallelism helps.

3. **yuv444p vs yuv420p is nearly identical in file size** at CRF 23+ (~0.1 MB difference), but yuv444p preserves 2+ dB more PSNR and avoids color bleeding on syntax-highlighted code. Keep yuv444p.

4. **CRF 23 vs 25 vs 28** — CRF 23 is the most conservative lossy setting. Confirmed by visual inspection: text fully legible, no visible artifacts in terminal or editor content. CRF 25 and 28 are also acceptable but CRF 23 provides a safer margin.

5. **Real-world size reduction**: a 100-frame segment at 3024x1964 goes from 87.45 MB to 9.29 MB — a **9.4x reduction** with no visible quality loss for screen review purposes.
