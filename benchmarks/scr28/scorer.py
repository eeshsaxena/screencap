#!/usr/bin/env python3
"""SCR-28 spike scorer: grade a model's predictions JSON against gold cases.

The scorer never imports a model. It loads a :class:`PredictionFile` (emitted by
``run_gliner.py`` or ``run_privacy_filter.py``), maps each native label to an
``EntityType`` via :mod:`label_maps`, merges adjacent same-type spans (Presidio
``SpanEvaluator`` semantics), and grades them through the reused core
``score_predictions`` from ``scoring_core.py``. This gives both
models identical matching + per-type aggregation + Markdown/JSON output.

Usage::

    # score one model's predictions against a Tier-2 gold JSONL
    python benchmarks/scr28/scorer.py \
        --predictions results/tier2-gliner.json \
        --cases-jsonl tier2_testbed/gold.jsonl \
        --out results/tier2-gliner-scored.json

    # smoke check against the built-in synthetic corpus
    python benchmarks/scr28/scorer.py --predictions preds.json --cases corpus
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Put this script's own dir on sys.path so the sibling flat modules (`_path_setup`
# et al.) import whether this runs as a script or as `benchmarks.scr28.scorer`.
# `_path_setup` then adds src/ + the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_setup  # noqa: F401,E402  (import for side effect: bootstraps sys.path)
from label_maps import OUT_OF_SCOPE, map_label  # noqa: E402
from schema import CasePrediction, PredictionFile  # noqa: E402
from scoring_core import (  # noqa: E402
    AggregateResult,
    print_benchmark_table,
    save_benchmark_json,
    score_predictions,
)

from screencap.privacy import Detection, DetectionResult, normalize_text  # noqa: E402
from tests.privacy.fixtures.test_corpus import (  # noqa: E402
    FALSE_POSITIVE_CASES,
    TRUE_POSITIVE_CASES,
    CorpusCase,
    ExpectedEntity,
)


def split_cases(cases: list[CorpusCase]) -> tuple[list[CorpusCase], list[CorpusCase]]:
    """Split cases into (true-positive, false-positive) like the corpus does.

    A no-PII block must be marked ``is_false_positive=True`` so any prediction on
    it counts as an FP. Non-FP cases with no expected entities are excluded from
    both (they would neither contribute a gold span nor an FP), matching
    ``run_benchmark``'s corpus convention.
    """
    tp = [c for c in cases if not c.is_false_positive and c.expected]
    fp = [c for c in cases if c.is_false_positive]
    return tp, fp


def _merge_adjacent_same_type(
    detections: list[Detection], text: str
) -> list[Detection]:
    """Merge same-type spans separated only by whitespace (or overlapping).

    Handles a name emitted as ``first name`` + ``last name`` (two PERSON spans)
    matching a single gold PERSON span. Different types, or spans separated by
    non-whitespace text, are never merged.

    Offsets are validated before merging: a pair is only considered mergeable
    when both spans lie within ``text`` and are ordered
    (``0 <= last.end <= det.start <= len(text)`` for the gap case, with
    ``det.end <= len(text)``). Out-of-range or negative offsets are rejected
    (left unmerged) rather than relying on Python's clamping slice semantics,
    which would otherwise let a bogus span swallow its neighbour.
    """
    if not detections:
        return []
    n = len(text)
    ordered = sorted(detections, key=lambda d: (d.start, d.end))
    merged: list[Detection] = [ordered[0]]
    for det in ordered[1:]:
        last = merged[-1]
        same_type = det.entity_type == last.entity_type
        # Both spans must be in-range for the gap/overlap reasoning below to be
        # meaningful; reject anything negative or past end-of-text.
        in_range = (
            0 <= last.start <= last.end <= n
            and 0 <= det.start <= det.end <= n
        )
        mergeable = False
        if same_type and in_range:
            if det.start <= last.end:
                # Touching or overlapping (offsets already validated in-range).
                mergeable = True
            else:
                # Separated: merge only when the gap is whitespace-only.
                gap = text[last.end : det.start]
                mergeable = gap == "" or gap.isspace()
        if mergeable:
            merged[-1] = Detection(
                entity_type=last.entity_type,
                start=last.start,
                end=max(last.end, det.end),
                score=max(last.score, det.score),
                source=last.source,
            )
        else:
            merged.append(det)
    return merged


def case_prediction_to_detections(
    case_pred: CasePrediction, model: str, normalized_text: str
) -> list[Detection]:
    """Map a case's predicted spans to scorable ``Detection``s.

    Native labels are mapped via :func:`label_maps.map_label`; ``None`` (unknown)
    and ``OUT_OF_SCOPE`` (known-but-unscored, e.g. ``private_url``) spans are
    dropped from scoring. Survivors are merged by type.
    """
    dets: list[Detection] = []
    for span in case_pred.spans:
        mapped = map_label(model, span.label)
        if mapped is None or mapped == OUT_OF_SCOPE:
            continue
        dets.append(
            Detection(
                entity_type=mapped,
                start=span.start,
                end=span.end,
                score=span.score,
                source=span.source or f"pii-{model}",
            )
        )
    return _merge_adjacent_same_type(dets, normalized_text)


def build_predictions_by_case(
    prediction_file: PredictionFile, cases: list[CorpusCase]
) -> dict[str, DetectionResult]:
    """Build the ``{case_id: DetectionResult}`` map ``score_predictions`` wants.

    The normalized text per case is reconstructed from the gold case text via
    ``normalize_text`` — the same transform the runner applied before inference,
    so the predicted offsets line up.
    """
    text_by_id = {c.id: normalize_text(c.text) for c in cases}
    out: dict[str, DetectionResult] = {}
    for case_pred in prediction_file.predictions:
        normalized = text_by_id.get(case_pred.case_id)
        if normalized is None:
            # Prediction for a case not in the gold set — skip; the gold set is
            # authoritative for what gets scored.
            continue
        if case_pred.case_id in out:
            # Two prediction entries for the same case would silently overwrite
            # each other (losing the first model's spans). The prediction file
            # is malformed — fail loudly rather than score a partial result.
            raise ValueError(
                f"duplicate case_id {case_pred.case_id!r} in prediction file"
            )
        out[case_pred.case_id] = DetectionResult(
            normalized_text=normalized,
            detections=case_prediction_to_detections(
                case_pred, prediction_file.model, normalized
            ),
        )
    return out


def score_prediction_file(
    prediction_file: PredictionFile, cases: list[CorpusCase]
) -> AggregateResult:
    """Score one model's predictions against gold ``cases``."""
    tp_cases, fp_cases = split_cases(cases)
    predictions_by_case = build_predictions_by_case(prediction_file, cases)
    return score_predictions(tp_cases, fp_cases, predictions_by_case)


