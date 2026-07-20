---
title: "openai/privacy-filter vs GLiNER as the default text-PII backend — NO-SWAP"
date: 2026-06-05
problem_type: evaluation
component: screencap.privacy
module: privacy
platform: macos
decision: NO-SWAP (keep GLiNER)
tags:
  - pii-detection
  - model-evaluation
  - gliner
  - privacy-filter
  - benchmarking
  - secrets-detection
references:
  - "PR #211 (spike harness foundation)"
  - "PR #217 (spike execution + verdict, then distilled to this doc)"
  - "docs/plans/2026-05-29-004-feat-scr-28-openai-privacy-filter-spike-plan.md"
  - "docs/brainstorms/2026-05-29-scr-28-openai-privacy-filter-spike-requirements.md"
---

# SCR-28: keep GLiNER; do not swap to `openai/privacy-filter`

**Exploratory** spike — there was no observed deficiency in GLiNER. The question:
should `openai/privacy-filter` (Apache-2.0, MoE token classifier, April 2026)
replace `knowledgator/gliner-pii-base-v1.0` as Screencap's single default text-PII
NER backend, fed by Apple Vision OCR + accessibility text?

**Verdict: NO-SWAP.** The full evaluation harness lived in PRs #211/#217 (recoverable
from git history); it was distilled to this doc rather than carried in the repo,
since it was one-off spike code and the durable value is the decision + the
learnings below.

## Why (confidence rests on three testbed-independent structural axes)

1. **Footprint.** privacy-filter is **2.6 GB on disk vs GLiNER's 196 MB (~14×)** and
   ~**3.8× slower** per OCR block on CPU (32 ms vs 8.5 ms; 29 vs 107 blocks/sec). RAM
   is comparable (~1.3 GB; the MoE keeps active params small). A `transformers`+`torch`
   backend also reopens the minos/PyInstaller bundling matrix the shipped binary
   deliberately avoids (see [build-errors learnings](../build-errors/macos-pre14-binary-install-failure.md)
   and [pyinstaller-frozen-binary-ci-failures.md](../build-errors/pyinstaller-frozen-binary-ci-failures.md)).
   For a distributed CLI/app this is close to decisive on its own.
2. **Native NER coverage gap.** privacy-filter's taxonomy has **no SSN and no
   credit-card class** — swapping the NER backend means leaning entirely on the
   regex/secrets layer for two of the highest-harm types (it scored 0% NER recall on
   both).
3. **Decoding integration cost.** The only pip-installable way to run the model (HF
   `pipeline`) applies **generic BIOES grouping, not the model's constrained Viterbi
   decoder** (there is **no `opf` PyPI package** despite the model card implying a CLI),
   and does **not reproduce the published accuracy**. A faithful integration would have
   to port the Viterbi decoder.

The accuracy evidence points the same way but is **directional** (small / partly
synthetic test sets — Screencap's real recordings are PII-sparse):

| Dimension | GLiNER | privacy-filter |
|---|---|---|
| Tier-1 PII-Masking-300k (n=400) person/email partial recall | **57% / 100%** | 47% / 60% |
| Tier-1 ADDRESS partial recall | 22% | **54%** (pf better) |
| Tier-2 synthetic OCR-noise (n=25) partial recall / doc-leak | **100% / 0%** | 69% / 32% |
| Tier-2 real Screencap OCR (2 in-scope PII, 182 FP probes) | caught both, 18 FP | caught both, **8 FP** (pf cleaner) |
| Secrets (binary bucket, 14 secrets) — see below | n/a (NER) | 50% |

privacy-filter has genuine strengths (better Tier-1 ADDRESS recall, fewer false
positives on noisy dev-screen OCR, broader native categories). This is a **fit**
decision for Screencap's current 6-type `EntityType` set and bundled-binary
constraint, not a quality judgment on the model.

## Secrets / API-keys — augment, not swap

privacy-filter's one capability GLiNER's NER lacks is a native `secret` class. Scored
against Screencap's existing regex + detect-secrets layer on a synthetic, code-OCR
set (14 secrets — AWS/GitHub/Stripe/Slack/Google/OpenAI keys, JWT, RSA key, conn
string, npm, basic-auth, password — + 6 entropy-trap distractors), collapsed to a
binary SECRET bucket:

| Backend | Partial recall | Precision |
|---|---|---|
| **Screencap regex + detect-secrets (current)** | **79%** (11/14) | 100% |
| privacy-filter `secret` NER | 50% (7/14) | 100% |
| GLiNER NER (baseline) | 0% | — |

Screencap's existing layer **beats** privacy-filter's native secret class, and neither
over-flags the entropy traps. But they are **complementary**: privacy-filter caught 2
secrets detect-secrets missed (an unprefixed hex key + base64 basic-auth, via context),
while detect-secrets caught the prefixed provider tokens privacy-filter missed — union
recall **93%**. So privacy-filter's `secret` head is an *augment* opportunity (added
alongside the existing layer), never a swap argument — and it carries the same 2.6 GB /
decoder cost.

