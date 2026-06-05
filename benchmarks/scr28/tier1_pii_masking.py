#!/usr/bin/env python3
"""Tier-1 orchestration: ``ai4privacy/pii-masking-300k`` -> gold cases + published-F1.

Tier 1 is the **fast common-ground read** and a wiring sanity check (R3): score
both backends on a public PII benchmark and reproduce privacy-filter's *baseline*
published F1. It is **not** the deciding input — Tier 2 (real ScreenCap OCR/AX
text) is (see U6). A Tier-1 win that does not survive Tier 2 is a no-swap (AE2).

Decoupled, like the rest of the harness
---------------------------------------
This module does **not** call a model. It has two jobs:

1. ``emit`` — load the dataset (English ``validation`` holdout; the 300k release
   ships only ``train`` + ``validation``, so the English-filtered ``validation``
   split *is* the held-out evaluation set) and write:
     * ``inputs.jsonl``  — ``{id, text}`` for the two ``run_*.py`` runners.
     * ``gold.jsonl``    — ``CorpusCase`` rows the existing ``scorer.py`` grades.
   Gold spans come from the dataset's ``privacy_mask`` (authoritative char
   offsets); each native class is folded onto ScreenCap's ``EntityType`` set via
   :data:`PII300K_TO_ENTITYTYPE`. Classes with no home are dropped from *both*
   sides and tallied so the drop is auditable, never silent.

2. ``published-f1`` — given a predictions JSON + the gold ``gold.jsonl``, compute
   a **binary PII-vs-O char-level** F1. This is the model-card-comparable headline
   (the card's 96% / 92.6% are a binary masking metric over the model's own
   taxonomy, *not* ScreenCap's 6-type collapse). Per-``EntityType`` P/R tables
   come from ``scorer.py`` as usual; this adds only the binary reproduction number.

Run order (see README.md): ``emit`` -> run both runners over ``inputs.jsonl`` ->
``scorer.py`` for per-entity tables -> ``published-f1`` for the reproduction check.

Dataset license: ``ai4privacy/pii-masking-300k`` is **not plainly permissive**
(academic use with citation; commercial use requires contacting ai4privacy).
Eval-only — the emitted ``inputs.jsonl`` / ``gold.jsonl`` carry dataset rows and
default to ``results/`` (gitignored). **Do not commit dataset rows.**
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

# Put this script's own dir on sys.path so the sibling flat modules import whether
# this runs as a script or as `benchmarks.scr28.tier1_pii_masking`. `_path_setup`
# then adds src/ + the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_setup  # noqa: F401,E402  (import for side effect: bootstraps sys.path)
from label_maps import OUT_OF_SCOPE, map_label  # noqa: E402
from schema import PredictionFile  # noqa: E402

from screencap.privacy import EntityType, normalize_text  # noqa: E402

if TYPE_CHECKING:
    from tests.privacy.fixtures.test_corpus import CorpusCase

DATASET = "ai4privacy/pii-masking-300k"
DEFAULT_SPLIT = "validation"  # the 300k release has only train + validation
DEFAULT_LANGUAGE = "English"

# ---------------------------------------------------------------------------
# Native class -> EntityType mapping
# ---------------------------------------------------------------------------
#
# The 300k English label vocabulary (27 classes, harvested from the dataset).
# Folded onto ScreenCap's canonical EntityType set so Tier 1 scores on the *same*
# entity definitions as GLiNER and privacy-filter (R4). Anything not listed here
# maps to None and is dropped with a logged tally.
#
# Shared types both NER backends can emit (the meaningful Tier-1 comparison):
#   names (GIVENNAME*/LASTNAME*) -> PERSON ; EMAIL -> EMAIL ; TEL -> PHONE ;
#   address components (STREET/BUILDING/SECADDRESS/CITY/STATE/POSTCODE/COUNTRY)
#       -> ADDRESS ; SOCIALNUMBER -> SSN
#
# Deliberate drops (-> OUT_OF_SCOPE), with reasons:
#   PASS (password)  : a secrets-layer concern, not NER — evaluated in Tier-2
#                      full-pipeline mode, not in this NER common-ground read.
#   PASSPORT/IDCARD/DRIVERLICENSE : no ScreenCap EntityType home (privacy-filter
#                      would tag these `account_number` -> OUT_OF_SCOPE too).
#   TITLE            : bare honorific (Mr./Dr.); not independently identifying and
#                      a boundary-noise source against single-PERSON spans.
#   USERNAME/IP/GEOCOORD/SEX/TIME/DATE/BOD : no EntityType home.
#
# NOTE: this release has **no CREDITCARDNUMBER class**, so Tier 1 cannot exercise
# CREDIT_CARD. Only Tier 2 can. The verdict must not read a Tier-1 silence on
# credit cards as evidence either way (surfaced for AE3).
#
# NOTE: address is fine-grained in gold (one span per component) but both models
# emit one coarse ADDRESS span. The scorer's overlap matching handles this — a
# single ADDRESS prediction overlapping several adjacent ADDRESS gold spans
# matches each — so folding all components to ADDRESS is the right call.
PII300K_TO_ENTITYTYPE: dict[str, str] = {
    # PERSON
    "GIVENNAME1": EntityType.PERSON,
    "GIVENNAME2": EntityType.PERSON,
    "LASTNAME1": EntityType.PERSON,
    "LASTNAME2": EntityType.PERSON,
    "LASTNAME3": EntityType.PERSON,
    # EMAIL / PHONE
    "EMAIL": EntityType.EMAIL,
    "TEL": EntityType.PHONE,
    # SSN
    "SOCIALNUMBER": EntityType.SSN,
    # ADDRESS (fine-grained components collapse to one type)
    "STREET": EntityType.ADDRESS,
    "BUILDING": EntityType.ADDRESS,
    "SECADDRESS": EntityType.ADDRESS,
    "CITY": EntityType.ADDRESS,
    "STATE": EntityType.ADDRESS,
    "POSTCODE": EntityType.ADDRESS,
    "COUNTRY": EntityType.ADDRESS,
    # Deliberate drops — no EntityType home / not an NER concern.
    "PASS": OUT_OF_SCOPE,
    "PASSPORT": OUT_OF_SCOPE,
    "IDCARD": OUT_OF_SCOPE,
    "DRIVERLICENSE": OUT_OF_SCOPE,
    "TITLE": OUT_OF_SCOPE,
    "USERNAME": OUT_OF_SCOPE,
    "IP": OUT_OF_SCOPE,
    "GEOCOORD": OUT_OF_SCOPE,
    "SEX": OUT_OF_SCOPE,
    "TIME": OUT_OF_SCOPE,
    "DATE": OUT_OF_SCOPE,
    "BOD": OUT_OF_SCOPE,
}


def map_pii300k_label(native: str) -> str | None:
    """ai4privacy native class -> EntityType / OUT_OF_SCOPE / None (unknown)."""
    return PII300K_TO_ENTITYTYPE.get(native)


# ---------------------------------------------------------------------------
# Dataset -> CorpusCase gold
# ---------------------------------------------------------------------------


def record_to_case(record: dict, idx: int) -> tuple["CorpusCase", Counter]:
    """Convert one dataset record to a ``CorpusCase`` (in-scope spans only).

    Gold entities use the ``privacy_mask`` entry's ``value`` as the expected
    substring — the corpus/scorer model is substring-based (it reconstructs
    offsets by locating the substring in ``normalize_text(text)``), so we never
    have to remap the dataset's source-text offsets through normalization.

    Returns ``(case, dropped_label_counter)``; the counter tallies native classes
    that mapped to None (unknown) or OUT_OF_SCOPE, for auditing.
    """
    from tests.privacy.fixtures.test_corpus import CorpusCase, ExpectedEntity

    text = record["source_text"]
    dropped: Counter = Counter()
    expected: list[ExpectedEntity] = []
    for mask in record.get("privacy_mask", []):
        native = mask["label"]
        value = mask["value"]
        mapped = map_pii300k_label(native)
        if mapped is None:
            dropped[f"UNKNOWN:{native}"] += 1
            continue
        if mapped == OUT_OF_SCOPE:
            dropped[native] += 1
            continue
        expected.append(
            ExpectedEntity(entity_type=mapped, substring=value, source=None)
        )
    case = CorpusCase(
        id=record.get("id") or f"tier1-{idx:06d}",
        description=f"tier1 | {DATASET} | {record.get('language', '')}",
        text=text,
        expected=expected,
        # A record whose every span dropped out of scope still has real text;
        # treat it as a no-PII (false-positive) case so model hits on it count as
        # FPs rather than being silently ignored.
        is_false_positive=not expected,
        frequency=None,
    )
    return case, dropped


def iter_pii300k_records(
    *, limit: int | None, language: str, split: str
) -> Iterator[dict]:
    """Yield English dataset records. Defers the heavy ``datasets`` import.

    Streams (``streaming=True``) so a capped run does not download all 300k rows.
    """
    from datasets import load_dataset  # heavy — imported lazily

    ds = load_dataset(DATASET, split=split, streaming=True)
    n = 0
    for record in ds:
        if language and record.get("language") != language:
            continue
        yield record
        n += 1
        if limit is not None and n >= limit:
            return


def load_pii300k_cases(
    *, limit: int | None, language: str = DEFAULT_LANGUAGE, split: str = DEFAULT_SPLIT
) -> tuple[list["CorpusCase"], Counter]:
    """Load the dataset into ``CorpusCase`` gold + an aggregate drop tally."""
    cases: list[CorpusCase] = []
    drop_tally: Counter = Counter()
    for idx, record in enumerate(iter_pii300k_records(limit=limit, language=language, split=split)):
        case, dropped = record_to_case(record, idx)
        cases.append(case)
        drop_tally.update(dropped)
    return cases, drop_tally


# ---------------------------------------------------------------------------
# Emit JSONL (inputs for runners, gold for scorer)
# ---------------------------------------------------------------------------


def write_inputs_jsonl(cases: list["CorpusCase"], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for c in cases:
            fh.write(json.dumps({"id": c.id, "text": c.text}) + "\n")


def write_gold_jsonl(cases: list["CorpusCase"], path: Path) -> None:
    """Write gold in the ``scorer.load_cases_jsonl`` schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for c in cases:
            fh.write(
                json.dumps(
                    {
                        "id": c.id,
                        "description": c.description,
                        "text": c.text,
                        "expected": [
                            {
                                "entity_type": e.entity_type,
                                "substring": e.substring,
                                "source": e.source,
                            }
                            for e in c.expected
                        ],
                        "is_false_positive": c.is_false_positive,
                        "frequency": c.frequency,
                    }
                )
                + "\n"
            )


