#!/usr/bin/env python3
"""openai/privacy-filter runner (isolated env) -> predictions JSON.

Runs in a separate venv (transformers 5.6.x + torch) — see README.md — because
privacy-filter's ``transformers`` requirement conflicts with the repo's pinned
4.57.6 / fast-gliner path. The version conflict lives entirely inside this
process: it emits the same :mod:`schema` JSON the repo-env scorer reads.

Decoding (two paths, cross-validated in U1)
-------------------------------------------
* ``pipeline`` — ``transformers.pipeline(task="token-classification",
  aggregation_strategy="first")`` returns entity groups with char ``start`` /
  ``end`` via the fast tokenizer's ``offset_mapping``. ``"first"`` (not
  ``"simple"``) is correct for a subword model.
* ``opf`` — the official ``opf --format json`` CLI, which applies the model's
  constrained Viterbi decoder. **Authoritative fallback** if U1 finds the
  pipeline offsets/labels diverge from ``opf``.

U1 must cross-check the two on a fixed sample set before the head-to-head trusts
pipeline offsets. The pure decode functions below are model-free and unit-tested
in ``tests/privacy/test_scr28_runners.py``; ``transformers``/``torch`` imports
are deferred into the loaders so importing this module never pulls heavy deps.

Offset contract: spans are half-open ``[start, end)`` into
``normalize_text(case.text)`` — each input is normalized with ScreenCap's
``normalize_text`` before inference so offsets align with the scorer's view.
Labels are privacy-filter's **native** span labels (``private_person`` etc.);
the scorer maps them via ``label_maps.OPENAI_ENTITY_MAPPING``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (_PROJECT_ROOT / "src", _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_SCR28_DIR = Path(__file__).resolve().parent
if str(_SCR28_DIR) not in sys.path:
    sys.path.insert(0, str(_SCR28_DIR))

from io_utils import load_input_texts  # noqa: E402
from schema import CasePrediction, PredictedSpan, PredictionFile  # noqa: E402

DEFAULT_MODEL = "openai/privacy-filter"


# ---------------------------------------------------------------------------
# Pure decode functions (model-free, unit-tested)
# ---------------------------------------------------------------------------


def entities_to_spans(entities: list[dict]) -> list[PredictedSpan]:
    """HF token-classification (aggregation_strategy="first") -> PredictedSpans.

    Each entity dict carries ``entity_group``, ``start``, ``end``, ``score``.
    The entity group is privacy-filter's native span label. Spans are emitted
    verbatim and NOT merged — merging adjacent same-type spans is the scorer's
    job (two back-to-back emails must remain two spans here).
    """
    spans: list[PredictedSpan] = []
    for ent in entities:
        start = ent.get("start")
        end = ent.get("end")
        if start is None or end is None or end <= start:
            continue  # drop zero-width / offset-less groups
        label = ent.get("entity_group") or ent.get("entity")
        if not label:
            continue
        spans.append(
            PredictedSpan(
                start=int(start),
                end=int(end),
                label=str(label),
                score=float(ent.get("score", 1.0)),
            )
        )
    return spans


def opf_spans_to_spans(opf_spans: list[dict]) -> list[PredictedSpan]:
    """``opf --format json`` spans -> PredictedSpans.

    opf emits ``{label, start, end, text}`` per detected span (Viterbi-decoded).
    """
    spans: list[PredictedSpan] = []
    for sp in opf_spans:
        start = sp.get("start")
        end = sp.get("end")
        if start is None or end is None or end <= start:
            continue
        label = sp.get("label")
        if not label:
            continue
        spans.append(
            PredictedSpan(
                start=int(start),
                end=int(end),
                label=str(label),
                score=float(sp.get("score", 1.0)),
            )
        )
    return spans


# ---------------------------------------------------------------------------
# Model-backed loaders (deferred heavy imports)
# ---------------------------------------------------------------------------


def _build_pipeline(model_id: str, device: str):
    """Build the HF token-classification pipeline. Deferred transformers import."""
    from transformers import pipeline  # heavy — imported lazily

    return pipeline(
        task="token-classification",
        model=model_id,
        aggregation_strategy="first",
        device=device,
    )


def run_via_pipeline(
    inputs: list[tuple[str, str]], model_id: str, tier: str, device: str = "cpu"
) -> PredictionFile:
    from screencap.privacy import normalize_text

    pipe = _build_pipeline(model_id, device)
    predictions: list[CasePrediction] = []
    for case_id, text in inputs:
        normalized = normalize_text(text)
        entities = pipe(normalized) if normalized else []
        predictions.append(CasePrediction(case_id, entities_to_spans(entities)))
    return PredictionFile(model="privacy-filter", tier=tier, predictions=predictions)


def run_via_opf(
    inputs: list[tuple[str, str]], tier: str, device: str = "cpu"
) -> PredictionFile:
    """Decode via the ``opf`` CLI (one invocation per input).

    Authoritative decoder if U1 finds the HF pipeline diverges. Parses
    ``opf --format json --output-mode typed --device <device>`` output.
    """
    import json

    from screencap.privacy import normalize_text

    predictions: list[CasePrediction] = []
    for case_id, text in inputs:
        normalized = normalize_text(text)
        if not normalized:
            predictions.append(CasePrediction(case_id, []))
            continue
        proc = subprocess.run(
            ["opf", "--format", "json", "--output-mode", "typed", "--device", device],
            input=normalized,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        opf_spans = payload.get("detected_spans") or payload.get("spans") or []
        predictions.append(CasePrediction(case_id, opf_spans_to_spans(opf_spans)))
    return PredictionFile(model="privacy-filter", tier=tier, predictions=predictions)


def main() -> None:
    parser = argparse.ArgumentParser(description="SCR-28 privacy-filter runner")
    parser.add_argument("--decoder", choices=["pipeline", "opf"], default="pipeline")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--device", default="cpu")
    parser.add_argument(
        "--inputs-jsonl",
        required=True,
        help="JSONL with {id|case_id, text} (inputs.jsonl or gold.jsonl)",
    )
    parser.add_argument("--tier", choices=["tier1", "tier2", "smoke"], default="smoke")
    parser.add_argument("--out", required=True, help="Output predictions JSON path")
    args = parser.parse_args()

    inputs = load_input_texts(args.inputs_jsonl)
    print(f"Running privacy-filter decoder={args.decoder} over {len(inputs)} inputs...")
    if args.decoder == "opf":
        prediction_file = run_via_opf(inputs, args.tier, args.device)
    else:
        prediction_file = run_via_pipeline(inputs, args.model, args.tier, args.device)
    prediction_file.save(args.out)
    n_spans = sum(len(p.spans) for p in prediction_file.predictions)
    print(f"Emitted {n_spans} spans across {len(inputs)} cases -> {args.out}")


if __name__ == "__main__":
    main()
