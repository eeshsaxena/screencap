"""Segmentation eval — the "beat the heuristic, never mislead" gate (U12, SCR-239).

Scores a local model's named-task output against a labeled fixture and the
idle-gap heuristic baseline, and reports the two numbers that gate turning named
output on by default (KTD10):

- **preference / quality** — does the model produce *correct* names where the
  heuristic produces none? (``correct_rate``)
- **never mislead** — of the names actually *surfaced* (after the U3 confidence
  gate), how many are *wrong*? (``wrong_name_rate`` — must reach ≈0). The score
  applies the confidence gate at the chosen threshold first, so it reflects what
  a user would see and proves the fail-closed default catches **field omissions**
  (a task the model emitted with no ``confidence`` is blanked, not surfaced).

The scoring logic below is deterministic and unit-tested on fixtures. The real
model run (:func:`run_model_eval`) needs a downloaded model on real hardware and
is invoked manually — it is not exercised in CI.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Importable whether run as a script (``python benchmarks/benchmark_segmentation.py``)
# or imported by the test (which inserts ``src`` on the path).
try:
    from screencap.segmentation.confidence_gate import apply_confidence_gate
except ImportError:  # pragma: no cover - script convenience
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from screencap.segmentation.confidence_gate import apply_confidence_gate


def _normalize(name: str) -> str:
    return " ".join((name or "").lower().split())


@dataclass(frozen=True)
class RunScore:
    """The scored outcome of one run over the labeled fixture."""

    total: int
    surfaced: int  # tasks with a non-empty name (after the gate)
    correct: int  # surfaced names that match an acceptable label
    wrong: int  # surfaced names that do NOT match (a mislead)
    unnamed: int  # blanked by the gate / never named

    @property
    def wrong_name_rate(self) -> float:
        return (self.wrong / self.surfaced) if self.surfaced else 0.0

    @property
    def correct_rate(self) -> float:
        return (self.correct / self.total) if self.total else 0.0


def score_run(raw_tasks: list[dict], labels: list[dict], threshold: str = "low") -> RunScore:
    """Apply the confidence gate at ``threshold``, then score names against labels.

    ``raw_tasks`` are the model's pre-gate tasks (each a dict with ``name`` and
    ``confidence``); ``labels`` is parallel by index, each ``{"acceptable": [...]}``
    naming the acceptable intent(s) for that interval. A surfaced name that is not
    acceptable is a *mislead* (wrong); a blanked name is *unnamed*.
    """
    gated = apply_confidence_gate({"tasks": copy.deepcopy(raw_tasks)}, threshold)
    tasks = gated["tasks"]
    correct = wrong = unnamed = 0
    for i, task in enumerate(tasks):
        name = _normalize(task.get("name", ""))
        if not name:
            unnamed += 1
            continue
        acceptable = {_normalize(a) for a in labels[i].get("acceptable", [])}
        if name in acceptable:
            correct += 1
        else:
            wrong += 1
    return RunScore(
        total=len(tasks), surfaced=correct + wrong, correct=correct,
        wrong=wrong, unnamed=unnamed,
    )


def heuristic_baseline(labels: list[dict]) -> RunScore:
    """The idle-gap heuristic names nothing meaningfully → 0 correct, 0 wrong."""
    return RunScore(total=len(labels), surfaced=0, correct=0, wrong=0, unnamed=len(labels))


def beats_heuristic(model: RunScore, heuristic: RunScore) -> bool:
    """The default-on gate: names users prefer over the heuristic, and none wrong."""
    return model.correct_rate > heuristic.correct_rate and model.wrong_name_rate == 0.0


def run_model_eval(fixture_path: Path, threshold: str = "low") -> dict:  # pragma: no cover
    """Manual path: run the downloaded model over the fixture on real hardware.

    Not run in CI (needs a downloaded model). Loads ``{sessions: [{summary,
    labels}]}`` and, for each, runs the provider, scores it, and reports the
    aggregate preference + wrong-name-rate against the bar.
    """
    from screencap.segmentation.routing import build_day_split_provider

    data = json.loads(fixture_path.read_text())
    provider = build_day_split_provider()
    model_scores, heur_scores = [], []
    for session in data["sessions"]:
        result = provider.segment(session["summary"])
        raw = result["tasks"] if isinstance(result, dict) else []
        model_scores.append(score_run(raw, session["labels"], threshold))
        heur_scores.append(heuristic_baseline(session["labels"]))
    return {
        "sessions": [asdict(s) for s in model_scores],
        "wrong_name_rate": max((s.wrong_name_rate for s in model_scores), default=0.0),
        "beats_heuristic": all(
            beats_heuristic(m, h) for m, h in zip(model_scores, heur_scores)
        ),
    }


def main() -> int:  # pragma: no cover - manual driver
    ap = argparse.ArgumentParser(description="Segmentation eval (SCR-239 U12)")
    ap.add_argument("fixture", type=Path)
    ap.add_argument("--threshold", default="low")
    args = ap.parse_args()
    report = run_model_eval(args.fixture, args.threshold)
    print(json.dumps(report, indent=2))
    return 0 if report["beats_heuristic"] and report["wrong_name_rate"] == 0.0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
