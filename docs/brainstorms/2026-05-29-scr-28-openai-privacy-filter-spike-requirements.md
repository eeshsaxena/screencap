---
date: 2026-05-29
topic: scr-28-openai-privacy-filter-spike
---

# SCR-28: Spike — `openai/privacy-filter` as Default Text-PII Backend

## Summary

A spike to decide one thing: should `openai/privacy-filter` replace GLiNER as ScreenCap's default text-PII detection backend? The deliverable is a swap / no-swap verdict backed by a two-tier evaluation — a public benchmark for a fast common-ground read, then real ScreenCap OCR + accessibility text as the deciding tier. No code integration; just the evidence and the call.

---

## Problem Frame

ScreenCap already runs a mature text-PII pipeline: `PiiDetector` ([src/screencap/privacy/pii.py](src/screencap/privacy/pii.py)) wraps Presidio with GLiNER (`knowledgator/gliner-pii-base-v1.0`) as the default NER backend, fed by Apple Vision OCR over screenshots plus accessibility-tree strings, with a separate secrets/regex layer alongside.

In April 2026 OpenAI released `openai/privacy-filter` (Apache 2.0, now in HF `transformers`): a text-only token-classification model for PII detection — 1.5B total / 50M active params, CPU-capable, 128k context, reporting F1 ~96–97% on the PII-Masking-300k benchmark, covering `private_person`, `private_address`, `private_email`, `private_phone`, `private_url`, `private_date`, `account_number`, `secret`.

This model occupies exactly the seat GLiNER does — the text-detection stage after OCR — so it's a candidate to swap into a single backend. There is **no observed deficiency** in the current GLiNER backend; the question is purely whether a newer model is meaningfully better for ScreenCap's input. The risk in answering carelessly is that the headline F1 is measured on clean, synthetic prose, while ScreenCap's actual input is noisy, fragmentary Vision-OCR output and accessibility strings — a distribution where a model that wins on prose can lose, and vice-versa.

---

## Requirements

**Evaluation scope & verdict**
- R1. Produce a documented **swap / no-swap recommendation** on replacing GLiNER with `openai/privacy-filter` as the default text-PII detection backend, with the supporting evidence attached.
- R2. State explicitly that the spike is **exploratory** — no known regression in the current backend motivated it — so readers do not infer a deficiency that isn't there.

**Tier 1 — public benchmark (fast common-ground read)**
- R3. Score both GLiNER and `openai/privacy-filter` on a public PII benchmark — **PII-Masking-300k**, preferably the corrected variant ([arXiv 2504.12308](https://arxiv.org/pdf/2504.12308)) — including reproducing privacy-filter's published F1 as a sanity check.
- R4. Run both models through a **single scoring harness** (Presidio's evaluation framework, already a project dependency) with detections mapped through ScreenCap's existing `EntityType` set, so the comparison is on identical entity definitions rather than each source's native metric.

**Tier 2 — real ScreenCap text (decisive)**
- R5. Assemble a small hand-labeled testbed drawn from **real ScreenCap recordings** — Apple Vision OCR output plus accessibility-tree strings — with known PII annotated.
- R6. Run both models **head-to-head on the Tier-2 testbed**; this tier is the deciding input to the verdict.
- R7. Report **per-entity precision/recall** (not just aggregate F1), so under-detection of specific high-priority types (names, emails, phones) is visible rather than averaged away.

**Comparison axes beyond accuracy**
- R8. Measure and report **latency/throughput and model footprint** (download size + resident memory) for each backend, since ScreenCap ships as a distributed CLI and privacy-filter is materially larger on disk than GLiNER's base model.
- R9. Document the **entity-coverage delta**: privacy-filter natively adds URL / date / account_number / secret and lacks explicit SSN / credit-card classes that the current mapping ([src/screencap/privacy/entity_mapping.py](src/screencap/privacy/entity_mapping.py)) uses — and how each maps onto, or falls outside, ScreenCap's `EntityType` set.

---

## Acceptance Examples

- AE1. **Covers R1, R6, R8.** Given Tier-2 results, when privacy-filter matches-or-beats GLiNER on per-entity recall for high-priority types **and** its latency/footprint are acceptable for the CLI, the spike recommends the swap.
- AE2. **Covers R1, R3, R6.** When privacy-filter wins Tier 1 but loses or ties on the Tier-2 OCR/AX testbed, the verdict is **no-swap** (or swap-blocked), and the doc says so explicitly rather than leaning on the public-benchmark win.
- AE3. **Covers R7, R9.** Given an entity type ScreenCap currently redacts (e.g., SSN) that privacy-filter does not natively label, when reporting results, the verdict surfaces the regression risk rather than hiding it under aggregate F1.

---

## Success Criteria

- A reader can tell from the spike output **whether to swap, and why**, without re-running the experiment.
- The verdict rests on ScreenCap's **real OCR/accessibility distribution**, not just a public benchmark number.
- If the verdict is positive, a downstream implementer gets the **entity-mapping gaps and footprint numbers** they'd need to scope the integration ticket.

---

## Scope Boundaries

- **No production integration** — wiring privacy-filter into the backend, config surface, or model packaging is a follow-up ticket if the verdict is positive.
- **Text-only** — no image/video/audio modality work; privacy-filter only ever slots at the post-OCR text stage.
- **No consolidation of the secrets-detection layer** via privacy-filter's `secret` class — interesting, but a separate question from the GLiNER swap.
- **No fine-tuning** of privacy-filter on ScreenCap data (its static-label limitation is noted, not acted on).
- The legacy **spaCy** backend is excluded from the comparison.
- Not a redesign of the broader redaction pipeline.

---

## Key Decisions

- **Two-tier evaluation, Tier 2 decisive.** Public benchmarks measure clean prose; ScreenCap's input is noisy OCR/AX text, so the swap call hinges on the real-distribution tier.
- **Single Presidio harness for both models.** Eliminates the apples-to-oranges trap of comparing two papers' differently-computed F1 scores.
- **Exploratory framing recorded.** No known GLiNER deficiency drives this; documenting that prevents a false-regression read downstream.
- **Verdict weighs footprint/latency and coverage delta, not F1 alone.** CLI distribution size and entity parity (SSN/credit-card gap) are first-class to a real swap decision.

---

## Dependencies / Assumptions

- `openai/privacy-filter` is loadable via HF `transformers` (`model_doc/openai_privacy_filter`) on CPU as documented.
- Presidio's evaluation harness can wrap a token-classification model like privacy-filter — possibly via a thin recognizer/adapter (flagged below as a planning question).
- **Decided (defer to execution):** no labeled Tier-2 testbed is assumed to exist; whoever runs the spike builds a minimal one from a handful of real recordings. Exact recording count / PII coverage is set at execution time.

---

## Outstanding Questions

### Deferred to Execution

- [Affects R5][User decision] How many recordings / what PII coverage counts as "enough" for a credible Tier-2 read — set once the testbed is being assembled.
- [Affects R1, R6][User decision] The exact "clear win" margin that flips the default. Suggested default: swap only if privacy-filter matches-or-beats GLiNER on recall for person/email/phone, with no material footprint/latency regression and no currently-redacted entity type lost.

### Deferred to Planning

- [Affects R4][Technical] Does Presidio's harness ingest privacy-filter directly, or does it need a custom recognizer/adapter?
- [Affects R6, R7][Needs research] Best way to align privacy-filter's BIOES token spans with ScreenCap's char-bbox OCR model when scoring on OCR text.
