---
title: "SCR-28 Verdict: openai/privacy-filter vs GLiNER as default text-PII backend"
type: spike-verdict
status: complete
date: 2026-06-05
plan: docs/plans/2026-05-29-004-feat-scr-28-openai-privacy-filter-spike-plan.md
origin: docs/brainstorms/2026-05-29-scr-28-openai-privacy-filter-spike-requirements.md
decision: NO-SWAP
---

# SCR-28 Verdict — keep GLiNER; do **not** swap to `openai/privacy-filter`

## Exploratory framing (R2)

This spike was **exploratory**. There is **no observed deficiency in GLiNER** and
nothing was broken. The question was narrow: should `openai/privacy-filter`
(Apache-2.0, MoE token classifier, April 2026) replace `knowledgator/gliner-pii-base-v1.0`
as ScreenCap's single default text-PII NER backend? The risk it guarded against is
that a model whose headline F1 is measured on clean synthetic prose can lose on
ScreenCap's real input — noisy, fragmentary OCR / accessibility text.

## Verdict (R1): **NO-SWAP**

Keep GLiNER as the default backend. **The confidence comes primarily from three
testbed-independent structural axes** — these hold regardless of how thin or
synthetic the accuracy testbed is. The accuracy evidence then points the same way,
but is treated as **directional** (small / partly-synthetic sets — see Caveats).

**Structural axes (do not depend on the testbed):**

1. **Footprint (R8).** privacy-filter is **2.6 GB on disk vs GLiNER's 196 MB
   (~14×)**, and ~**3.8× slower** per OCR block (32 ms vs 8.5 ms; 29 vs 107
   blocks/sec). A `transformers`+`torch` backend also reopens the
   minos/PyInstaller bundling matrix that the shipped binary deliberately avoids.
   For a distributed CLI/app, this is close to decisive on its own.
2. **Native NER coverage gap (AE3).** privacy-filter's NER has **no SSN and no
   credit-card class** by construction (its taxonomy is fixed; confirmed by the
   coverage delta and by 0% NER recall on both). Swapping the NER backend means
   leaning entirely on the regex/secrets layer for two of the highest-harm types.
3. **Decoding integration cost.** The only pip-installable way to run the model
   (HF `pipeline`) applies **generic BIOES grouping, not the model's constrained
   Viterbi decoder** (no `opf` package exists), and does **not reproduce the
   published accuracy** in our harness. A faithful integration would have to port
   the Viterbi decoder — non-trivial.

**Accuracy axis (directional, points the same way):**

4. On clean Tier-1 prose (n=400) GLiNER **matches or beats** privacy-filter on
   person and email recall, and on a small synthetic OCR-noise set (n=25) GLiNER
   leads on partial recall and document-leak. privacy-filter never clears the AE1
   swap bar. The **real** ScreenCap Tier-2 set is too PII-sparse (2 in-scope
   instances) to settle recall on its own — hence "directional," and hence the
   weight on the structural axes above.

privacy-filter is a capable model with genuine strengths (better Tier-1 ADDRESS
recall; fewer false positives on noisy dev-screen OCR; broader native categories;
a `secret` class that *complements* — see Secrets below). This is a **fit**
decision for ScreenCap's current 6-type `EntityType` set, distribution, and
bundled-binary constraint, **not** a quality judgment on the model.

---

## Evidence

### Tier 1 — public benchmark, PII-Masking-300k (English validation, n=400)

Per-type **partial (leak-relevant) recall** — the fair headline for a redaction
backend, and the metric on which privacy-filter is not penalized by the
offset/decoding artifacts noted below:

| Type | GLiNER partial-R | privacy-filter partial-R | Winner |
|---|---|---|---|
| PERSON | **57%** | 47% | GLiNER |
| EMAIL | **100%** | 60% | GLiNER |
| PHONE | 95% | 90% | ~tie (GLiNER) |
| SSN | **54%** | **0%** (no class) | GLiNER |
| ADDRESS | 22% | **54%** | privacy-filter |
| CREDIT_CARD | — | — | not in dataset |

