# U1 — privacy-filter loading & decoding validation

**Date:** 2026-06-05 · **Env:** `.venv-pf` · **transformers 5.6.2 / torch 2.12.0 / datasets 4.8.5**
· **device:** CPU · model: `openai/privacy-filter` (loads without `trust_remote_code`).

Re-run: `PYTHONPATH=src benchmarks/scr28/.venv-pf/bin/python benchmarks/scr28/_validate_decoding.py`

## Result: offsets correct, `aggregation_strategy="first"` confirmed

- **0 / 14 offset mismatches** — every emitted `[start, end)` slices the reported
  surface text out of the input. The char-offset contract the scorer relies on holds.
- **`"first"` beats `"simple"`.** With `"first"`, the email decodes as one span
  (`john.doe@example.com`) and the phone as one (`+1 415 555 1212`). With `"simple"`,
  the email fragments into `john.doe@example` + `.com` and the phone into
  `+1 415 555 121` + `2`. The runner uses `"first"`.
- **Native labels observed:** `private_person`, `private_email`, `private_phone`,
  `secret` — a subset of the 8-label taxonomy, matching `label_maps.OPENAI_ENTITY_MAPPING`.
- **8-label taxonomy confirmed** from the model card: `account_number`, `private_address`,
  `private_email`, `private_person`, `private_phone`, `private_url`, `private_date`,
  `secret` — `label_maps.py` is correct.

## Caveats discovered (carried into the verdict)

1. **No `opf` CLI exists.** The plan's authoritative cross-check decoder
   (`pip install opf` / `opf --format json`) is **not a real package**; the model card
   ships only the `pipeline` API. Validation was done against the pipeline directly
   (offset-correctness on a fixed sample set) rather than against `opf`.
2. **HF `pipeline` ≠ Viterbi.** The pipeline does generic BIOES grouping over per-token
   argmax, not the model's constrained Viterbi decoder. It emits the warning
   *"Tokenizer does not support real words, using fallback heuristic"*. This degrades
   boundary coherence — privacy-filter's **exact-span** recall collapsed to ~0.5% on
   Tier-1 while **partial** recall stayed reasonable.
3. **Leading-space offsets.** `"first"` spans include the leading word-boundary space
   (`[10,16) = " Alice"`). Harmless for redaction (over-covers by one space) but makes
   exact-boundary matching against gold systematically fail. → **partial/leak-relevant
   recall is the fair metric**, used throughout the verdict.
4. **`screencap` metadata in `.venv-pf`.** `from screencap.privacy import normalize_text`
   raised `PackageNotFoundError` until `pip install -e . --no-deps` registered the dist
   metadata (`src/screencap/__init__.py` calls `version("screencap")` at import). The
   `--no-deps` flag avoids pulling the conflicting `transformers` 4.57.6.
5. **GLiNER + privacy-filter cannot share one env** (`transformers` 4.57.6 vs 5.6.2) —
   moot by design: the JSON-decoupled runners never coexist in one process.

## Sample decode (`aggregation_strategy="first"`)

```
name     private_person   [10,16) " Alice"  [16,22) " Smith"   score 1.000  (two sub-spans -> scorer merges)
email    private_email    [11,32) " john.doe@example.com"      score 1.000
phone    private_phone    [4,20)  " +1 415 555 1212"           score 1.000
address  (no spans)   <- under-detected here; see Tier-1 ADDRESS 54% partial recall for the real picture
date     (no spans)   <- private_date is OUT_OF_SCOPE for ScreenCap anyway
secret   secret           [8,51) " AWS_SECRET_ACCESS_KEY=AKIA…"  score 0.955  (OUT_OF_SCOPE)
fp-build (no spans)   <- correctly did not hallucinate PII on "Compiling module foo"
```
