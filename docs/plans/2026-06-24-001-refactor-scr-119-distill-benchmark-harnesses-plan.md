---
title: "refactor: Distill remaining one-off benchmark harnesses to docs/solutions (SCR-119)"
type: refactor
status: active
date: 2026-06-24
---

# refactor: Distill remaining one-off benchmark harnesses to docs/solutions (SCR-119)

## Summary

Apply an explicit keep/distill/delete decision to every remaining file under `benchmarks/`, following the PR #217 precedent (commit `6a86fcd5`): capture durable findings as focused `docs/solutions/research/` learnings, then remove the one-off mouse-move audit harness and the three stale JSON result artifacts, keep `benchmark_pii.py` as a maintained tool with a short README, and re-verify that nothing in `src/` or `tests/` imports from `benchmarks/`.

---

## Problem Frame

As the repo went public, the team began retiring one-off spike/benchmark code so the tree reflects only what's maintained. PR #217 set the pattern for SCR-28: a ~4k-line benchmark harness was distilled to a single `docs/solutions/research/scr-28-privacy-filter-vs-gliner.md` learning, the harness was removed, and the one piece of test-load-bearing code was relocated into `tests/`. SCR-119 is the follow-up pass for the **remaining** `benchmarks/` files. Leaving them in place is low-grade clutter: a future reader can't tell which benchmarks are live tools versus dead spike output, and the stale result JSONs imply a currency they no longer have.

---

## Requirements

- R1. Every remaining `benchmarks/*` file has an explicit keep/distill/delete decision applied.
- R2. Durable findings from distilled/deleted benchmarks are captured under `docs/solutions/`.
- R3. No production (`src/`) or test (`tests/`) code imports from `benchmarks/` (re-audited after any moves).
- R4. `benchmark_pii.py` is retained as a maintained tool, documented by a short `benchmarks/README.md`.
- R5. Full test suite + privacy suite green; ruff clean.

---

## Scope Boundaries

- Not re-running the benchmarks to refresh numbers — distill the committed JSON outputs as-is.
- Not modifying the existing `docs/solutions/research/scr-28-privacy-filter-vs-gliner.md` or the `tests/redaction/` scoring core (`scoring_core.py`, `test_benchmark.py`, `fixtures/test_corpus.py`) — both were settled in PR #217.
- Not cleaning `benchmark_results/` — it is gitignored (`.gitignore:27`) and untracked; nothing to remove.
- Not refactoring `benchmark_pii.py`'s import of `tests.redaction.*` — see Key Technical Decisions.

### Deferred to Follow-Up Work

- The completed SCR-28 spike plan `docs/plans/2026-05-29-004-feat-scr-28-openai-privacy-filter-spike-plan.md` references the soon-to-be-deleted `baseline_before_fast_gliner.json/` and `after_fast_gliner.json/` artifacts (and already carries pre-SCR-33 stale paths like `tests/privacy/test_benchmark.py`). It is a historical record of shipped work; leave it untouched rather than rewriting a completed plan doc. Flagged here only so the dangling references are a known, deliberate non-fix.

---

## Context & Research

### Relevant Code and Patterns