- privacy-filter does **not** match-or-beat GLiNER on person/email — the AE1 swap
  criterion — even on the clean distribution it was designed for.
- privacy-filter is **better on ADDRESS** (54% vs 22%): it natively handles the
  dataset's fine-grained address components better than GLiNER. A genuine strength.
- **CREDIT_CARD cannot be measured on Tier 1** — this dataset release has no
  credit-card class. Only Tier 2 exercises it.

### Tier-1 published-F1 reproduction (R3) — did NOT reproduce; cause understood

| Metric | GLiNER | privacy-filter | Published target |
|---|---|---|---|
| Binary PII-vs-O, char-level, **6-type collapse** | 0.66 | 0.56 | — |
| Binary PII-vs-O, char-level, **full taxonomy** | 0.51 | **0.64** | token 0.96 / exact-span 0.926 |

Neither setup reaches the model card's 0.96/0.926. This is **not a wiring bug** —
U1 confirmed offsets are byte-correct (0/14 mismatches) and per-type recall is
sensible. The gap is explained by:
- **HF `pipeline` ≠ Viterbi.** The model ships a constrained Viterbi decoder; the
  pipeline does generic argmax+BIOES grouping (it even warns: *"Tokenizer does not
  support real words, using fallback heuristic"*). privacy-filter's **exact-span
  recall collapsed to ~0.5% on Tier-1** — a boundary-degradation signature.
- **Leading-space offsets.** `aggregation_strategy="first"` emits spans that
  include the leading word-boundary space (`[10,16)=" Alice"`), so exact-boundary
  matches systematically fail while partial overlap succeeds. Harmless for
  redaction (it over-covers), but it makes exact-span metrics meaningless here.
- **Char-level + 6-type collapse** are stricter than the card's token-level metric
  over its own 8-label taxonomy.

In the **full-taxonomy** view privacy-filter (0.64) out-recalls GLiNER (0.51)
because it natively detects date/url/account-number/secret that GLiNER's 6-type
NER ignores. That breadth is real but **outside ScreenCap's current EntityType set**.

### Tier 2 — REAL ScreenCap OCR/AX (decisive distribution; `v15-pii-positive`)

The real recordings available are dev-screen captures with **very sparse high-harm
PII** — 2 genuine in-scope instances (1 PERSON — the repo owner's name; 1 EMAIL —
the owner's address) across 243 extracted blocks, plus 182 no-PII blocks (code,
file paths, timestamps, commit messages) used as **false-positive probes**. (AX
text across all recordings yielded 0 emails/phones/SSNs — a finding in itself
about the distribution.)

| Backend | PII recall (2 cases) | FP on 182 no-PII probes | Doc leak |
|---|---|---|---|
| GLiNER NER | 2/2 (partial) | 18 | 0% |
| privacy-filter NER | 2/2 (partial) | **8** | 0% |

- Both caught the real email and person. privacy-filter raised **fewer false
  positives** on noisy dev-screen OCR (8 vs 18) — a real precision strength.
- The real set is too thin on high-harm types to settle recall — hence the
  synthetic OCR-noise set below for per-type recall power.

### Tier 2 — SYNTHETIC OCR-noise (per-type recall power; 25 PII, OCR-styled)

Disclosed as **synthetic** (standard test SSN/card numbers, invented names) in
OCR-style noise modeled on the real extract (leading bullets, split tokens, missing
spaces). It exists because the real recordings lack high-harm PII density. This is
the per-type recall-under-noise read:

| Backend | Partial recall (all types) | SSN | CREDIT_CARD | Doc leak |
|---|---|---|---|---|
| **GLiNER NER** | **100%** | 100% | 100% | **0%** |
| privacy-filter NER | 69% | **0%** | **0%** | **32%** |
| privacy-filter ∪ regex/secrets | 85% | 67% | 67% | 16% |
| GLiNER full pipeline | 100% | 100% | 100% | 0% |

- GLiNER detects **every** high-harm type under OCR noise with zero leaks.
- privacy-filter's NER **leaks 1 in 3 PII-bearing blocks** here, driven by the
  SSN/credit-card gap and some phone/address misses.
- The regex/secrets layer recovers SSN/credit-card to **67% partial recall** —
  it **partially but not fully** compensates (AE3): GLiNER stays ahead at 100%.

### Footprint, latency, coverage delta (R8, R9)

| Axis | GLiNER | privacy-filter |
|---|---|---|
| Disk (HF cache) | **196 MB** | **2.6 GB** (safetensors; pipeline path) |
| RSS (load + warmup) | ~1.2 GB | ~1.3 GB (comparable — MoE keeps active params small) |
| CPU latency / OCR block | **8.5 ms** (p90 11.8) | 32.2 ms (p90 60.7) |
| Throughput | **107 blocks/s** | 29 blocks/s |
| Native NER → EntityType | PERSON, EMAIL, PHONE, **SSN**, **CREDIT_CARD**, ADDRESS | PERSON, EMAIL, PHONE, ADDRESS |
| Adds (no EntityType home) | — | url, date, account_number, secret |

- **RAM is a wash**; **disk is 14× worse** and is the bundled-binary cost that
  matters for a distributed CLI/app.
- The ONNX `q4f16` variant (~809 MB per the model card) is smaller, but the HF
  `pipeline` path cannot use ONNX without `optimum`+`onnxruntime` — which reopens
  the **minos 11 vs 14 contagion** documented in
  [`docs/solutions/build-errors/macos-pre14-binary-install-failure.md`](../solutions/build-errors/macos-pre14-binary-install-failure.md)
  and the frozen-binary smoke-test gauntlet in
  [`docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md`](../solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md).

### Secrets / API-keys — privacy-filter's `secret` class vs ScreenCap's detect-secrets layer

privacy-filter's one capability GLiNER's NER lacks is a native `secret` class. The
PII head-to-head leaves it out of scope; this axis scores it directly. A synthetic,
code-OCR-styled set of **14 secrets** (AWS keys, GitHub PAT, Stripe/Slack/Google/
OpenAI keys, JWT, RSA private-key header, connection string, npm token, base64
basic-auth, password) + **6 entropy-trap distractors** (git SHAs, UUIDs, hashes),
collapsed to a binary `SECRET` bucket:

| Backend | Partial recall | Precision | Doc leak |
|---|---|---|---|
| **ScreenCap regex + detect-secrets (current)** | **79%** (11/14) | 100% | 21% |
| privacy-filter `secret` NER | 50% (7/14) | 100% | 50% |
| GLiNER NER (baseline) | 0% (0/14) | — | 100% |

- **ScreenCap's existing secrets layer beats privacy-filter's native `secret` class**
  (79% vs 50%). privacy-filter's one NER edge over GLiNER does **not** improve on what
  ScreenCap already has — it is worse. This *reinforces* no-swap.
- Neither over-flags the entropy-trap distractors (both **100% precision** — no FPs on
  git SHAs / UUIDs / hashes).
- They are **complementary**, not redundant: privacy-filter caught 2 secrets
  detect-secrets missed (an unprefixed hex `api_key` and a base64 basic-auth token —
  via context), while detect-secrets caught the prefixed-token secrets (AWS `AKIA`,
  Slack `xoxb`, Google `AIza`, OpenAI `sk-proj`, connection strings) privacy-filter
  missed. Their **union recall is 93% (13/14)** vs 79% for detect-secrets alone.
- **Bearing on the verdict:** none on the NER-swap decision (no-swap holds). But it
  reframes the "privacy-filter adds a `secret` class" angle from a swap argument into
  an **augment** opportunity — see "When privacy-filter would be worth revisiting."

---

## Decision rules (origin acceptance examples)

- **AE1** (swap iff privacy-filter matches/beats GLiNER on person/email/phone recall
  **and** acceptable footprint/latency): **FAILS both halves.** GLiNER ≥ privacy-filter
  on person and email recall (Tier-1 and Tier-2), and privacy-filter is 14× the disk
  and ~3.8× the latency. → **no-swap.**
- **AE2** (a Tier-1 win that does not survive Tier 2 → no-swap, stated as such):
  privacy-filter **does not even win Tier 1** on the high-harm types, and on the
  synthetic OCR-noise set (n=25) loses on partial recall and document-leak
  (69% vs 100%, 32% vs 0%). The verdict does **not** lean on a public-benchmark
  result (there is no clean Tier-1 win to lean on), and treats the Tier-2 accuracy
  numbers as directional given the small/synthetic sets — the no-swap call is
  carried by the structural axes, not by the recall deltas.
- **AE3** (a currently-redacted type privacy-filter does not natively label →
  surface the regression): **SSN and credit-card** are exactly this. Surfaced
  explicitly, not hidden under aggregate F1: privacy-filter NER scores **0%** on
  both; the regex/secrets layer recovers them only to **67%** partial recall, below
  GLiNER's 100%. Swapping would trade a 100%-recall path for a 67%-recall one on
  two of the highest-harm types.

---

## When privacy-filter *would* be worth revisiting

This verdict is scoped to today's ScreenCap, and to **swapping the NER backend**.
Re-open the question if:
- ScreenCap wants **date / url / account-number** as first-class redaction types —
  privacy-filter detects these natively; GLiNER does not.
- The **bundled-binary constraint** changes (e.g., a server-side redaction tier
  where 2.6 GB and `transformers`+`torch` are acceptable).
- Someone ports the model's **Viterbi decoder** (or an official `opf`-equivalent
  ships) so its published accuracy is actually reachable, **and** the SSN/credit-card
  gap is closed (native classes or a validated regex/secrets equivalence).

