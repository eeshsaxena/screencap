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
from typing import Any

# Put this script's own dir on sys.path so the sibling flat modules (`_path_setup`
# et al.) import whether this runs as a script or as
# `benchmarks.scr28.run_privacy_filter`. `_path_setup` then adds src/ + project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_setup  # noqa: F401,E402  (import for side effect: bootstraps sys.path)
from io_utils import load_input_texts  # noqa: E402
from schema import CasePrediction, PredictedSpan, PredictionFile  # noqa: E402

DEFAULT_MODEL = "openai/privacy-filter"

# opf forks one process per input; above this many inputs the per-process startup
# cost dominates and the `pipeline` decoder should be used instead.
OPF_INPUT_CEILING = 1000


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


def _build_pipeline(model_id: str, device: str) -> Any:
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
        try:
            proc = subprocess.run(
                ["opf", "--format", "json", "--output-mode", "typed", "--device", device],
                input=normalized,
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
            payload = json.loads(proc.stdout)
        except FileNotFoundError:
            # opf binary missing — fatal: the whole run can't proceed.
            raise SystemExit("opf not found; is .venv-pf set up?") from None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            # Per-input failure: skip this case (no detections) and keep going so
            # one bad block doesn't abort the whole tier.
            print(
                f"[warn] opf decode failed for case {case_id!r}: "
                f"{type(exc).__name__}; recording no detections.",
                file=sys.stderr,
            )
            predictions.append(CasePrediction(case_id, []))
            continue
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
        # opf spawns one subprocess per input — fine at Tier-2 block scale, but
        # a Tier-1 sweep (thousands of inputs) would fork thousands of processes.
        if len(inputs) > OPF_INPUT_CEILING:
            print(
                f"[warn] opf decoder spawns one process per input and you passed "
                f"{len(inputs)} (> {OPF_INPUT_CEILING}); this will be very slow. "
                f"Use --decoder pipeline at Tier-1 scale.",
                file=sys.stderr,
            )
        prediction_file = run_via_opf(inputs, args.tier, args.device)
    else:
        prediction_file = run_via_pipeline(inputs, args.model, args.tier, args.device)
    prediction_file.save(args.out)
    n_spans = sum(len(p.spans) for p in prediction_file.predictions)
    print(f"Emitted {n_spans} spans across {len(inputs)} cases -> {args.out}")


if __name__ == "__main__":
    main()