# ---------------------------------------------------------------------------
# Binary PII-vs-O char-level F1 (published-F1 reproduction)
# ---------------------------------------------------------------------------

# All canonical EntityType string constants (PII + secrets) — a predicted/gold
# label counts as "PII" in the binary metric only if it is one of these.
_REAL_ENTITY_TYPES: frozenset[str] = frozenset(
    v for k, v in vars(EntityType).items() if not k.startswith("_") and isinstance(v, str)
)


def _char_mask_from_substrings(normalized: str, substrings: list[str]) -> set[int]:
    """Char indices in ``normalized`` covered by *any* occurrence of a substring.

    Marks **all** occurrences (a duplicated PII value is PII wherever it appears).
    """
    covered: set[int] = set()
    for sub in substrings:
        if not sub:
            continue
        start = normalized.find(sub)
        while start != -1:
            covered.update(range(start, start + len(sub)))
            start = normalized.find(sub, start + 1)
    return covered


def _char_mask_from_spans(span_ranges: list[tuple[int, int]], text_len: int) -> set[int]:
    covered: set[int] = set()
    for start, end in span_ranges:
        lo = max(0, start)
        hi = min(text_len, end)
        if hi > lo:
            covered.update(range(lo, hi))
    return covered


def binary_pii_f1(
    prediction_file: PredictionFile, cases: list["CorpusCase"]
) -> dict[str, float | int]:
    """Char-level binary PII-vs-O F1 — the model-card-comparable headline.

    Gold positives: chars covered by any in-scope (mapped to a real ``EntityType``)
    gold substring, located in ``normalize_text(case.text)``. Predicted positives:
    chars covered by any predicted span whose native label maps to a real
    ``EntityType`` (OUT_OF_SCOPE / unknown spans don't count as PII here). TP/FP/FN
    are summed over chars across all cases.

    A proxy for the card's token-level metric — char granularity, not subword —
    so expect to land *near*, not exactly on, the published numbers.
    """
    text_by_id = {c.id: normalize_text(c.text) for c in cases}
    gold_subs_by_id: dict[str, list[str]] = {}
    for c in cases:
        gold_subs_by_id[c.id] = [
            e.substring for e in c.expected if e.entity_type in _REAL_ENTITY_TYPES
        ]

    tp = fp = fn = 0
    model = prediction_file.model
    for case_pred in prediction_file.predictions:
        normalized = text_by_id.get(case_pred.case_id)
        if normalized is None:
            continue
        gold_chars = _char_mask_from_substrings(
            normalized, gold_subs_by_id.get(case_pred.case_id, [])
        )
        pred_ranges = [
            (s.start, s.end)
            for s in case_pred.spans
            if (m := map_label(model, s.label)) is not None and m != OUT_OF_SCOPE
        ]
        pred_chars = _char_mask_from_spans(pred_ranges, len(normalized))
        tp += len(gold_chars & pred_chars)
        fp += len(pred_chars - gold_chars)
        fn += len(gold_chars - pred_chars)

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp_chars": tp,
        "fp_chars": fp,
        "fn_chars": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_emit(args: argparse.Namespace) -> None:
    limit = None if args.limit < 0 else args.limit
    print(f"Loading {DATASET} (split={args.split}, language={args.language}, limit={limit})...")
    cases, drop_tally = load_pii300k_cases(
        limit=limit, language=args.language, split=args.split
    )
    out_dir = Path(args.out_dir)
    inputs_path = out_dir / "tier1-inputs.jsonl"
    gold_path = out_dir / "tier1-gold.jsonl"
    write_inputs_jsonl(cases, inputs_path)
    write_gold_jsonl(cases, gold_path)

    n_pii = sum(1 for c in cases if not c.is_false_positive)
    n_spans = sum(len(c.expected) for c in cases)
    print(f"Wrote {len(cases)} cases ({n_pii} with PII, {n_spans} gold spans).")
    print(f"  inputs -> {inputs_path}")
    print(f"  gold   -> {gold_path}")
    if drop_tally:
        print("\nDropped native classes (audit — not scored):")
        for label, n in drop_tally.most_common():
            print(f"  {label}: {n}")
    print(
        "\nNote: this release has no CREDITCARDNUMBER class — Tier 1 does not "
        "exercise CREDIT_CARD (only Tier 2 can)."
    )


