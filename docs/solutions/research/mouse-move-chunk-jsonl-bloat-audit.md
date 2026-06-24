---
title: "Keeping mouse.move in chunk JSONL by default does not bloat chunks — KEEP-MOVES"
date: 2026-06-24
problem_type: evaluation
component: screencap.pipeline
module: pipeline
platform: macos
decision: KEEP-MOVES (keep mouse.move events in chunk JSONL by default)
tags:
  - chunk-processing
  - mouse-move
  - perf-audit
  - event-export
  - scrubber
references:
  - "Unit 9 of the (now-removed) unified-export-callable refactor — R11/R12 keep-mouse.move default"
  - "Distilled from benchmarks/benchmark_mouse_move_bloat.py + benchmarks/2026-04-27-mouse-move-bloat-synthetic.json (removed in SCR-119)"
  - "docs/plans/2026-06-24-001-refactor-scr-119-distill-benchmark-harnesses-plan.md"
---

# Keeping `mouse.move` in chunk JSONL by default: KEEP-MOVES

The unified-export-callable refactor proposed keeping `mouse.move` events in chunk
JSONL by default (R11/R12) instead of dropping them at the chunk-processor layer as
the pre-refactor pipeline did. Unit 9 gated that decision behind a four-threshold
operational audit. This doc preserves the durable result; the synthetic harness
(`benchmark_mouse_move_bloat.py`) and its result JSON were one-off pre-merge gates and
were removed in SCR-119 (recoverable from git history).

**Verdict: KEEP-MOVES.** The synthetic worst-case audit passed the two hard quantified
gates with wide margin. Keeping `mouse.move` does not bloat the chunk JSONL because the
unified export already downsamples moves heavily before they are written.

## What was measured

The audit fabricated a worst-case **600 s (10-min) idle-reading chunk** and ran it
through the production export → scrub path (`unified_export_events` →
`write_events_jsonl` → `scrub_events_jsonl`), comparing **keep-moves** (post-refactor)
against a **drop-moves baseline** (the pre-refactor behavior, reproduced by filtering
`mouse.move` rows before the callable).

Synthetic corpus: **41,255 `mouse.move` rows + 380 other action rows + 10 window
events** (alternating 60 s windows between a MASK_WINDOW app and an ALLOW app), 70% idle
reading at ~12 ms move cadence / 30% active interaction.

| Measurement | Keep-moves (default) | Drop-moves baseline | Gate | Result |
|---|---|---|---|---|
| Chunk JSONL size | **0.62 MB** (650,939 B) | 0.065 MB (66,592 B) | ≤ 50 MB | **PASS** (≈80× headroom) |
| Events written | 251 | 131 | — | 41,255 moves → +120 written events |
| Total wall time | 4.29 s | 3.41 s | ≤ 2.0× baseline | **PASS** (1.26×) |
| Scrub-walk fraction | 87% of total | — | ≤ 30% (directional only) | see below |
| Cloud Run parse RSS | — | — | ≤ 1.5× baseline | SKIPPED (synthetic) |

## Why keeping moves is safe

- **The export downsamples moves before writing.** 41,255 raw `mouse.move` rows produced
  only ~120 additional written events (251 vs 131) and **0.62 MB** of JSONL — three
  orders of magnitude under the 50 MB ceiling. The chunk does not grow with raw OS move
  density; it grows with the (small) downsampled event count.
- **Wall-time impact is minor.** Keeping moves cost 1.26× the drop-moves baseline wall
  time, well under the 2.0× gate.

## The scrub-fraction caveat (directional, not a regression)

The scrub walk consumed 87% of synthetic total wall time, over the 30% threshold — but
this is a **corpus artifact, not a moves regression**, and threshold #2 was directional-
only in synthetic mode:

- The drop-moves baseline already showed the same pattern (scrub was 3.39 s of 3.41 s
  total, ~99%), so removing moves would not bring the fraction under 30%.
- Adding moves changed scrub time by only **1.10×** (post/pre). The scrubber walks every
  event through Presidio + GLiNER; that per-event fixed cost dominates a synthetic
  corpus that lacks real chunk-pipeline work (transcription, screenshot masking, upload
  latency). Real recordings have far more total wall time, which pushes the scrub
  fraction down naturally.

## When to revisit

- If the chunk processor's event downsampling for `mouse.move` changes (denser writes),
  re-check the JSONL-size gate.
- The synthetic gate catches gross regressions only. A real **≥30-min idle-reading
  recording** remains the authoritative scrub-fraction (#2) and Cloud Run RSS (#4) check
  if the keep-moves default is ever re-litigated.