# ---------------------------------------------------------------------------
# Full-pipeline union (U6): privacy-filter NER ∪ GLiNER regex/secrets
# ---------------------------------------------------------------------------


def remap_file_to_entitytype(prediction_file: PredictionFile) -> PredictionFile:
    """Pre-map a file's native labels to ``EntityType``, dropping OUT_OF_SCOPE/None.

    Returns a new ``PredictionFile`` whose spans carry ``EntityType`` constants as
    labels and ``model="gliner"`` — so a later union of files from *different* label
    vocabularies (privacy-filter native vs GLiNER's already-``EntityType`` regex/
    secrets) can be scored uniformly via the identity-validating GLiNER mapping.
    Without this, scoring a mixed-vocabulary union under one ``model`` would drop
    the other vocabulary's spans (e.g. a regex ``SSN`` span dropped as an unknown
    privacy-filter label).
    """
    from schema import CasePrediction, PredictedSpan

    out: list[CasePrediction] = []
    for cp in prediction_file.predictions:
        spans = []
        for s in cp.spans:
            mapped = map_label(prediction_file.model, s.label)
            if mapped is None or mapped == OUT_OF_SCOPE:
                continue
            spans.append(PredictedSpan(s.start, s.end, mapped, s.score, s.source))
        out.append(CasePrediction(cp.case_id, spans))
    return PredictionFile(model="gliner", tier=prediction_file.tier, predictions=out)


