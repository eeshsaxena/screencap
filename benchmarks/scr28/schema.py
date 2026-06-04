"""Common prediction JSON schema for the SCR-28 spike.

Both model runners emit predictions in this schema:

* ``run_gliner.py`` — repo env (transformers 4.57.6 + fast-gliner ONNX)
* ``run_privacy_filter.py`` — isolated env (transformers 5.6.x + torch)

``scorer.py`` (repo env) loads them and grades against gold cases. This module
is **pure stdlib** so it imports cleanly in either environment — never add an
ML or ``screencap`` dependency here.

Offset contract
---------------
Span ``[start, end)`` are half-open character offsets into
``screencap.privacy.normalize_text(case.text)``. Each runner MUST normalize the
case text with ``normalize_text`` *before* inference, so the offsets it emits
align with the text the scorer reconstructs for the same case. Keeping the text
out of the JSON keeps Tier-1 files small and avoids re-committing the Tier-2
testbed's raw PII into the prediction artifacts.

Labels
------
``label`` is the model's **native** label (e.g. ``"private_person"`` for
privacy-filter, or an already-mapped ``EntityType`` constant like ``"PERSON"``
for the GLiNER production pipeline). The scorer maps natives to ``EntityType``
through ``label_maps.py`` — keeping the mapping in one place and letting
out-of-scope labels (``private_url`` etc.) stay visible in the raw predictions
for coverage-delta reporting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PredictedSpan:
    """A single predicted span in a case.

    ``start`` / ``end`` are half-open char offsets into the normalized case text.
    ``label`` is the model's native label. ``source`` optionally tags which
    detector produced it (``"ner"``, ``"regex"``, ``"secrets"``) so the
    full-pipeline union read can attribute spans.
    """

    start: int
    end: int
    label: str
    score: float = 1.0
    source: str = "ner"

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "label": self.label,
            "score": self.score,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PredictedSpan":
        return cls(
            start=int(d["start"]),
            end=int(d["end"]),
            label=str(d["label"]),
            score=float(d.get("score", 1.0)),
            source=str(d.get("source", "ner")),
        )


@dataclass
class CasePrediction:
    """All predicted spans for one ``CorpusCase``, keyed by ``case_id``."""

    case_id: str
    spans: list[PredictedSpan] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "spans": [s.to_dict() for s in self.spans]}

    @classmethod
    def from_dict(cls, d: dict) -> "CasePrediction":
        return cls(
            case_id=str(d["case_id"]),
            spans=[PredictedSpan.from_dict(s) for s in d.get("spans", [])],
        )


@dataclass
class PredictionFile:
    """Top-level prediction artifact: one model, one tier, many cases."""

    model: str  # "gliner" | "privacy-filter"
    tier: str  # "tier1" | "tier2" | "smoke"
    predictions: list[CasePrediction] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "model": self.model,
            "tier": self.tier,
            "predictions": [p.to_dict() for p in self.predictions],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PredictionFile":
        version = int(d.get("schema_version", SCHEMA_VERSION))
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"prediction schema_version {version} != supported {SCHEMA_VERSION}"
            )
        return cls(
            model=str(d["model"]),
            tier=str(d["tier"]),
            predictions=[CasePrediction.from_dict(p) for p in d.get("predictions", [])],
            schema_version=version,
        )

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def load(cls, path: Path | str) -> "PredictionFile":
        return cls.from_dict(json.loads(Path(path).read_text()))