- **Precedent to mirror:** PR #217, commit `6a86fcd5 refactor(scr28): distill spike to a docs/solutions learning; relocate scoring core`, and its output `docs/solutions/research/scr-28-privacy-filter-vs-gliner.md`. That doc's shape (YAML frontmatter with `problem_type: evaluation`, `decision:`, `references:`; a verdict; a "why" with a metrics table; "reusable learnings"; "when to revisit") is the template for the new distillation docs.
- **`benchmarks/benchmark_pii.py`** (154 lines) — standalone PII benchmark runner. `--engine {presidio,presidio-gliner,full}` builds a `screencap.redaction.DetectionPipeline`; `--compare A.json B.json` diffs two result JSONs. It imports `run_benchmark` / `print_benchmark_table` / `save_benchmark_json` from `tests/redaction/test_benchmark.py`, which re-exports them from `tests/redaction/scoring_core.py`.
- **`benchmarks/benchmark_mouse_move_bloat.py`** (679 lines) — synthetic-corpus audit for the "R11 keep-mouse.move chunk-processor default" decision, written as Unit 9 of the unified-export-callable refactor. Its referenced plan (`docs/plans/2026-04-26-001-refactor-unified-export-callable-plan.md`) no longer exists in the repo; the refactor it gated is shipped. Drives the production export → scrub path (`unified_export_events` → `write_events_jsonl` → `scrub_events_jsonl`) over a fabricated worst-case chunk.
- **`tests/redaction/test_benchmark.py`** — the live, hard-gated privacy benchmark (recall ≥95%, precision ≥80%, doc-leak ≤5%, redaction-survival <5%). This is the test-load-bearing home of the scoring core; `benchmark_pii.py` is a thin CLI shell over it.
- **Result artifacts (all git-tracked):** `benchmarks/2026-04-27-mouse-move-bloat-synthetic.json` (output of the mouse-move audit), and the directories `benchmarks/baseline_before_fast_gliner.json/2026-03-15-full.json` + `benchmarks/after_fast_gliner.json/2026-03-15-full.json` (PII-benchmark outputs bracketing the spaCy→fast-gliner ONNX swap).

### Institutional Learnings

- `docs/solutions/research/scr-28-privacy-filter-vs-gliner.md` already records *why* the repo runs GLiNER via `fast-gliner` (ONNX, no `transformers` at inference) — footprint + bundled-binary constraints. The new fast-gliner doc records the *accuracy cost* of that migration, complementing it.
- Repo `docs/solutions/` convention (and the `compound` skill) is one focused learning per file — hence two separate distillation docs rather than a combined one.

### Audit findings (already gathered, ground the plan)

- `grep -rn "benchmarks" src/ tests/` returns only a docstring word-mention in `tests/redaction/test_benchmark.py:60` and `fixtures/test_corpus.py:1` — **no imports**. R3 already holds; the unit re-confirms it after deletions.
- The only `sys.path.insert(... "benchmarks" ...)`-style coupling is *inside* the benchmark files reaching **into** `src/` — i.e. benchmarks depending on the repo, which is fine for standalone tools. This is the inverse of the smell #217 fixed.
- No CI workflow (`.github/`), `Makefile`, `pyproject.toml`, or `setup.cfg` references `benchmarks/` — deletions do not touch build/lint/test config.
- Per `CLAUDE.md`, ruff is scoped to the engine sub-package (`ruff check src/screencap/engine/`); this work adds only Markdown + a README and deletes Python, so the lint surface is effectively unchanged.

---

## Key Technical Decisions

- **Keep `benchmark_pii.py`** (the decision the issue left open). The production PII pipeline and the hard-gated privacy suite are both alive, and this runner re-measures exactly those gated metrics across `presidio`/`presidio-gliner`/`full` engines — a genuinely reusable tuning tool, not dead spike output. Document it with a short `benchmarks/README.md`.
- **Do not "fix" `benchmark_pii.py`'s `tests.redaction.*` import.** It is a dev harness (never shipped, imported by nothing), and depending on the test corpus/scorer is the inverse of the #217 smell (test/prod → benchmarks), which R3 targets. Note it in the README; leave the code as-is.
- **Distill into two focused docs, not one.** The mouse-move finding is an operational/perf audit of a chunk-processing default; the fast-gliner finding is a PII-accuracy delta. Separate topics → separate `docs/solutions/research/` files, matching the one-learning-per-file convention.
- **Capture-then-delete ordering.** Within each removal unit, write (and self-review) the distillation doc *first*, then delete the source script/artifacts in the same commit — never delete before the durable numbers are captured.
- **Leave the completed SCR-28 plan doc's now-dangling artifact references untouched** (see Deferred to Follow-Up Work) — rewriting a shipped historical plan is out of scope and counter to treating plans as historical records.