def union_prediction_files(files: list[PredictionFile]) -> PredictionFile:
    """Union spans per ``case_id`` across files (each already ``EntityType``-mapped).

    Pass files through :func:`remap_file_to_entitytype` first so every span speaks
    the ``EntityType`` vocabulary. The result is ``model="gliner"`` (identity
    validation). The scorer's adjacent-same-type merge runs at scoring time, so
    overlapping NER/regex spans for the same type collapse there.
    """
    from schema import CasePrediction

    by_case: dict[str, list] = {}
    order: list[str] = []
    tier = files[0].tier if files else "smoke"
    for pf in files:
        for cp in pf.predictions:
            if cp.case_id not in by_case:
                by_case[cp.case_id] = []
                order.append(cp.case_id)
            by_case[cp.case_id].extend(cp.spans)
    return PredictionFile(
        model="gliner",
        tier=tier,
        predictions=[CasePrediction(cid, by_case[cid]) for cid in order],
    )


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def load_cases_jsonl(path: Path) -> list[CorpusCase]:
    """Load gold cases from a JSONL file (Tier-2 ``gold.jsonl`` format).

    Each line is a JSON object::

        {"id": "...", "description": "...", "text": "...",
         "expected": [{"entity_type": "PERSON", "substring": "...",
                       "source": null}],
         "is_false_positive": false, "frequency": null}
    """
    cases: list[CorpusCase] = []
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            expected = [
                ExpectedEntity(
                    entity_type=e["entity_type"],
                    substring=e["substring"],
                    source=e.get("source"),
                )
                for e in d.get("expected", [])
            ]
            case = CorpusCase(
                id=d["id"],
                description=d.get("description", ""),
                text=d["text"],
                expected=expected,
                is_false_positive=d.get("is_false_positive", False),
                frequency=d.get("frequency"),
            )
            # The scorer reconstructs each case's normalized text via
            # normalize_text (the same transform the extractor/runner applied).
            # If a gold substring isn't found there, the offsets are misaligned —
            # warn loudly so the misalignment surfaces instead of silently
            # scoring it as a miss.
            normalized = normalize_text(case.text)
            for exp in expected:
                if exp.substring and exp.substring not in normalized:
                    print(
                        f"[warn] gold case {case.id!r}: expected substring "
                        f"{exp.substring!r} ({exp.entity_type}) not found in "
                        f"normalize_text(text) — gold/text misalignment.",
                        file=sys.stderr,
                    )
            cases.append(case)
    return cases


def load_corpus_cases() -> list[CorpusCase]:
    """The built-in synthetic corpus (TP + FP) — for smoke checks only."""
    return [*TRUE_POSITIVE_CASES, *FALSE_POSITIVE_CASES]


def _load_cases(args: argparse.Namespace) -> list[CorpusCase]:
    if args.cases_jsonl:
        return load_cases_jsonl(Path(args.cases_jsonl))
    if args.cases == "corpus":
        return load_corpus_cases()
    raise SystemExit("Provide --cases-jsonl <path> or --cases corpus")


def main() -> None:
    parser = argparse.ArgumentParser(description="SCR-28 spike scorer")
    parser.add_argument(
        "--predictions",
        required=True,
        help="Path to a predictions JSON (schema.PredictionFile)",
    )
    parser.add_argument(
        "--cases-jsonl",
        help="Path to a gold JSONL file (Tier-2 gold.jsonl format)",
    )
    parser.add_argument(
        "--cases",
        choices=["corpus"],
        help="Built-in case source (currently only the synthetic 'corpus')",
    )
    parser.add_argument("--out", help="Optional path to save the scored AggregateResult JSON")
    args = parser.parse_args()

    prediction_file = PredictionFile.load(args.predictions)
    cases = _load_cases(args)
    result = score_prediction_file(prediction_file, cases)

    print(f"\n# SCR-28 scorer — model={prediction_file.model} tier={prediction_file.tier}")
    print_benchmark_table(result)

    if args.out:
        save_benchmark_json(result, Path(args.out))
        print(f"\nScored results saved to {args.out}")


if __name__ == "__main__":
    main()
