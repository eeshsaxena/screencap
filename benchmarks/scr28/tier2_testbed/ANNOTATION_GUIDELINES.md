# Tier-2 annotation guidelines (SCR-28)

These rules govern hand-labeling `gold.jsonl` from `inputs.jsonl` (produced by
[`extract.py`](extract.py)). **Read this before labeling.** The Tier-2 testbed is
the spike's *decisive* input, so the gold must be consistent and **blind to model
output**.

## Golden rule: label the text, not the model

Annotate what a human reading the block would call PII — **never** look at what
GLiNER or privacy-filter predicted first. If you label to match a model, the
head-to-head measures nothing. Label from `inputs.jsonl` alone.

## Canonical label set

Use exactly these `EntityType` strings (the spike scores PII; secrets are out of
scope for Tier-2 — see the plan's Scope Boundaries):

| Label | What it covers | Notes |
|-------|----------------|-------|
| `PERSON` | A person's name | First + last name = **one** span (see boundaries). |
| `EMAIL` | An email address | Full `local@domain.tld`. |
| `PHONE` | A phone number | Include country code + separators. |
| `ADDRESS` | A physical/street address or locality | City/country count. |
| `SSN` | A US Social Security Number | privacy-filter has **no** native SSN class — it relies on the regex layer (full-pipeline mode). Label them so the gap is measurable. |
| `CREDIT_CARD` | A payment card number | Same caveat as SSN. |

Anything else (URLs, dates, account numbers, usernames, secrets/keys) — **do not
label**. privacy-filter emits some of these natively, but they have no current
`EntityType` home and are excluded from the shared-type head-to-head (the scorer
maps them to `OUT_OF_SCOPE`). Coverage of those extras is handled separately in
U7's coverage-delta table, not here.

## Span boundary rules

Offsets are **half-open `[start, end)`** char indices into the block `text` *as it
appears in `inputs.jsonl`* (already `normalize_text`-d at extraction). The
`substring` you record must be the exact text at those offsets.

1. **Names: first + last = one span.** `"John Doe"` is a single `PERSON`, not
   `"John"` + `"Doe"`. (The scorer merges split model spans to match this.)
2. **No trailing punctuation/whitespace.** `"jane@x.com,"` → label `"jane@x.com"`.
3. **Emails/phones are whole.** Include the full address / the full dial string
   with country code and separators (`"+1 415 555 1212"`).
4. **Addresses as one span** covering the contiguous address text in the block.
5. **OCR noise stays as-is.** If OCR mangled a character inside an entity, label
   the mangled text that's actually present — do not "correct" it. The point is
   to measure detection on real noisy input.
6. **Per block only.** Never label across blocks; each line is independent (the
   scrubber detects per block).

## No-PII blocks are false-positive cases

A block with **no** PII must be recorded with `"is_false_positive": true` and an
empty `expected`. This makes any model prediction on it count as a false positive —
essential for measuring precision on noisy UI chrome (menus, build output, code).

## `gold.jsonl` format

One JSON object per line:

```json
{"id": "rec-x-ocr-0007", "description": "ocr | menu bar", "text": "Email John Doe at john@x.com", "expected": [{"entity_type": "PERSON", "substring": "John Doe", "source": null}, {"entity_type": "EMAIL", "substring": "john@x.com", "source": null}], "is_false_positive": false, "frequency": null}
{"id": "rec-x-ocr-0008", "description": "ocr | build output", "text": "Compiling module foo (3 warnings)", "expected": [], "is_false_positive": true, "frequency": "high"}
```

- Keep the `id` from `inputs.jsonl` so provenance is traceable.
- `source` inside `expected` is usually `null` (any detector may match).
- `frequency` (`"high"`/`"medium"`/`"low"`) is optional; it weights FP impact for
  noisy high-frequency blocks. Set it for FP cases when you can estimate it.
- See [`gold.example.jsonl`](gold.example.jsonl) for a synthetic, committable example.

## Coverage targets (directional)

The user sets what "enough" means, but aim for a credible read:

- **Stratify** by modality (`ocr` / `ax`) and by entity type.
- Target **≥30–50 instances per high-priority type** (PERSON, EMAIL, PHONE).
- Include SSN/CREDIT_CARD where they occur, even if sparse — AE3 depends on them.

## Double-annotation + agreement

1. Pick a **50–100-item subset** spanning both modalities and the main types.
2. Have **two annotators label it independently** (neither sees the other's labels
   or any model output).
3. Compute and **record agreement** (e.g. span-level exact-match rate, or
   Cohen's κ on the PII-vs-O per-token roll-up) in `gold.jsonl`'s companion notes
   or the verdict doc.
4. **Reconcile** disagreements into the final gold; document systematic ones
   (e.g. "annotator A included honorifics in PERSON; resolved to exclude").

Treat the result as **directional** — small + single-team — and report per-type
support `n` so readers can weigh sparse types appropriately.