---

## Open Questions

### Resolved During Planning

- *Keep vs. distill `benchmark_pii.py`?* → **Keep + README** (see Key Technical Decisions). User-confirmed.
- *One distillation doc or two?* → **Two** focused `research/` docs (mouse-move audit; fast-gliner accuracy tradeoff).
- *Does anything depend on the files being removed?* → No (grep audit; no CI/config references). Re-verified in U4.

### Deferred to Implementation

- Exact filenames/slugs for the two new docs — proposed below, adjustable at write time to match any preferred `docs/solutions/research/` naming.
- Whether to add a one-line pointer to the new docs from the SCR-28 doc's "see also" — optional, decide while writing.

---

## Implementation Units

### U1. Distill the mouse-move bloat audit, then remove the harness + its artifact

**Goal:** Capture the durable findings of the R11 keep-mouse.move audit in a `docs/solutions/` learning, then delete the one-off script and its result JSON.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Create: `docs/solutions/research/mouse-move-chunk-jsonl-bloat-audit.md` (slug adjustable)
- Delete: `benchmarks/benchmark_mouse_move_bloat.py`
- Delete: `benchmarks/2026-04-27-mouse-move-bloat-synthetic.json`

**Approach:**
- Write the doc in the SCR-28 frontmatter style, mirroring its full field set so both new docs stay consistent: `problem_type: evaluation`, `component: screencap.pipeline`, `module: pipeline`, `decision:` capturing "keep mouse.move in chunk JSONL by default", `tags:` (chunk-processing / mouse-move / perf-audit), `references:` to this plan and the relevant chunk-processor/export modules.
- Record the durable numbers from the committed synthetic JSON so they survive deletion: synthetic worst-case 600 s chunk → **41,255 mouse.move + 380 other action rows + 10 window events** collapse to **251 events written** / **~0.62 MB JSONL** (export downsamples moves heavily). Decision gate: JSONL 0.62 MB ≪ 50 MB (PASS); total wall ratio **1.26×** ≪ 2.0× (PASS); scrub-walk fraction was **directional-only** in synthetic mode (the per-event Presidio/GLiNER fixed cost dominates a synthetic corpus that lacks transcription/screenshot-masking/upload latency — see the harness's own diagnosis), and Cloud Run RSS was skipped.
- State the durable conclusion: keeping `mouse.move` in chunk JSONL by default does **not** bloat chunks (the unified export already downsamples moves to a small event count), and the synthetic gate only catches gross regressions — a real ≥30-min idle-reading recording remains the authoritative #2 check if ever revisited.
- Before deleting, grep the repo for `benchmark_mouse_move_bloat` references outside `benchmarks/` (expect only this plan); confirm none in CI/config.

**Patterns to follow:** `docs/solutions/research/scr-28-privacy-filter-vs-gliner.md` (structure, frontmatter, "when to revisit" section).

**Test scenarios:**
- Test expectation: none — documentation write + deletion of an unimported standalone script; no behavioral code path changes. Coverage is the U4 verification gate.

**Verification:**
- New doc exists, renders, and contains the JSONL-size / wall-ratio numbers (so deletion loses nothing durable).
- `benchmark_mouse_move_bloat.py` and `2026-04-27-mouse-move-bloat-synthetic.json` are gone; `git grep benchmark_mouse_move_bloat` returns only this plan (and the new doc if it names the old script).

---

### U2. Distill the fast-gliner accuracy tradeoff, then delete the two result artifacts

**Goal:** Capture the measured accuracy delta of the spaCy→fast-gliner ONNX swap, then delete the stale baseline/after result directories.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Create: `docs/solutions/research/fast-gliner-onnx-accuracy-tradeoff.md` (slug adjustable)
- Delete: `benchmarks/baseline_before_fast_gliner.json/` (directory, incl. `2026-03-15-full.json`)
- Delete: `benchmarks/after_fast_gliner.json/` (directory, incl. `2026-03-15-full.json`)

**Approach:**
- Write the doc in the SCR-28 style (`problem_type: evaluation`, `component: screencap.redaction`, `module: redaction`, `tags:` pii-detection/gliner/benchmarking, `references:` to the SCR-28 doc + this plan).
- Record the durable delta from the two committed JSONs (baseline = before fast-gliner; after = fast-gliner ONNX), measured on a **40-TP / 41-FP-probe corpus (n≈81)**: F1 **0.959 → 0.940**; precision **0.959 → 0.922**; partial-overlap recall **unchanged at 0.959**; total false positives **2 → 4** — **PERSON +2** (FP 1→3) and **ADDRESS +1** (FP 0→1), partially offset by **EMAIL −1** (FP 1→0), all within the `pii-gliner` source; document-leak rate **unchanged (5%)**; redaction-survival **unchanged (4.1%)**. (The per-type deltas must sum to the stated net before the source JSON is deleted.)
- State the durable conclusion: the fast-gliner ONNX migration (adopted for footprint/speed and to avoid `transformers` at inference — cross-link the SCR-28 doc) **held recall, doc-leak, and redaction-survival** while costing ~3.7 precision points via a small rise in PERSON/ADDRESS false positives. Note this is a single small (n≈81-case) PII-sparse corpus, so it is directional. Add a "when to revisit" pointer (e.g. re-measure if PERSON/ADDRESS FP rate becomes a concern).
- Before deleting, grep for `fast_gliner.json` / `baseline_before_fast_gliner` / `after_fast_gliner` references outside `benchmarks/` (expect only the completed SCR-28 plan doc, which is deliberately left stale — see Scope Boundaries).

**Patterns to follow:** `docs/solutions/research/scr-28-privacy-filter-vs-gliner.md` metrics-table treatment.

**Test scenarios:**
- Test expectation: none — documentation write + deletion of untracked-by-code result artifacts; no code path changes.

**Verification:**
- New doc exists and contains the precision/recall/F1/FP numbers.
- Both `*_fast_gliner.json/` directories are gone; the only remaining repo references to them are in the historical SCR-28 plan doc (known, deliberate per Scope Boundaries).

---

### U3. Keep `benchmark_pii.py`; document it with a benchmarks README

**Goal:** Make the keep decision explicit and discoverable by adding a short README that describes the retained tool and why it stays.

**Requirements:** R1, R4

**Dependencies:** None

**Files:**
- Create: `benchmarks/README.md`
- (No change to `benchmarks/benchmark_pii.py`.)

**Approach:**
- README documents: what `benchmark_pii.py` is (PII detection benchmark runner), how to run it (`--engine {presidio,presidio-gliner,full}`, `--compare A.json B.json`, `--output-dir`), where results go (`benchmark_results/`, gitignored), and its relationship to the gated privacy suite (`tests/redaction/test_benchmark.py`).
- Note explicitly that the runner imports the scoring core from `tests/redaction/` (acceptable dev-harness direction; the inverse of the smell SCR-119/PR #217 targets) so a future reader doesn't "fix" it.
- Keep it short — a tool pointer, not a manual.

**Patterns to follow:** concise tool-README tone; mirror the usage block already in `benchmark_pii.py`'s module docstring.

**Test scenarios:**
- Test expectation: none — documentation-only addition; the kept script is unchanged. Its runnability is checked in U4.

**Verification:**
- `benchmarks/README.md` exists and accurately reflects the current `benchmark_pii.py` CLI.
- `benchmarks/` now contains exactly `benchmark_pii.py` + `README.md` (post-U1/U2).

---

### U4. Final import audit + green-suite / ruff verification gate

**Goal:** Confirm the acceptance criteria hold after the removals: no `src/` or `tests/` code imports from `benchmarks/`, and the full + privacy suites and ruff are green.

**Requirements:** R3, R5

**Dependencies:** U1, U2, U3

**Files:**
- No source changes expected (verification only). If the audit surfaces an unexpected import into `benchmarks/`, fix it here and record what changed.

**Approach:**
- Re-run the import audit: `grep -rn "benchmarks" src/ tests/` and a Python-import-shaped search (`from benchmarks` / `import benchmarks`) across `src/` and `tests/`; expect only the existing docstring word-mentions, zero imports.
- Smoke-check the kept tool still resolves its imports after the deletions, e.g. `python benchmarks/benchmark_pii.py --help` (run with `PYTHONPATH=src` per the worktree testing note in project memory).
- Run the full suite and the privacy-marked subset; run ruff per repo convention.

**Execution note:** Verification-only — expected to produce no diff. Treat a non-empty `from benchmarks`/`import benchmarks` result in `src/` or `tests/` as a hard failure to resolve before closing.

**Test scenarios:**
- Verify (import audit): `grep -rn -e "from benchmarks" -e "import benchmarks" src/ tests/` → no matches.
- Verify (tool intact): `PYTHONPATH=src python benchmarks/benchmark_pii.py --help` exits 0.
- Verify (suites): full `pytest` green; `pytest -m privacy` green (recall/precision/leak/survival gates intact).
- Verify (lint): ruff clean on the repo's configured scope.

**Verification:**
- All four acceptance-criteria boxes in SCR-119 can be checked: per-file decision applied (U1–U3), durable findings captured (U1, U2), no prod/test imports from `benchmarks/` (this unit), suites + ruff green (this unit).

---

## System-Wide Impact

- **Interaction graph:** `benchmarks/` is an isolated, dev-only tree — not imported by `src/` or `tests/`, not referenced by CI/`Makefile`/`pyproject.toml`. Removing the mouse-move harness and artifacts has no runtime or test blast radius.
- **Unchanged invariants:** The production PII pipeline (`screencap.redaction`), the gated privacy suite (`tests/redaction/test_benchmark.py` + `scoring_core.py` + `fixtures/test_corpus.py`), and the SCR-28 learning doc are explicitly **not** touched. `benchmark_pii.py`'s CLI behavior is unchanged.
- **State lifecycle risks:** None — no persistent data, DB, or capture path is involved.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Deleting a file something secretly depends on | Grep audit already shows no `src/`/`tests/`/CI/config references; U1/U2 re-grep per-filename before deleting; U4 is a final import-audit gate. |
| Losing durable benchmark numbers on deletion | Capture-then-delete ordering: the distillation doc (with the concrete numbers) is written and self-reviewed in the same unit/commit *before* the source is removed. |
| Dangling references to deleted artifacts in the historical SCR-28 plan doc | Deliberate non-fix, documented under Deferred to Follow-Up Work; that doc is a shipped historical record and already carries pre-SCR-33 stale paths. |
| Distillation docs drift into restating the harness instead of the finding | Follow the SCR-28 doc shape — lead with the verdict/decision and the durable numbers, not the script mechanics. |

---

## Sources & References

- Linear: [SCR-119](https://linear.app/zk-email/issue/SCR-119/clean-up-remaining-one-off-benchmark-harnesses-distill-to)
- Precedent: PR #217 (commit `6a86fcd5`); [docs/solutions/research/scr-28-privacy-filter-vs-gliner.md](docs/solutions/research/scr-28-privacy-filter-vs-gliner.md)
- Files under decision: [benchmarks/benchmark_pii.py](benchmarks/benchmark_pii.py), [benchmarks/benchmark_mouse_move_bloat.py](benchmarks/benchmark_mouse_move_bloat.py), `benchmarks/2026-04-27-mouse-move-bloat-synthetic.json`, `benchmarks/baseline_before_fast_gliner.json/`, `benchmarks/after_fast_gliner.json/`
- Live gated suite: [tests/redaction/test_benchmark.py](tests/redaction/test_benchmark.py), [tests/redaction/scoring_core.py](tests/redaction/scoring_core.py)