def _cmd_published_f1(args: argparse.Namespace) -> None:
    from scorer import load_cases_jsonl  # local import: needs screencap

    prediction_file = PredictionFile.load(args.predictions)
    cases = load_cases_jsonl(Path(args.cases_jsonl))
    metrics = binary_pii_f1(prediction_file, cases)
    print(f"\n# Tier-1 published-F1 reproduction — model={prediction_file.model}")
    print("Binary PII-vs-O, char-level (proxy for the card's token-level headline)\n")
    print(f"  precision : {metrics['precision']:.4f}")
    print(f"  recall    : {metrics['recall']:.4f}")
    print(f"  f1        : {metrics['f1']:.4f}")
    print(
        f"  chars     : TP={metrics['tp_chars']} FP={metrics['fp_chars']} "
        f"FN={metrics['fn_chars']}"
    )
    if prediction_file.model == "privacy-filter":
        print(
            "\nBaseline target (model card §): token F1 ~0.96, exact-span ~0.926. "
            "The 'corrected' 0.974/0.942 variant is NOT a downloadable dataset and "
            "is NOT the target. A large gap here is a harness bug (offsets/label "
            "map), not a model verdict — fix before trusting the head-to-head."
        )
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(metrics, indent=2) + "\n")
        print(f"\nSaved -> {args.out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SCR-28 Tier-1 (PII-Masking-300k) orchestration")
    sub = parser.add_subparsers(dest="command", required=True)

    p_emit = sub.add_parser("emit", help="Load dataset -> inputs.jsonl + gold.jsonl")
    p_emit.add_argument("--limit", type=int, default=500, help="Max English records (-1 = all)")
    p_emit.add_argument("--language", default=DEFAULT_LANGUAGE)
    p_emit.add_argument("--split", default=DEFAULT_SPLIT, choices=["train", "validation"])
    p_emit.add_argument(
        "--out-dir",
        default="benchmarks/scr28/results",
        help="Where to write tier1-inputs.jsonl / tier1-gold.jsonl (gitignored)",
    )
    p_emit.set_defaults(func=_cmd_emit)

    p_f1 = sub.add_parser("published-f1", help="Binary PII-vs-O reproduction from predictions")
    p_f1.add_argument("--predictions", required=True, help="Predictions JSON (schema.PredictionFile)")
    p_f1.add_argument("--cases-jsonl", required=True, help="tier1-gold.jsonl")
    p_f1.add_argument("--out", help="Optional path to save metrics JSON")
    p_f1.set_defaults(func=_cmd_published_f1)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