## Reusable learnings (the compounding value)

- **Decouple inference from scoring via JSON when models have conflicting deps.**
  privacy-filter needs `transformers` 5.6.x; the repo pins 4.57.6 + runs GLiNER via
  `fast-gliner` ONNX. Rather than reconcile, each model ran in its own venv and emitted
  predicted spans as JSON in a common schema; a pure-Python scorer graded both. The
  version conflict lived entirely inside the two runner processes. This pattern
  generalizes to any "compare model A vs B with incompatible runtimes" task.
- **HF `pipeline` ≠ the model's own decoder.** `openai/privacy-filter` ships a
  constrained Viterbi decoder, but `pipeline(task="token-classification",
  aggregation_strategy="first")` does generic argmax+BIOES grouping (it warns
  *"Tokenizer does not support real words, using fallback heuristic"*). Symptoms:
  exact-span recall collapsed to ~0%, and the published F1 (token 0.96 / exact-span
  0.926) was **not** reproducible (best faithful char-level binary ≈ 0.64). Always
  validate the decoding path before trusting a model's headline numbers. Use `"first"`,
  not `"simple"` (which fragments emails/phones). Offsets include a **leading space** —
  harmless for redaction (over-covers) but breaks exact-boundary matching, so
  **partial / leak-relevant recall is the fair metric**.
- **Screencap's real recordings are PII-sparse.** Dev-screen captures; AX text across
  all recordings yielded 0 emails/phones/SSNs. A credible per-type recall read needs
  synthetic OCR-noise augmentation — the real set is best for the **precision / false-
  positive** axis (does a backend hallucinate PII on code/paths/timestamps?).
- **Footprint is a bundled-binary question, not a parameter count.** A 50M-active MoE
  is still 2.6 GB of safetensors on disk and reopens the `minos`-contagion / frozen-
  binary smoke-test matrix. Measure the *download + bundling* cost, not just RAM.
- **Gotcha — `screencap` metadata in an isolated venv.** `src/screencap/__init__.py`
  calls `version("screencap")` at import, so any venv that imports `screencap` (even
  just for `normalize_text`) needs `pip install -e . --no-deps` to register the dist
  metadata — `--no-deps` avoids pulling the conflicting `transformers` pin.

## When to revisit

- Screencap wants **date / url / account-number** as first-class redaction types
  (privacy-filter detects these natively; GLiNER does not).
- The **bundled-binary constraint** relaxes (e.g. a server-side redaction tier where
  2.6 GB + `transformers`/`torch` are acceptable).
- Someone ports the **Viterbi decoder** so the published accuracy is reachable **and**
  closes the SSN/credit-card gap.
- **Secrets augment:** adding privacy-filter's `secret` head alongside (not replacing)
  the detect-secrets layer, if secrets coverage becomes a priority and the footprint
  math clears.

## Privacy posture

privacy-filter runs **fully locally** (transformers/ONNX, CPU) — same trust posture as
the current GLiNER pipeline. The swap question is purely fit/cost, not a privacy
regression either way.
