#!/usr/bin/env python3
"""Faithful published-F1 sanity check: full-taxonomy binary PII-vs-O (run in .venv-pf).

``score_all.py``'s binary metric collapses gold to ScreenCap's 6 EntityTypes,
which understates privacy-filter (its url/date/account_number/secret predictions
and the dataset's many dropped classes don't count). The model card's 96% headline
is binary over the model's *own* taxonomy on *all* PII. This reproduces that
faithfully: gold PII = **every** ``privacy_mask`` span (any class); predicted PII =
**every** predicted span (any label). If privacy-filter lands near its published
number here while the collapsed metric is low, the wiring is sound and the low
collapsed number is purely the 6-type collapse + char strictness (R3 sanity check).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _path_setup  # noqa: F401,E402

from screencap.privacy import normalize_text  # noqa: E402

DATASET = "ai4privacy/pii-masking-300k"


def _mask_from_substrings(normalized: str, subs: list[str]) -> set[int]:
    covered: set[int] = set()
    for sub in subs:
        if not sub:
            continue
        i = normalized.find(sub)
        while i != -1:
            covered.update(range(i, i + len(sub)))
            i = normalized.find(sub, i + 1)
    return covered


def main() -> None:
    from datasets import load_dataset

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    # Rebuild the SAME 400-record English validation gold, but keep ALL classes.
    ds = load_dataset(DATASET, split="validation", streaming=True)
    gold_subs: dict[str, list[str]] = {}
    order: list[str] = []
    n = 0
    for rec in ds:
        if rec.get("language") != "English":
            continue
        cid = rec.get("id") or f"tier1-{n:06d}"
        gold_subs[cid] = [m["value"] for m in rec.get("privacy_mask", [])]
        order.append(cid)
        n += 1
        if n >= limit:
            break
    # We need each case's text to normalize + locate. Re-read from emitted gold.
    text_by_id = {}
    for line in (Path(__file__).resolve().parent / "results" / "tier1-gold.jsonl").open():
        d = json.loads(line)
        text_by_id[d["id"]] = normalize_text(d["text"])

    for label, fname in [("GLiNER NER", "tier1-gliner-ner.json"),
                         ("privacy-filter NER", "tier1-pf-ner.json")]:
        pf = json.loads((Path(__file__).resolve().parent / "results" / fname).read_text())
        tp = fp = fn = 0
        pred_by_id: dict[str, list] = {}
        for cp in pf["predictions"]:
            pred_by_id[cp["case_id"]] = cp["spans"]
        for cid in order:
            norm = text_by_id.get(cid)
            if norm is None:
                continue
            gold = _mask_from_substrings(norm, gold_subs.get(cid, []))
            pred: set[int] = set()
            for s in pred_by_id.get(cid, []):
                lo, hi = max(0, s["start"]), min(len(norm), s["end"])
                if hi > lo:
                    pred.update(range(lo, hi))
            tp += len(gold & pred)
            fp += len(pred - gold)
            fn += len(gold - pred)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        print(f"{label:22s} full-taxonomy binary char F1={f1:.3f}  P={p:.3f} R={r:.3f}  "
              f"(TP={tp} FP={fp} FN={fn})")


if __name__ == "__main__":
    main()
