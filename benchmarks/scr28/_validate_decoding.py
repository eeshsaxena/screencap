#!/usr/bin/env python3
"""U1 decoding validation for openai/privacy-filter (run in .venv-pf).

The plan's U1 cross-checks the HF ``pipeline`` char offsets against the official
``opf --format json`` Viterbi decoder. **There is no ``opf`` PyPI package** (the
model card ships only the ``pipeline`` API; the "opf CLI" in the plan does not
exist), so this script instead validates the ``pipeline`` decoder directly:

* loads ``pipeline(task="token-classification", model="openai/privacy-filter")``
  with both ``aggregation_strategy="first"`` and ``"simple"``,
* runs a fixed sample set (name / email / phone / address / date / high-entropy
  secret / a no-PII false-positive probe),
* asserts the emitted half-open ``[start, end)`` offsets actually slice the
  predicted surface text out of the input (offset correctness), and
* records the 8 native labels seen, so the label-map (label_maps.py) is confirmed
  against real output.

Caveat recorded for the verdict: HF ``pipeline`` aggregation does generic BIOES
grouping over per-token argmax, **not** the model's constrained Viterbi decoder.
Without ``opf`` we cannot diff against Viterbi; the spike measures the
pipeline-decoded path (what a straightforward integration would use) and flags
boundary-coherence differences as a known caveat.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _path_setup  # noqa: F401,E402

MODEL = "openai/privacy-filter"

# (label, text) — fixed probe set. The last two are false-positive probes: a
# high-entropy build token and a pure no-PII line that must stay clean.
SAMPLES = [
    ("name", "My name is Alice Smith and I work there."),
    ("email", "Reach me at john.doe@example.com any time."),
    ("phone", "Call +1 415 555 1212 to confirm the order."),
    ("address", "Ship it to 1600 Amphitheatre Parkway, Mountain View, CA 94043."),
    ("date", "The contract starts on January 15, 2026."),
    ("secret", "Run with AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE set."),
    ("fp-build", "Compiling module foo with 3 warnings and 0 errors."),
]


def _run(strategy: str, device: str = "cpu") -> list[dict]:
    from transformers import pipeline

    clf = pipeline(task="token-classification", model=MODEL,
                   aggregation_strategy=strategy, device=device)
    rows = []
    print(f"\n## aggregation_strategy={strategy!r}\n")
    for label, text in SAMPLES:
        ents = clf(text)
        for e in ents:
            start, end = e.get("start"), e.get("end")
            grp = e.get("entity_group") or e.get("entity")
            score = float(e.get("score", 0.0))
            sliced = text[start:end] if start is not None and end is not None else None
            word = (e.get("word") or "").strip()
            # Offset correctness: the slice should equal the reported word
            # (modulo the tokenizer's leading-space marker).
            ok = sliced is not None and sliced.strip() == word.strip()
            rows.append({"sample": label, "label": grp, "start": start, "end": end,
                         "score": round(score, 4), "slice": sliced, "word": word, "offset_ok": ok})
            flag = "OK " if ok else "!! "
            print(f"  [{flag}] {label:9s} {grp:16s} [{start},{end}) "
                  f"slice={sliced!r} word={word!r} score={score:.3f}")
        if not ents:
            print(f"  [ -- ] {label:9s} (no spans)")
    return rows


def main() -> None:
    print(f"# U1 decoding validation — {MODEL}")
    import transformers
    print(f"transformers {transformers.__version__}")

    rows_first = _run("first")
    rows_simple = _run("simple")

    all_rows = rows_first + rows_simple
    bad = [r for r in all_rows if not r["offset_ok"]]
    labels_seen = sorted({r["label"] for r in all_rows})
    print("\n## Summary")
    print(f"native labels observed: {labels_seen}")
    print(f"offset mismatches: {len(bad)} / {len(all_rows)}")
    if bad:
        print("MISMATCHES:")
        for r in bad:
            print(f"  {r}")
    # Exit non-zero if offsets are broken — that would invalidate the head-to-head.
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
