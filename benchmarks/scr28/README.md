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

### 2. `.venv-pf` (privacy-filter runner) — DEFERRED setup

privacy-filter requires `transformers` 5.6.x, which conflicts with the repo pin — hence
the separate venv. **The exact working `transformers` version is pinned empirically in
U1** (the model's `config.json` stamps `5.6.0.dev0`; the "≥4.50" claim is unreliable).
Record it here once U1 confirms it.

```bash
python3.12 -m venv benchmarks/scr28/.venv-pf
benchmarks/scr28/.venv-pf/bin/pip install \
    "transformers==<PINNED-IN-U1>" torch datasets
# the official decoder CLI (authoritative fallback if HF pipeline offsets diverge):
benchmarks/scr28/.venv-pf/bin/pip install opf   # privacy-filter CLI

# run (the runner adds repo src/ to sys.path for normalize_text only — no torch
# conflict, since screencap.privacy is pure-Python):
PYTHONPATH=src benchmarks/scr28/.venv-pf/bin/python \
    benchmarks/scr28/run_privacy_filter.py \
    --decoder pipeline --device cpu \
    --inputs-jsonl benchmarks/scr28/tier2_testbed/inputs.jsonl \
    --tier tier2 --out benchmarks/scr28/results/tier2-pf-ner.json
```

`.venv-pf` is gitignored (`.venv-*/`). The privacy-filter weights (~0.8–2.8 GB depending
on variant) download to the HF cache, not the repo.

> **`--decoder opf` scale ceiling:** `opf` spawns one subprocess per input, so it is
> intended for Tier-2 block scale (tens–hundreds of inputs). Above ~1000 inputs the
> per-process startup cost dominates and the runner warns; use `--decoder pipeline`
> (in-process, single model load) for Tier-1 sweeps.

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

> **Status (this session):** harness foundation only — `schema.py`, `io_utils.py`,
> `label_maps.py`, `scorer.py`, both runners, the `score_predictions` extraction, and
> `tier2_testbed/extract.py` + guidelines are built and tested. Steps 1, 2, 4, 5, 6 and the
> `gold.jsonl` labeling are deferred (need the `.venv-pf` + model/dataset downloads and human
> annotation).

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
