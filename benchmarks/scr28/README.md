# SCR-28 spike — `openai/privacy-filter` vs GLiNER as the default text-PII backend

A self-contained evaluation harness that scores GLiNER and `openai/privacy-filter`
head-to-head and feeds a documented **swap / no-swap verdict**. It is **exploratory**:
there is no observed deficiency in GLiNER. No production code under `src/screencap/`
changes; the only shared-surface edit is the backward-compatible `score_predictions`
extraction in [`tests/privacy/test_benchmark.py`](../../tests/privacy/test_benchmark.py).

Plan: [`docs/plans/2026-05-29-004-feat-scr-28-openai-privacy-filter-spike-plan.md`](../../docs/plans/2026-05-29-004-feat-scr-28-openai-privacy-filter-spike-plan.md).

## The core idea: predictions are data, not live model calls

privacy-filter needs `transformers` 5.6.x; the repo pins 4.57.6 (transitive) and runs
GLiNER through `fast-gliner` (ONNX, no transformers at inference). Rather than force
both into one process, **each model runs in its own environment and emits predicted
spans as JSON** in a common schema ([`schema.py`](schema.py)). A pure-Python scorer
([`scorer.py`](scorer.py)) — reusing the in-repo scoring core `score_predictions` —
grades both JSONs against identical gold on identical entity definitions. The version
conflict lives entirely inside the two `run_*.py` processes; the scorer never imports
a model.

```
                 ┌─ run_gliner.py  (repo venv) ──────────┐
inputs (cases) ──┤                                        ├─→ predictions JSON ─→ scorer.py ─→ per-entity P/R tables
                 └─ run_privacy_filter.py (.venv-pf) ─────┘                         (score_predictions)
```

## Files

| File | Role | Env |
|------|------|-----|
| `schema.py` | Common prediction JSON schema (pure stdlib; loads in either env). | both |
| `io_utils.py` | `(case_id, text)` JSONL loader (pure stdlib). | both |
| `label_maps.py` | Native-label → `EntityType` maps (`OPENAI_ENTITY_MAPPING`; GLiNER reuses production maps). | repo |
| `scorer.py` | Load predictions JSON → map labels → merge adjacent same-type → `score_predictions` → tables/JSON. | repo |
| `run_gliner.py` | GLiNER runner (`full` / `ner` / `regex-secrets` modes) → predictions JSON. | repo |
| `run_privacy_filter.py` | privacy-filter runner (`pipeline` / `opf` decoders) → predictions JSON. | `.venv-pf` |
| `tier1_pii_masking.py` | *(U4, deferred)* load `ai4privacy/pii-masking-300k` → gold → orchestrate. | both |
| `measure_footprint.py` | *(U7, deferred)* download size, RSS, per-block latency/throughput. | both |
| `tier2_testbed/extract.py` | Pull OCR + AX text blocks from a real `recording.db` → `inputs.jsonl`. | repo |
| `tier2_testbed/ANNOTATION_GUIDELINES.md` | How to hand-label `gold.jsonl`. | — |
| `results/` | Per-model, per-tier JSON + Markdown outputs. | — |

Reusable scoring core: `score_predictions(tp_cases, fp_cases, predictions_by_case)` lives in
[`scoring_core.py`](scoring_core.py) and is re-exported from
[`tests/privacy/test_benchmark.py`](../../tests/privacy/test_benchmark.py) for the existing
tests. `run_benchmark(pipeline)` is now a thin wrapper over it. Behavior equivalence is
verified by the model-free `score_predictions` ⇄ `run_benchmark` equivalence tests in
[`tests/privacy/test_benchmark_scoring.py`](../../tests/privacy/test_benchmark_scoring.py)
plus the unchanged existing privacy suite — no golden artifact is committed.

## Two environments

### 1. Repo venv (GLiNER runner + scorer)

The existing project environment (`pip install -e ".[dev]"`, `transformers` 4.57.6 +
`fast-gliner` ONNX). In a worktree, prefer `PYTHONPATH=src` so imports resolve to *this*
checkout rather than wherever `pip install -e` last pointed.

```bash
PYTHONPATH=src python benchmarks/scr28/run_gliner.py \
    --mode ner --inputs-jsonl benchmarks/scr28/tier2_testbed/inputs.jsonl \
    --tier tier2 --out benchmarks/scr28/results/tier2-gliner-ner.json
```

### 2. `.venv-pf` (privacy-filter runner) — pinned by U1

privacy-filter requires `transformers` 5.6.x, which conflicts with the repo pin — hence
the separate venv. **U1 pinned the working stack: `transformers==5.6.2`, `torch 2.12.0`,
`datasets 4.8.5`** (the model's `config.json` stamps `5.6.0.dev0`; 5.6.2 loads it cleanly
on CPU).

```bash
python3.12 -m venv benchmarks/scr28/.venv-pf
benchmarks/scr28/.venv-pf/bin/pip install \
    "transformers==5.6.2" torch datasets
# Register screencap's package METADATA in the venv (no deps -> does NOT pull the
# conflicting transformers 4.57.6). Without this, `from screencap.privacy import
# normalize_text` raises PackageNotFoundError: src/screencap/__init__.py does
# `version("screencap")` at import, which needs the dist metadata present.
benchmarks/scr28/.venv-pf/bin/pip install -e . --no-deps