Separately, **as an augment rather than a swap**: privacy-filter's `secret` class
caught 2 secrets ScreenCap's detect-secrets layer missed (lifting union secret
recall 79% → 93%). If secrets coverage becomes a priority, adding privacy-filter's
`secret` head *alongside* (not replacing) the existing secrets layer is a separate,
independently-evaluable enhancement — but it carries the same 2.6 GB / decoder cost,
so the bundled-binary math has to clear first.

## Privacy posture

privacy-filter runs **fully locally** (transformers/ONNX, CPU) — no captured text
leaves the machine, the **same trust posture as the current GLiNER pipeline**. The
swap question is purely fit/cost, not a privacy regression either way.

## Production-integration cost (if a future verdict flips positive)

A downstream integration ticket would need: a new backend in `PiiDetector`, an
`OPENAI_ENTITY_MAPPING` in `entity_mapping.py`, a `create_default_pipeline` branch,
`_VALID_PII_ENGINES` wiring, setup-wizard model download (2.6 GB), a ported Viterbi
decoder, leading-space offset trimming, an SSN/credit-card coverage plan, **and**
clearing the PyInstaller/minos smoke-test gauntlet. This is **not** small.

---

## Caveats & limitations (read before re-using these numbers)

- **Decoder:** all privacy-filter numbers use the HF `pipeline` generic-grouping
  decoder, **not** the model's constrained Viterbi decoder (no pip-installable
  `opf` CLI exists). Exact-span metrics are unreliable for privacy-filter here;
  **partial/leak-relevant recall is the fair metric** and is what the verdict uses.
- **Tier-2 real PII is sparse** (2 in-scope instances); the per-type Tier-2 recall
  read leans on the **synthetic** OCR-noise set, which is disclosed as synthetic.
  A human should reconcile the agent-drafted gold before treating any single Tier-2
  number as authoritative — though the verdict's direction is robust across all
  four axes regardless.
- **Tier-1 n=400** English validation holdout (the 300k release ships only
  train+validation); CREDIT_CARD absent from the dataset.
- The ai4privacy dataset is **eval-only** (academic license); no dataset rows are
  committed.

## Reproduce

See [`benchmarks/scr28/README.md`](../../benchmarks/scr28/README.md). Raw scored
tables: `benchmarks/scr28/results/SCORES.md` and `FOOTPRINT.md` (gitignored —
contain derived numbers over real/synthetic PII inputs).
