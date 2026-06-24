---
title: "spaCy → fast-gliner ONNX swap: recall/leak held, ~3.7 precision points traded"
date: 2026-06-24
problem_type: evaluation
component: screencap.redaction
module: redaction
platform: macos
decision: ACCEPTED (fast-gliner ONNX is the shipped GLiNER backend)
tags:
  - pii-detection
  - gliner
  - fast-gliner
  - onnx
  - benchmarking
  - model-evaluation
references:
  - "docs/solutions/research/scr-28-privacy-filter-vs-gliner.md (why the repo runs GLiNER via fast-gliner ONNX)"
  - "Distilled from benchmarks/baseline_before_fast_gliner.json + benchmarks/after_fast_gliner.json (2026-03-15 runs, removed in SCR-119)"
  - "docs/plans/2026-06-24-001-refactor-scr-119-distill-benchmark-harnesses-plan.md"
---

# fast-gliner ONNX swap — accuracy tradeoff

ScreenCap runs GLiNER through `fast-gliner` (ONNX, no `transformers` at inference) for
footprint and bundled-binary reasons — see
[scr-28-privacy-filter-vs-gliner.md](scr-28-privacy-filter-vs-gliner.md) for *why* that
runtime was chosen. This doc preserves the **accuracy cost** of that swap, measured by
`benchmark_pii.py` immediately before and after the migration (2026-03-15). The two
result JSONs were removed in SCR-119; the numbers live here.

**Verdict: ACCEPTED.** The swap **held recall, document-leak rate, and redaction
survival** while trading ~3.7 precision points via two extra PERSON/ADDRESS false
positives. For ScreenCap (PII-sparse dev-screen captures where the precision/FP axis is
the one that matters) this was an acceptable cost for the footprint/speed win.

## Measured delta

Corpus: the standard privacy benchmark — **40 true-positive cases + 41 false-positive
probe cases (n ≈ 81)**, PII-sparse, partly synthetic.

| Metric | Baseline (spaCy/pre-fast-gliner) | After (fast-gliner ONNX) | Delta |
|---|---|---|---|
| F1 | 0.959 | 0.940 | −0.019 |
| Precision | 0.959 | 0.922 | −3.7 pts |
| Partial-overlap recall | 0.959 | 0.959 | **unchanged** |
| Exact-span recall | 0.653 | 0.633 | −0.020 |
| Total false positives | 2 | 4 | +2 (net) |
| Document-leak rate | 5% (2/40) | 5% (2/40) | **unchanged** |
| Redaction-survival rate | 4.1% (2/49) | 4.1% (2/49) | **unchanged** |
| Weighted FP impact | 110 | 211 | +101 |

**False-positive movement (net +2, all in the `pii-gliner` source):**

- PERSON: FP 1 → 3 (**+2**)
- ADDRESS: FP 0 → 1 (**+1**)
- EMAIL: FP 1 → 0 (**−1**, precision 0.917 → 1.0)

The three per-type movements sum to the +2 net total — the EMAIL improvement partially
offset the PERSON/ADDRESS regressions.

## Reading

- **The cost is precision, not recall.** No true positive was lost (partial recall held
  at 0.959), and the safety-critical aggregates — document-leak rate and redaction-
  survival — were unchanged. The swap made the model slightly more eager to flag
  PERSON/ADDRESS on noisy OCR, not blinder to real PII.
- **Directional, not definitive.** A single small (n ≈ 81), PII-sparse corpus. Treat the
  exact point values as indicative; the durable signal is "recall/leak held, modest
  precision cost," not the third decimal place.

## When to revisit

- If PERSON/ADDRESS false-positive rate on real recordings becomes a user-visible
  problem, this is the swap that introduced the regression — re-measure against a larger,
  real OCR corpus before tuning thresholds.
- If the `fast-gliner` ONNX model version changes, re-run `benchmark_pii.py --engine
  presidio-gliner` (and `--engine presidio` for the spaCy reference) and compare.