# run:
PYTHONPATH=src benchmarks/scr28/.venv-pf/bin/python \
    benchmarks/scr28/run_privacy_filter.py \
    --decoder pipeline --device cpu \
    --inputs-jsonl benchmarks/scr28/tier2_testbed/inputs.jsonl \
    --tier tier2 --out benchmarks/scr28/results/tier2-pf-ner.json
```

`.venv-pf` is gitignored (`.venv-*/`). The privacy-filter weights download to the HF
cache, not the repo — the HF `pipeline` path fetches **`model.safetensors` (~2.6 GB)**
(it cannot use the smaller ONNX variants without `optimum`+`onnxruntime`).

> **There is no `opf` CLI.** The plan assumed an official `opf --format json` decoder as
> the authoritative cross-check; **no such PyPI package exists** and the model card ships
> only the `pipeline` API. U1 therefore validated the `pipeline` decoder directly (offsets
> were byte-correct, 0/14 mismatches — see `U1_DECODING_VALIDATION.md`). The runner's
> `--decoder opf` path is retained but **non-functional**; use `--decoder pipeline`.
>
> **Decoding caveat:** HF `pipeline(aggregation_strategy="first")` does generic BIOES
> grouping over argmax, **not** the model's constrained Viterbi decoder, and emits spans
> that include a leading word-boundary space. Exact-span metrics are unreliable for
> privacy-filter; **partial/leak-relevant recall is the fair metric**. `"first"` is
> correct (`"simple"` fragments emails/phones — confirmed in U1).

## Run order

1. **U1** — set up `.venv-pf`, load privacy-filter on CPU, cross-validate the HF
   `pipeline(aggregation_strategy="first")` char offsets against `opf --format json` on a
   fixed sample set. If they diverge, use `--decoder opf`. Record the pinned `transformers`
   version above.
2. **U4 (Tier 1)** — `tier1_pii_masking.py` loads `ai4privacy/pii-masking-300k` (English
   test holdout), runs both runners, scores via `scorer.py`, and reproduces privacy-filter's
   **baseline** published F1 (token ~0.96, exact-span ~0.926 — *not* the un-downloadable
   "corrected" 0.974) as a wiring sanity check.
3. **U5 (Tier 2)** — `tier2_testbed/extract.py` pulls real OCR + AX blocks → `inputs.jsonl`;
   hand-label `gold.jsonl` per `ANNOTATION_GUIDELINES.md` (double-annotate a subset, record
   agreement).
4. **U6 (Tier 2 head-to-head)** — run both runners per block (no concatenation) in NER-only
   and full-pipeline modes (privacy-filter NER ∪ `run_gliner.py --mode regex-secrets`,
   resolved via `DetectionResolver`); score with `scorer.py`. **Decisive.**
5. **U7** — `measure_footprint.py`: disk size + RSS + per-block latency/throughput; plus the
   entity-coverage delta table.
6. **U8** — write `docs/spikes/<date>-scr-28-privacy-filter-verdict.md` applying AE1/AE2/AE3.

> **Status:** spike COMPLETE. All units (U1–U8) executed; verdict at
> [`docs/spikes/2026-06-05-scr-28-privacy-filter-verdict.md`](../../docs/spikes/2026-06-05-scr-28-privacy-filter-verdict.md)
> → **NO-SWAP** (keep GLiNER). Tier-1 (`tier1_pii_masking.py`), footprint/latency
> (`measure_footprint.py`), the scoring driver (`score_all.py`), the U1 decode validation
> (`_validate_decoding.py` + `U1_DECODING_VALIDATION.md`), and the Tier-2 gold draft
> (`tier2_testbed/_draft_gold.py`) are all in place. Raw results are gitignored under
> `results/` (`SCORES.md`, `FOOTPRINT.md`) — regenerate via the run order above.
>
> The Tier-2 gold is **agent-drafted** (per the plan); a human should reconcile it before
> treating any single Tier-2 number as authoritative. The verdict's direction is robust
> across four independent axes regardless.

## Data & PII handling

- **Tier-2 inputs/gold contain real PII.** `tier2_testbed/inputs.jsonl`, `gold.jsonl`, and
  `results/tier2-*` are **gitignored**. Commit only `extract.py`, `ANNOTATION_GUIDELINES.md`,
  and `*.example.jsonl` synthetic exemplars. Regenerate extracts locally via `extract.py`.
- **`ai4privacy/pii-masking-300k` license is not plainly permissive** (academic use with
  citation; commercial use requires contacting ai4privacy). Eval-only internal use; **do not
  commit dataset rows or ship the dataset.**
- Both models run **locally** — no captured text leaves the machine, same trust posture as
  the current GLiNER pipeline.

## Offset contract

Spans are half-open `[start, end)` char offsets into `screencap.privacy.normalize_text(case.text)`.
Each runner normalizes the case text with `normalize_text` before inference so its offsets align
with the text the scorer reconstructs per case. `label` is the model's **native** label; the
scorer maps it to an `EntityType` via `label_maps.py`. Adjacent same-type spans are merged at
**scoring** time (Presidio `SpanEvaluator` semantics), not by the runners.

## Tests

Model-free, fast (run in the repo venv):

```bash
PYTHONPATH=src python -m pytest tests/privacy/test_benchmark_scoring.py tests/privacy/test_scr28_runners.py -q
```
