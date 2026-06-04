#!/usr/bin/env python3
"""GLiNER runner (repo env) -> predictions JSON for the SCR-28 spike.

Runs ScreenCap's production GLiNER path over a list of input texts and emits
predicted spans in the common :mod:`schema`. Three modes:

* ``full`` — ``create_default_pipeline(pii_engine="presidio-gliner")``: GLiNER
  NER unioned with regex + secrets, resolved + heuristic-filtered. The "full
  pipeline" GLiNER baseline (U6).
* ``ner`` — ``PiiDetector(ner_backend="gliner")`` only: the NER backend in
  isolation, for the NER-only head-to-head against privacy-filter (U6).
* ``regex-secrets`` — RegexDetector + DetectSecretsDetector only, **model-
  agnostic**. Emitted separately so the privacy-filter side can build the
  full-pipeline union (privacy-filter NER ∪ regex/secrets) the scorer resolves.

The pipeline normalizes input with ``normalize_text`` internally and returns
offsets into that normalized text — the same transform the scorer reconstructs
per case, so offsets align. Heavy detector imports are deferred into the build
function to keep ``--help`` fast.

Run in the repo venv (transformers 4.57.6 + fast-gliner ONNX). See README.md.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

# Put this script's own dir on sys.path so the sibling flat modules (`_path_setup`
# et al.) import whether this runs as a script or as `benchmarks.scr28.run_gliner`.
# `_path_setup` then adds src/ + the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_setup  # noqa: F401,E402  (import for side effect: bootstraps sys.path)
from io_utils import load_input_texts  # noqa: E402
from schema import CasePrediction, PredictedSpan, PredictionFile  # noqa: E402

if TYPE_CHECKING:
    from screencap.privacy import Detection

MODES = ("full", "ner", "regex-secrets")


def _build_detect(mode: str) -> Callable[[str], tuple[str, list["Detection"]]]:
    """Return a ``detect(text) -> (normalized_text, list[Detection])`` callable.

    Heavy imports are deferred here so importing this module (e.g. for unit
    tests of the emit logic) stays cheap.
    """
    from screencap.privacy import normalize_text

    if mode == "full":
        from screencap.privacy import create_default_pipeline

        pipeline = create_default_pipeline(pii_engine="presidio-gliner")

        def detect(text: str):
            result = pipeline.detect(text)
            return result.normalized_text, result.detections

        return detect

    if mode == "ner":
        from screencap.privacy.pii import PiiDetector

        detector = PiiDetector(ner_backend="gliner")

        def detect(text: str):
            normalized = normalize_text(text)
            return normalized, detector.detect(normalized)

        return detect

    if mode == "regex-secrets":
        from screencap.privacy.regex import RegexDetector
        from screencap.privacy.secrets import DetectSecretsDetector

        detectors = [RegexDetector(), DetectSecretsDetector()]

        def detect(text: str):
            normalized = normalize_text(text)
            dets = []
            for d in detectors:
                dets.extend(d.detect(normalized))
            return normalized, dets

        return detect

    raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")


def detections_to_spans(detections: list["Detection"]) -> list[PredictedSpan]:
    """Convert ``screencap.privacy.Detection``s to schema ``PredictedSpan``s.

    GLiNER's production path already emits ``EntityType`` constants as the
    ``entity_type`` — these become the span's native label, so the scorer's
    GLiNER mapping is an identity-with-validation. ``source`` is preserved so
    the full-pipeline union can attribute spans (ner / regex / secrets).
    """
    return [
        PredictedSpan(
            start=d.start, end=d.end, label=d.entity_type, score=d.score, source=d.source
        )
        for d in detections
    ]


def run(inputs: list[tuple[str, str]], mode: str, tier: str) -> PredictionFile:
    detect = _build_detect(mode)
    predictions: list[CasePrediction] = []
    for case_id, text in inputs:
        _normalized, detections = detect(text)
        predictions.append(CasePrediction(case_id, detections_to_spans(detections)))
    return PredictionFile(model="gliner", tier=tier, predictions=predictions)


def main() -> None:
    parser = argparse.ArgumentParser(description="SCR-28 GLiNER runner")
    parser.add_argument("--mode", choices=MODES, default="full")
    parser.add_argument(
        "--inputs-jsonl",
        required=True,
        help="JSONL with {id|case_id, text} (inputs.jsonl or gold.jsonl)",
    )
    parser.add_argument("--tier", choices=["tier1", "tier2", "smoke"], default="smoke")
    parser.add_argument("--out", required=True, help="Output predictions JSON path")
    args = parser.parse_args()

    inputs = load_input_texts(args.inputs_jsonl)
    print(f"Running GLiNER mode={args.mode} over {len(inputs)} inputs...")
    prediction_file = run(inputs, args.mode, args.tier)
    prediction_file.save(args.out)
    n_spans = sum(len(p.spans) for p in prediction_file.predictions)
    print(f"Emitted {n_spans} spans across {len(inputs)} cases -> {args.out}")


if __name__ == "__main__":
    main()
