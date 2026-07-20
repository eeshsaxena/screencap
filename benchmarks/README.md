# benchmarks/

Maintained developer tools for benchmarking Screencap. Not shipped, not imported by
`src/` or `tests/`. One-off spike/audit harnesses are not kept here — they are distilled
to `docs/solutions/research/` and removed (see SCR-119 and PR #217 for the precedent).

## `benchmark_pii.py` — PII detection benchmark runner

Re-measures the privacy detection pipeline's precision / recall / leak / survival
metrics — the same metrics the gated privacy suite asserts in
`tests/redaction/test_benchmark.py` (recall ≥ 95%, precision ≥ 80%, document-leak ≤ 5%,
redaction-survival < 5%). Use it to see how a tuning change moves those numbers across
detector backends before relying on the pass/fail gates.

```bash
# Run a single engine and save results to benchmark_results/<date>-<engine>.json
python benchmarks/benchmark_pii.py --engine full              # GLiNER + resolver + heuristic filter (default)
python benchmarks/benchmark_pii.py --engine presidio          # spaCy NER backend
python benchmarks/benchmark_pii.py --engine presidio-gliner   # GLiNER NER backend, no resolver/filter

# Diff two saved result JSONs side-by-side (totals, FP-by-source, per-type)
python benchmarks/benchmark_pii.py --compare benchmark_results/a.json benchmark_results/b.json
```

Results are written under `benchmark_results/` (gitignored) by default; override with
`--output-dir`.

### Note on imports

`benchmark_pii.py` imports its scoring core and corpus from `tests/redaction/`
(`run_benchmark`, `print_benchmark_table`, `save_benchmark_json` via
`tests/redaction/test_benchmark.py`, which re-exports `tests/redaction/scoring_core.py`,
plus the corpus in `tests/redaction/fixtures/test_corpus.py`). This benchmark-depends-on-
tests direction is intentional and is the *inverse* of the "production/test code reaching
into `benchmarks/`" smell that SCR-119 / PR #217 guard against — do not "fix" it by
copying the scorer in here.

Because nothing in the normal test run exercises this tool, it is not protected from
drift if the scoring core's signatures change. Run `pytest -m privacy` (or
`python benchmarks/benchmark_pii.py --help` for a quick import smoke-check) after editing
`tests/redaction/scoring_core.py` to confirm the runner still resolves.
