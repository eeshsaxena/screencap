#!/usr/bin/env python3
"""Score every SCR-28 prediction file and emit one consolidated report (U4+U6).

Runs in the repo env. Reads the prediction JSONs the two runners produced, grades
each against its gold via ``scorer.score_prediction_file``, builds the U6
full-pipeline union (privacy-filter NER ∪ GLiNER regex/secrets), computes the
Tier-1 published-F1 reproduction for both models, and writes a Markdown summary +
per-file scored JSON under ``results/``.

The report carries only aggregate/per-type numbers (no raw PII strings), but it is
written under the gitignored ``results/`` dir; transcribe the headline numbers into
the verdict doc (which is committed).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _path_setup  # noqa: F401,E402
from schema import PredictionFile  # noqa: E402
from scorer import (  # noqa: E402
    load_cases_jsonl,
    remap_file_to_entitytype,
    remap_file_to_secret_bucket,
    score_prediction_file,
    union_prediction_files,
)
from scoring_core import AggregateResult  # noqa: E402
from tier1_pii_masking import binary_pii_f1  # noqa: E402

R = Path(__file__).resolve().parent / "results"
T2 = Path(__file__).resolve().parent / "tier2_testbed"

HIGH_HARM = ("PERSON", "EMAIL", "PHONE", "SSN", "CREDIT_CARD", "ADDRESS")


def md_table(agg: AggregateResult, title: str) -> str:
    lines = [f"### {title}", ""]
    lines.append("| Entity | TP | FP | FN | Exact R | Partial R | Precision | F1 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for etype in sorted(agg.per_type, key=lambda e: (e not in HIGH_HARM, e)):
        r = agg.per_type[etype]
        lines.append(
            f"| {etype} | {r.true_positives} | {r.false_positives} | "
            f"{r.false_negatives} | {r.recall_exact:.0%} | {r.recall_partial:.0%} | "
            f"{r.precision:.0%} | {r.f1:.0%} |"
        )
    lines.append("")
    lines.append(
        f"**Totals:** TP={agg.total_tp} FP={agg.total_fp} FN={agg.total_fn} · "
        f"Precision {agg.precision:.1%} · Partial-Recall {agg.recall_partial:.1%} · "
        f"Exact-Recall {agg.recall_exact:.1%} · F1 {agg.f1:.1%}"
    )
    lines.append(
        f"**Doc leak rate:** {agg.document_leak_rate:.1%} · "
        f"**Redaction survival:** {agg.redaction_survival_rate:.1%} · "
        f"**FP by source:** {dict(sorted(agg.fp_by_source.items()))}"
    )
    lines.append("")
    return "\n".join(lines)


def score(pred_path: Path, cases, title: str, out: list[str]) -> AggregateResult:
    pf = PredictionFile.load(_require(pred_path))
    agg = score_prediction_file(pf, cases)
    out.append(md_table(agg, title))
    return agg


def _require(path: Path) -> Path:
    """Fail legibly if a regenerated (gitignored) artifact is missing."""
    if not path.exists():
        raise SystemExit(
            f"missing {path} — run the README run order first (tier1 emit + both "
            f"runners + tier2 extract/_draft_gold) to regenerate results/ and "
            f"tier2_testbed/ artifacts, then re-run score_all.py."
        )
    return path


def main() -> None:
    report: list[str] = ["# SCR-28 scoring report", ""]

    # ---- Tier 1 (public benchmark) ----
    t1_cases = load_cases_jsonl(_require(R / "tier1-gold.jsonl"))
    report.append("## Tier 1 — PII-Masking-300k (English validation, n=400)\n")
    score(R / "tier1-gliner-ner.json", t1_cases, "Tier1 · GLiNER NER", report)
    score(R / "tier1-gliner-full.json", t1_cases, "Tier1 · GLiNER full pipeline", report)
    score(R / "tier1-pf-ner.json", t1_cases, "Tier1 · privacy-filter NER", report)

    report.append("### Tier-1 published-F1 reproduction (binary PII-vs-O, char-level)\n")
    report.append("| Model | Precision | Recall | F1 |")
    report.append("|---|---|---|---|")
    for label, fname in [("GLiNER NER", "tier1-gliner-ner.json"),
                         ("privacy-filter NER", "tier1-pf-ner.json")]:
        m = binary_pii_f1(PredictionFile.load(R / fname), t1_cases)
        report.append(f"| {label} | {m['precision']:.3f} | {m['recall']:.3f} | {m['f1']:.3f} |")
    report.append("\n_Baseline target (privacy-filter model card): token F1 ~0.96, "
                  "exact-span ~0.926. Char-level proxy, expect *near* not exact._\n")

    # ---- Tier 2 real ----
    real_cases = load_cases_jsonl(_require(T2 / "gold.jsonl"))
    report.append("## Tier 2 — REAL ScreenCap OCR/AX (v15-pii-positive, "
                  f"{sum(1 for c in real_cases if c.expected)} PII / "
                  f"{sum(1 for c in real_cases if c.is_false_positive)} FP probes)\n")
    score(R / "tier2-real-gliner-ner.json", real_cases, "Tier2-real · GLiNER NER", report)
    score(R / "tier2-real-pf-ner.json", real_cases, "Tier2-real · privacy-filter NER", report)
    _full_pipeline(R / "tier2-real-pf-ner.json", R / "tier2-real-gliner-regexsecrets.json",
                   real_cases, "Tier2-real · privacy-filter ∪ regex/secrets (full pipeline)", report)
    score(R / "tier2-real-gliner-full.json", real_cases, "Tier2-real · GLiNER full pipeline", report)

    # ---- Tier 2 synthetic ----
    syn_cases = load_cases_jsonl(_require(T2 / "gold.synthetic.jsonl"))
    report.append("## Tier 2 — SYNTHETIC OCR-noise (per-type recall power; "
                  f"{sum(1 for c in syn_cases if c.expected)} PII-bearing)\n")
    report.append("_Synthetic values (standard test SSN/card numbers, invented names) in "
                  "OCR-styled noise. Recall-under-noise signal the sparse real set can't give._\n")
    score(R / "tier2-syn-gliner-ner.json", syn_cases, "Tier2-syn · GLiNER NER", report)
    score(R / "tier2-syn-pf-ner.json", syn_cases, "Tier2-syn · privacy-filter NER", report)
    _full_pipeline(R / "tier2-syn-pf-ner.json", R / "tier2-syn-gliner-regex-secrets.json",
                   syn_cases, "Tier2-syn · privacy-filter ∪ regex/secrets (full pipeline)", report)
    score(R / "tier2-syn-gliner-full.json", syn_cases, "Tier2-syn · GLiNER full pipeline", report)

    # ---- Secrets / API-keys (binary SECRET bucket) ----
    sec_cases = load_cases_jsonl(_require(T2 / "gold.secrets.jsonl"))
    report.append("## Secrets / API-keys (binary SECRET bucket; "
                  f"{sum(1 for c in sec_cases if c.expected)} secrets / "
                  f"{sum(1 for c in sec_cases if c.is_false_positive)} entropy-trap distractors)\n")
    report.append("_privacy-filter's native `secret` NER vs ScreenCap's dedicated "
                  "regex+detect-secrets layer. Synthetic, code-OCR styled, tokens intact._\n")
    _secrets(R / "secrets-pf-ner.json", sec_cases, "Secrets · privacy-filter `secret` NER", report)
    _secrets(R / "secrets-gliner-regexsecrets.json", sec_cases,
             "Secrets · ScreenCap regex+detect-secrets layer", report)
    _secrets(R / "secrets-gliner-ner.json", sec_cases,
             "Secrets · GLiNER NER (baseline — no secret class)", report)

    out_path = R / "SCORES.md"
    out_path.write_text("\n".join(report) + "\n")
    print(f"Wrote {out_path}")
    print("\n".join(report))


def _secrets(pred_path: Path, cases, title: str, out: list[str]) -> None:
    bucketed = remap_file_to_secret_bucket(PredictionFile.load(_require(pred_path)))
    agg = score_prediction_file(bucketed, cases)
    out.append(md_table(agg, title))


def _full_pipeline(pf_ner: Path, gliner_rs: Path, cases, title: str, out: list[str]) -> None:
    union = union_prediction_files([
        remap_file_to_entitytype(PredictionFile.load(pf_ner)),
        remap_file_to_entitytype(PredictionFile.load(gliner_rs)),
    ])
    agg = score_prediction_file(union, cases)
    out.append(md_table(agg, title))


if __name__ == "__main__":
    main()
