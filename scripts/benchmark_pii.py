#!/usr/bin/env python3
"""Standalone PII detection benchmark runner.

Usage:
    python scripts/benchmark_pii.py --engine presidio
    python scripts/benchmark_pii.py --engine presidio-gliner
    python scripts/benchmark_pii.py --compare benchmark_results/a.json benchmark_results/b.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on the path
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root / "src"))
sys.path.insert(0, str(_project_root))

from screencap.privacy import DetectionPipeline
from screencap.privacy.regex import RegexDetector
from screencap.privacy.secrets import DetectSecretsDetector
from tests.privacy.fixtures.test_corpus import FALSE_POSITIVE_CASES, TRUE_POSITIVE_CASES
from tests.privacy.test_benchmark import (
    AggregateResult,
    print_benchmark_table,
    run_benchmark,
    save_benchmark_json,
)


def _build_pipeline(engine: str) -> DetectionPipeline:
    """Build a pipeline for the given engine."""
    if engine == "full":
        # Full pipeline with resolver + heuristic filter (Step 3)
        from screencap.privacy import create_default_pipeline

        return create_default_pipeline()

    detectors = [RegexDetector(), DetectSecretsDetector()]

    if engine in ("presidio", "presidio-gliner"):
        from screencap.privacy.pii import PiiDetector

        ner_backend = "spacy" if engine == "presidio" else "gliner"
        detectors.append(PiiDetector(ner_backend=ner_backend))

    return DetectionPipeline(detectors)


def _compare(file_a: Path, file_b: Path) -> None:
    """Compare two benchmark JSON files side-by-side."""
    a = json.loads(file_a.read_text())
    b = json.loads(file_b.read_text())

    print(f"\n## Benchmark Comparison")
    print(f"  A: {file_a.name}")
    print(f"  B: {file_b.name}\n")

    # Totals comparison
    ta, tb = a["totals"], b["totals"]
    print("### Totals\n")
    print("| Metric | A | B | Delta |")
    print("|---|---|---|---|")
    for key in ["true_positives", "false_positives", "false_negatives"]:
        va, vb = ta[key], tb[key]
        delta = vb - va
        sign = "+" if delta > 0 else ""
        print(f"| {key} | {va} | {vb} | {sign}{delta} |")
    for key in ["precision", "recall_partial", "recall_exact", "avg_coverage", "f1",
                 "document_leak_rate", "redaction_survival_rate"]:
        va, vb = ta.get(key, 0), tb.get(key, 0)
        delta = vb - va
        sign = "+" if delta > 0 else ""
        print(f"| {key} | {va:.1%} | {vb:.1%} | {sign}{delta:.1%} |")
    for key in ["weighted_fp_impact"]:
        va, vb = ta.get(key, 0), tb.get(key, 0)
        delta = vb - va
        sign = "+" if delta > 0 else ""
        print(f"| {key} | {va:.0f} | {vb:.0f} | {sign}{delta:.0f} |")

    # FP by source comparison
    src_a = a.get("fp_by_source", {})
    src_b = b.get("fp_by_source", {})
    all_sources = sorted(set(src_a.keys()) | set(src_b.keys()))
    if all_sources:
        print("\n### FP by Detector Source\n")
        print("| Source | A | B | Delta |")
        print("|---|---|---|---|")
        for src in all_sources:
            va, vb = src_a.get(src, 0), src_b.get(src, 0)
            delta = vb - va
            sign = "+" if delta > 0 else ""
            print(f"| {src} | {va} | {vb} | {sign}{delta} |")

    # Per-type comparison
    all_types = sorted(set(a.get("per_type", {}).keys()) | set(b.get("per_type", {}).keys()))
    if all_types:
        print("\n### Per-Type FP Comparison\n")
        print("| Entity Type | FP (A) | FP (B) | Delta |")
        print("|---|---|---|---|")
        for etype in all_types:
            fp_a = a.get("per_type", {}).get(etype, {}).get("false_positives", 0)
            fp_b = b.get("per_type", {}).get(etype, {}).get("false_positives", 0)
            delta = fp_b - fp_a
            sign = "+" if delta > 0 else ""
            print(f"| {etype} | {fp_a} | {fp_b} | {sign}{delta} |")


def main() -> None:
    parser = argparse.ArgumentParser(description="PII detection benchmark runner")
    parser.add_argument(
        "--engine",
        choices=["presidio", "presidio-gliner", "full"],
        default="full",
        help="PII engine to benchmark. 'full' includes GLiNER + resolver + heuristic filter (default: full)",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("FILE_A", "FILE_B"),
        help="Compare two benchmark JSON files",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark_results",
        help="Directory for JSON output (default: benchmark_results/)",
    )
    args = parser.parse_args()

    if args.compare:
        _compare(Path(args.compare[0]), Path(args.compare[1]))
        return

    print(f"Building pipeline with engine={args.engine}...")
    pipeline = _build_pipeline(args.engine)

    print("Running benchmark...")
    results = run_benchmark(pipeline)
    print_benchmark_table(results)

    # Save results
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = Path(args.output_dir) / f"{today}-{args.engine}.json"
    save_benchmark_json(results, out_path)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
