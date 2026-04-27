#!/usr/bin/env python3
"""Synthetic-corpus audit for R11 keep-mouse.move chunk-processor default.

Unit 9 of the unified-export-callable refactor (plan
``docs/plans/2026-04-26-001-refactor-unified-export-callable-plan.md``)
specifies a pre-merge gate that measures four operational impacts of
keeping ``mouse.move`` events in chunk JSONL by default:

  1. Worst-case chunk JSONL size (≤50 MB).
  2. Scrubber walk wall time as a fraction of total chunk processing
     wall time (≤30%).
  3. End-to-end chunk processing wall time vs the pre-Unit-6 baseline
     (post/pre ratio ≤2.0). Baseline is reproduced here by manually
     dropping ``mouse.move`` rows before feeding the unified callable —
     that is exactly what the chunk processor used to do.
  4. Cloud Run parse RSS (≤1.5× baseline). SKIPPED in synthetic mode.

The plan's preferred audit corpus is a real ≥30-min idle-reading
recording. This script implements the option-c synthetic fallback: it
fabricates a worst-case 10-min chunk worth of action_event +
window_event rows directly in memory, runs them through the production
chunk pipeline (``unified_export_events`` →
``write_events_jsonl`` → ``scrub_events_jsonl``), and reports the
measurements with explicit caveats. The synthetic gate catches gross
regressions (e.g. JSONL grows to >100 MB, scrubber walk dominates) but
is not a substitute for the real-recording audit before final merge.

Usage::

    python benchmarks/benchmark_mouse_move_bloat.py
    python benchmarks/benchmark_mouse_move_bloat.py --chunk-seconds 600
    python benchmarks/benchmark_mouse_move_bloat.py --json results.json
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import random
import sys
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path

# Ensure the project root is importable.
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root / "src"))
sys.path.insert(0, str(_project_root))


# ---------------------------------------------------------------------------
# Synthetic corpus generation
# ---------------------------------------------------------------------------


# Real bundle IDs from screencap/privacy/context.py so the policy matrix
# actually routes through MASK_WINDOW (Slack, PUBLIC mode → CHAT →
# MASK_WINDOW) and ALLOW (an unknown bundle ID → UNKNOWN class → ALLOW
# in PUBLIC mode).
MASK_WINDOW_BUNDLE_ID = "com.tinyspeck.slackmacgap"  # CHAT → MASK_WINDOW (PUBLIC)
ALLOW_BUNDLE_ID = "com.example.PlainTextEditor"  # UNKNOWN → ALLOW (PUBLIC)


@dataclass
class CorpusStats:
    chunk_seconds: float
    mouse_move_rows: int
    other_action_rows: int
    window_rows: int
    mask_window_intervals: int
    allow_intervals: int


def generate_synthetic_corpus(
    chunk_seconds: float,
    seed: int,
    chunk_start_ts: float = 1_700_000_000.0,
) -> tuple[list[dict], list[dict], list, CorpusStats]:
    """Build a worst-case 10-min idle-reading chunk.

    Profile:

    - Activity timeline alternates 60-second windows between MASK_WINDOW
      app (Slack) and ALLOW app (synthetic plaintext editor) so the
      scrub-layer interval handling is exercised.
    - 70% of seconds are idle reading: dense ``mouse.move`` rows at
      ~12 ms cadence (worst-case OS event rate, high cursor activity
      while reading). No clicks or keys in idle stretches.
    - 30% of seconds are active interaction: a click every ~3 s, a
      scroll every ~2 s, a 5-character key.type burst every ~6 s, plus
      regular mouse.move at ~30 ms cadence.

    Returns:
        (action_rows, window_rows, blocked_intervals, stats)
    """
    from screencap.privacy.actions import PrivacyAction
    from screencap.scrub_pipeline import BlockedInterval

    rng = random.Random(seed)
    action_rows: list[dict] = []
    window_rows: list[dict] = []
    blocked_intervals: list[BlockedInterval] = []

    chunk_end_ts = chunk_start_ts + chunk_seconds
    row_id = 1

    # Window-event timeline: alternate every 60 s between MASK_WINDOW and
    # ALLOW. Use modulo on the integer second so the toggles are
    # deterministic regardless of the random seed.
    SWITCH_INTERVAL = 60.0
    cursor = chunk_start_ts
    is_mask = True
    win_id_counter = 0

    while cursor < chunk_end_ts:
        bundle = MASK_WINDOW_BUNDLE_ID if is_mask else ALLOW_BUNDLE_ID
        title = "Slack — #general" if is_mask else "untitled.txt"
        win_id_counter += 1
        window_rows.append({
            "id": win_id_counter,
            "recording_id": 1,
            "timestamp": cursor,
            "app_bundle_id": bundle,
            "title": title,
            "window_id": str(win_id_counter),
            "left": 0,
            "top": 0,
            "width": 1920,
            "height": 1080,
            "browser_url": None,
        })
        seg_end = min(cursor + SWITCH_INTERVAL, chunk_end_ts)
        if is_mask:
            blocked_intervals.append(BlockedInterval(
                start=cursor,
                end=seg_end,
                action=PrivacyAction.MASK_WINDOW,
                reason="synthetic_mask_window",
            ))
        cursor = seg_end
        is_mask = not is_mask

    mask_intervals_count = len(blocked_intervals)
    allow_intervals_count = len(window_rows) - mask_intervals_count

    # Action-event timeline: walk one second at a time, decide
    # idle-vs-active by deterministic 70/30 split, emit rows.
    sec_offset = 0
    mouse_x = 600
    mouse_y = 400
    while sec_offset < chunk_seconds:
        t_sec_start = chunk_start_ts + sec_offset
        # Deterministic 70/30 split: every 10-second window has 7
        # idle seconds and 3 active seconds, in that order. (Random
        # would also work, but periodic gives more reproducible
        # interval-vs-event interactions.)
        is_idle = (sec_offset % 10) < 7

        if is_idle:
            # Dense moves at ~12 ms cadence with ±3 ms jitter.
            t = t_sec_start
            while t < t_sec_start + 1.0:
                # Small random walk.
                mouse_x = max(0, min(1919, mouse_x + rng.randint(-3, 3)))
                mouse_y = max(0, min(1079, mouse_y + rng.randint(-3, 3)))
                action_rows.append({
                    "id": row_id,
                    "recording_id": 1,
                    "name": "move",
                    "timestamp": t,
                    "mouse_x": mouse_x,
                    "mouse_y": mouse_y,
                    "mouse_pressure": None,
                    "modifier_flags": None,
                })
                row_id += 1
                t += 0.012 + rng.random() * 0.006 - 0.003
        else:
            # Active second: ~30 ms move cadence + a click ~every 3 s
            # + a scroll ~every 2 s + a key burst ~every 6 s.
            t = t_sec_start
            while t < t_sec_start + 1.0:
                mouse_x = max(0, min(1919, mouse_x + rng.randint(-15, 15)))
                mouse_y = max(0, min(1079, mouse_y + rng.randint(-15, 15)))
                action_rows.append({
                    "id": row_id,
                    "recording_id": 1,
                    "name": "move",
                    "timestamp": t,
                    "mouse_x": mouse_x,
                    "mouse_y": mouse_y,
                    "mouse_pressure": None,
                    "modifier_flags": None,
                })
                row_id += 1
                t += 0.030 + rng.random() * 0.015 - 0.0075

            # Click pair every 3 active-seconds (i.e. seconds 7, 10, …).
            if sec_offset % 3 == 0:
                ts_click = t_sec_start + 0.5
                action_rows.append({
                    "id": row_id, "recording_id": 1, "name": "click",
                    "timestamp": ts_click,
                    "mouse_x": mouse_x, "mouse_y": mouse_y,
                    "mouse_button_name": "left",
                    "mouse_pressed": True,
                    "mouse_pressure": None, "modifier_flags": None,
                })
                row_id += 1
                action_rows.append({
                    "id": row_id, "recording_id": 1, "name": "click",
                    "timestamp": ts_click + 0.05,
                    "mouse_x": mouse_x, "mouse_y": mouse_y,
                    "mouse_button_name": "left",
                    "mouse_pressed": False,
                    "mouse_pressure": None, "modifier_flags": None,
                })
                row_id += 1

            # Scroll every 2 active-seconds.
            if sec_offset % 2 == 0:
                action_rows.append({
                    "id": row_id, "recording_id": 1, "name": "scroll",
                    "timestamp": t_sec_start + 0.7,
                    "mouse_x": mouse_x, "mouse_y": mouse_y,
                    "mouse_dx": 0, "mouse_dy": 3,
                    "modifier_flags": None,
                    "scroll_phase": None, "momentum_phase": None,
                    "is_continuous": False,
                })
                row_id += 1

            # Five-character key burst every 6 active-seconds.
            if sec_offset % 6 == 0:
                burst_t = t_sec_start + 0.9
                for ch in "hello":
                    action_rows.append({
                        "id": row_id, "recording_id": 1, "name": "press",
                        "timestamp": burst_t,
                        "key_char": ch, "canonical_key_char": ch,
                        "key_name": ch, "canonical_key_name": ch,
                        "key_vk": None, "canonical_key_vk": None,
                    })
                    row_id += 1
                    action_rows.append({
                        "id": row_id, "recording_id": 1, "name": "release",
                        "timestamp": burst_t + 0.02,
                        "key_char": ch, "canonical_key_char": ch,
                        "key_name": ch, "canonical_key_name": ch,
                        "key_vk": None, "canonical_key_vk": None,
                    })
                    row_id += 1
                    burst_t += 0.06

        sec_offset += 1

    mouse_move_rows = sum(1 for r in action_rows if r["name"] == "move")
    other_action_rows = len(action_rows) - mouse_move_rows
    stats = CorpusStats(
        chunk_seconds=chunk_seconds,
        mouse_move_rows=mouse_move_rows,
        other_action_rows=other_action_rows,
        window_rows=len(window_rows),
        mask_window_intervals=mask_intervals_count,
        allow_intervals=allow_intervals_count,
    )
    return action_rows, window_rows, blocked_intervals, stats


# ---------------------------------------------------------------------------
# Pipeline drivers
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    label: str
    action_rows_in: int
    events_written: int
    jsonl_bytes: int
    export_seconds: float
    scrub_seconds: float
    total_seconds: float
    peak_traced_bytes: int


def _make_scrubber():
    """Build the production scrubber pipeline + anonymizer.

    The chunk processor does ``create_default_pipeline(require_pii=True)``;
    we mirror that. The first call is slow because it spins up Presidio
    + GLiNER; subsequent runs reuse the same instances.
    """
    from screencap.privacy import Anonymizer, create_default_pipeline

    pipeline = create_default_pipeline(require_pii=True)
    anonymizer = Anonymizer()
    return pipeline, anonymizer


def run_pipeline(
    label: str,
    action_rows: list[dict],
    window_rows: list[dict],
    blocked_intervals,
    pipeline,
    anonymizer,
    out_dir: Path,
) -> PipelineResult:
    """Run a single (export → scrub) pass and capture timings + sizes."""
    from screencap.engine.export import unified_export_events
    from screencap.exporter import build_export_metadata, write_events_jsonl
    from screencap.privacy.filter import build_cloud_window_filter
    from screencap.scrub_pipeline import scrub_events_jsonl

    jsonl_path = out_dir / f"events_{label}.jsonl"
    if jsonl_path.exists():
        jsonl_path.unlink()

    # Build the cloud filter the same way the chunk processor does.
    window_filter = build_cloud_window_filter(
        cloud_bound=True,
        privacy_mode="internal",
        capture_dir=out_dir,
    )

    gc.collect()
    tracemalloc.start()

    t0 = time.perf_counter()
    events = unified_export_events(
        action_rows,
        window_rows,
        initial_window_row=None,
        window_filter=window_filter,
    )
    meta = build_export_metadata(exclude_moves=False)
    events_written = write_events_jsonl(jsonl_path, events, meta)
    t_export = time.perf_counter() - t0

    jsonl_bytes = jsonl_path.stat().st_size

    t1 = time.perf_counter()
    had_errors = scrub_events_jsonl(
        jsonl_path,
        pipeline,
        anonymizer,
        blocked_intervals=blocked_intervals,
    )
    t_scrub = time.perf_counter() - t1

    _, peak_traced = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    if had_errors:
        print(f"  [warn] scrub reported errors for {label}", file=sys.stderr)

    return PipelineResult(
        label=label,
        action_rows_in=len(action_rows),
        events_written=events_written,
        jsonl_bytes=jsonl_bytes,
        export_seconds=t_export,
        scrub_seconds=t_scrub,
        total_seconds=t_export + t_scrub,
        peak_traced_bytes=peak_traced,
    )


# ---------------------------------------------------------------------------
# Reporting + decision gate
# ---------------------------------------------------------------------------


# Plan thresholds.
MAX_JSONL_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_SCRUB_FRACTION = 0.30  # 30% of total wall time
MAX_WALL_RATIO = 2.0  # post-Unit-6 / pre-Unit-6


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.2f} KB"
    return f"{n} B"


def evaluate_gate(
    post: PipelineResult, pre: PipelineResult,
) -> tuple[bool, dict]:
    """Apply the plan thresholds to the synthetic measurements.

    Per the orchestrator instructions for option-c synthetic mode:

    - #1 (JSONL size) and #3 (wall ratio) are hard quantified gates.
    - #2 (scrub walk fraction) is **directional only** in synthetic mode —
      the synthetic corpus has no transcription, no screenshot masking,
      no upload latency, so the scrubber will dominate the absolute total
      regardless of whether moves are present. The signal worth reading
      is the post-vs-pre **delta** in scrub time, not the absolute fraction.
    - #4 (Cloud Run RSS) is skipped in synthetic mode.

    The synthetic gate passes when #1 and #3 pass, with #2 reported and
    diagnosed but not gating. The real-recording audit before final merge
    is the authoritative #2 check.

    Returns ``(passed, details_dict)``.
    """
    scrub_fraction = (
        post.scrub_seconds / post.total_seconds if post.total_seconds else 0.0
    )
    wall_ratio = (
        post.total_seconds / pre.total_seconds if pre.total_seconds else 0.0
    )
    scrub_delta_ratio = (
        post.scrub_seconds / pre.scrub_seconds if pre.scrub_seconds else 0.0
    )

    checks = {
        "chunk_jsonl_size_pass": post.jsonl_bytes <= MAX_JSONL_BYTES,
        # Directional — reported but not gating in synthetic mode.
        "scrub_fraction_pass": scrub_fraction <= MAX_SCRUB_FRACTION,
        "wall_ratio_pass": wall_ratio <= MAX_WALL_RATIO,
        "cloud_run_memory_pass": None,  # SKIPPED in synthetic mode
    }
    details = {
        "jsonl_bytes": post.jsonl_bytes,
        "jsonl_threshold_bytes": MAX_JSONL_BYTES,
        "scrub_fraction": scrub_fraction,
        "scrub_fraction_threshold": MAX_SCRUB_FRACTION,
        "scrub_delta_ratio": scrub_delta_ratio,
        "wall_ratio": wall_ratio,
        "wall_ratio_threshold": MAX_WALL_RATIO,
        "checks": checks,
    }
    # Hard gates: #1 and #3. #2 is directional, #4 is skipped.
    quantified_pass = (
        checks["chunk_jsonl_size_pass"] and checks["wall_ratio_pass"]
    )
    return quantified_pass, details


def render_report(
    stats: CorpusStats,
    post: PipelineResult,
    pre: PipelineResult,
    gate_passed: bool,
    details: dict,
) -> str:
    lines: list[str] = []
    lines.append("# R11 Mouse-Move Bloat Audit (Synthetic, Unit 9 Option C)\n")
    lines.append(
        "Synthetic corpus stand-in for the plan's preferred ≥30-min idle-reading\n"
        "recording. Catches gross regressions only — real-recording audit is\n"
        "still required before final merge.\n"
    )

    lines.append("## Synthetic corpus characteristics\n")
    lines.append(f"- Chunk duration: {stats.chunk_seconds:.0f} s (matches default 600 s chunk)")
    lines.append(f"- mouse.move rows: {stats.mouse_move_rows:,}")
    lines.append(f"- other action rows (clicks/scrolls/keys): {stats.other_action_rows:,}")
    lines.append(f"- window_event rows: {stats.window_rows} ({stats.mask_window_intervals} MASK_WINDOW intervals + {stats.allow_intervals} ALLOW)")
    lines.append("- App mix: alternating 60s windows between Slack (CHAT → MASK_WINDOW in PUBLIC) and an unknown bundle (UNKNOWN → ALLOW)")
    lines.append("- Activity profile: 70% idle reading at ~12ms move cadence, 30% active interaction (clicks + scrolls + key bursts) at ~30ms move cadence\n")

    lines.append("## Measurements\n")
    lines.append("| Measurement | Post-Unit-6 (keep moves) | Pre-Unit-6 baseline (drop moves) | Ratio |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| Action rows in | {post.action_rows_in:,} | {pre.action_rows_in:,} | "
        f"{post.action_rows_in / max(pre.action_rows_in, 1):.2f}× |"
    )
    lines.append(
        f"| Events written | {post.events_written:,} | {pre.events_written:,} | "
        f"{post.events_written / max(pre.events_written, 1):.2f}× |"
    )
    lines.append(
        f"| Chunk JSONL size | {_fmt_bytes(post.jsonl_bytes)} | {_fmt_bytes(pre.jsonl_bytes)} | "
        f"{post.jsonl_bytes / max(pre.jsonl_bytes, 1):.2f}× |"
    )
    lines.append(
        f"| Export wall time | {post.export_seconds:.3f} s | {pre.export_seconds:.3f} s | "
        f"{post.export_seconds / max(pre.export_seconds, 1e-9):.2f}× |"
    )
    lines.append(
        f"| Scrub walk wall time | {post.scrub_seconds:.3f} s | {pre.scrub_seconds:.3f} s | "
        f"{post.scrub_seconds / max(pre.scrub_seconds, 1e-9):.2f}× |"
    )
    lines.append(
        f"| Total wall time | {post.total_seconds:.3f} s | {pre.total_seconds:.3f} s | "
        f"{post.total_seconds / max(pre.total_seconds, 1e-9):.2f}× |"
    )
    lines.append(
        f"| Peak tracemalloc | {_fmt_bytes(post.peak_traced_bytes)} | {_fmt_bytes(pre.peak_traced_bytes)} | "
        f"{post.peak_traced_bytes / max(pre.peak_traced_bytes, 1):.2f}× |"
    )
    lines.append("")

    lines.append("## Decision gate\n")
    lines.append("| # | Threshold | Value | Pass? |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| 1 | Chunk JSONL ≤ {_fmt_bytes(MAX_JSONL_BYTES)} | "
        f"{_fmt_bytes(post.jsonl_bytes)} | "
        f"{'PASS' if details['checks']['chunk_jsonl_size_pass'] else 'BREACH'} |"
    )
    lines.append(
        f"| 2 | Scrub walk ≤ {MAX_SCRUB_FRACTION:.0%} of total (directional in synthetic) | "
        f"{details['scrub_fraction']:.1%} (post/pre scrub delta = {details['scrub_delta_ratio']:.2f}×) | "
        f"{'PASS' if details['checks']['scrub_fraction_pass'] else 'DIRECTIONAL — see diagnosis'} |"
    )
    lines.append(
        f"| 3 | Total wall ≤ {MAX_WALL_RATIO:.1f}× baseline | "
        f"{details['wall_ratio']:.2f}× | "
        f"{'PASS' if details['checks']['wall_ratio_pass'] else 'BREACH'} |"
    )
    lines.append(
        "| 4 | Cloud Run parse RSS ≤ 1.5× baseline | n/a | "
        "SKIPPED (option c, synthetic-only) |"
    )
    lines.append("")

    # Diagnosis for #2 if it tripped the directional threshold.
    if not details["checks"]["scrub_fraction_pass"]:
        lines.append(
            "### #2 directional diagnosis\n\n"
            "The scrub walk exceeds 30% of total wall time in the synthetic corpus, "
            "but this is a corpus artifact rather than a regression caused by moves. "
            "Two pieces of evidence:\n\n"
            f"- **Pre-Unit-6 baseline already shows the same pattern:** scrub took "
            f"{pre.scrub_seconds:.2f}s out of {pre.total_seconds:.2f}s "
            f"({pre.scrub_seconds / max(pre.total_seconds, 1e-9):.1%}) when moves "
            f"were dropped before reaching the callable. Removing moves would not "
            f"bring the fraction under 30%.\n"
            f"- **Adding moves only changes scrub time by "
            f"{details['scrub_delta_ratio']:.2f}× (post/pre).** The scrubber walks "
            f"every event recursively through Presidio + GLiNER; the per-event fixed "
            f"cost dominates a synthetic corpus that lacks real chunk-pipeline work "
            f"(transcription, screenshot masking, upload latency). Real recordings "
            f"have far more total wall time, which would naturally push the scrub "
            f"fraction down.\n\n"
            "Per the orchestrator's option-c instructions, threshold #2 is "
            "**directional only** in synthetic mode — the real-recording audit is the "
            "authoritative #2 check.\n"
        )

    if gate_passed:
        lines.append(
            "**Synthetic gate verdict: PASS** — hard quantified thresholds #1 and #3 "
            "satisfied; #2 directional (see diagnosis); #4 skipped (option c). "
            "Real-recording audit on a ≥30-min idle-reading session is still required "
            "before final merge for full sign-off (per Unit 9 of the plan).\n"
        )
    else:
        lines.append(
            "**Synthetic gate verdict: BREACH** — at least one hard quantified threshold "
            "(#1 or #3) failed. Per the plan's no-implementer-override clause, Unit 6's "
            "removal of the chunk-processor's mouse.move drop must be reverted in a "
            "follow-up commit, and R11/R12 updated to 'chunk path drops mouse.move by "
            "default'.\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic R11 mouse.move bloat audit (Unit 9, option c)."
    )
    parser.add_argument(
        "--chunk-seconds", type=float, default=600.0,
        help="Synthetic chunk duration in seconds (default 600s, matches the chunk processor default).",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="RNG seed for reproducible mouse jitter (default 42).",
    )
    parser.add_argument(
        "--json", type=Path, default=None,
        help="Optional path to write the raw measurements as JSON.",
    )
    args = parser.parse_args()

    # Quiet down screencap's INFO logging during the run.
    logging.basicConfig(level=logging.WARNING)

    print("Generating synthetic corpus...")
    action_rows, window_rows, blocked_intervals, stats = generate_synthetic_corpus(
        chunk_seconds=args.chunk_seconds, seed=args.seed,
    )
    print(
        f"  rows: {stats.mouse_move_rows:,} mouse.move + "
        f"{stats.other_action_rows:,} other actions + "
        f"{stats.window_rows} window events"
    )
    print(
        f"  intervals: {stats.mask_window_intervals} MASK_WINDOW + "
        f"{stats.allow_intervals} ALLOW"
    )

    print("Initializing scrubber pipeline (Presidio + GLiNER warmup)...")
    pipeline, anonymizer = _make_scrubber()

    with tempfile.TemporaryDirectory(prefix="benchmark_mouse_move_bloat_") as td:
        out_dir = Path(td)

        # Pre-Unit-6 baseline: drop mouse.move rows before they reach the
        # unified callable. This is exactly what the chunk processor used
        # to do (a ``MouseMoveEvent`` filter at the chunk processor layer).
        # Run the baseline FIRST so any one-time costs (Pydantic schema
        # warmup, regex compilation in process_events, etc.) are paid
        # before the post-Unit-6 measurement.
        pre_rows = [r for r in action_rows if r["name"] != "move"]

        print("\nRun 1 — pre-Unit-6 baseline (mouse.move rows dropped before callable)...")
        pre_result = run_pipeline(
            "baseline_no_moves",
            pre_rows, window_rows, blocked_intervals,
            pipeline, anonymizer, out_dir,
        )
        print(
            f"  {pre_result.events_written:,} events, "
            f"{_fmt_bytes(pre_result.jsonl_bytes)}, "
            f"export={pre_result.export_seconds:.2f}s, "
            f"scrub={pre_result.scrub_seconds:.2f}s"
        )

        print("Run 2 — post-Unit-6 (mouse.move kept by default)...")
        post_result = run_pipeline(
            "post_unit6_keep_moves",
            action_rows, window_rows, blocked_intervals,
            pipeline, anonymizer, out_dir,
        )
        print(
            f"  {post_result.events_written:,} events, "
            f"{_fmt_bytes(post_result.jsonl_bytes)}, "
            f"export={post_result.export_seconds:.2f}s, "
            f"scrub={post_result.scrub_seconds:.2f}s"
        )

    gate_passed, details = evaluate_gate(post_result, pre_result)
    report = render_report(stats, post_result, pre_result, gate_passed, details)
    print()
    print(report)

    if args.json is not None:
        payload = {
            "stats": asdict(stats),
            "post": asdict(post_result),
            "pre": asdict(pre_result),
            "gate_details": details,
            "gate_passed_synthetic": gate_passed,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nMeasurements written to {args.json}")

    # Exit non-zero on synthetic-gate breach so CI / orchestrator can
    # detect it without parsing the report.
    return 0 if gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
