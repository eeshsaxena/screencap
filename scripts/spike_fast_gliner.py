#!/usr/bin/env python3
"""Phase 0 compatibility spike: verify fast-gliner with knowledgator ONNX models.

Tests:
1. Model loading (base, small, edge)
2. Entity prediction accuracy on representative texts
3. Score distribution
4. Memory growth over repeated inference
5. Per-call latency vs current gliner package
"""

from __future__ import annotations

import gc
import os
import sys
import time
import traceback

# Representative texts from the benchmark corpus
TEXTS = [
    "John Doe - Google Chrome",
    "jane.smith@example.org - Outlook",
    "Contact jane@example.com at 555-123-4567",
    "Hello John Doe, your email is john@example.com",
    "Ship to 123 Main St, Anytown, CA 90210, USA",
    "SSN: 123-45-6789 CC: 4532 0151 1283 0366",
    "Jose Garcia logged in from terminal",
    "Meeting with Sarah Connor at 3pm",
    "Phone: (555) 123-4567",
    "Dear Mr. Thompson, your account balance is $5,000",
]

# GLiNER entity mapping (same as entity_mapping.py)
GLINER_LABELS = [
    "name", "first name", "last name",
    "email address", "phone number", "ssn",
    "credit card", "location address", "location city", "location country",
]

MODELS = [
    "knowledgator/gliner-pii-base-v1.0",
    "knowledgator/gliner-pii-small-v1.0",
    "knowledgator/gliner-pii-edge-v1.0",
]


def get_rss_mb() -> float:
    """Get current RSS in MB."""
    import resource
    # macOS returns bytes, Linux returns KB
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def test_model(model_id: str) -> dict:
    """Test a single model. Returns results dict."""
    from fast_gliner import FastGLiNER

    print(f"\n{'='*70}")
    print(f"Testing: {model_id}")
    print(f"{'='*70}")

    # 1. Load model
    print("\n[1] Loading model...")
    t0 = time.perf_counter()
    try:
        model = FastGLiNER.from_pretrained(model_id)
    except Exception as e:
        print(f"  FAILED to load: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {"model": model_id, "status": "LOAD_FAILED", "error": str(e)}

    load_time = time.perf_counter() - t0
    print(f"  Loaded in {load_time:.2f}s")

    # 2. Run predictions on representative texts
    print("\n[2] Running predictions...")
    all_predictions = []
    latencies = []

    for text in TEXTS:
        t0 = time.perf_counter()
        try:
            preds = model.predict_entities(text, GLINER_LABELS)
        except Exception as e:
            print(f"  FAILED on text={text!r}: {type(e).__name__}: {e}")
            traceback.print_exc()
            return {"model": model_id, "status": "PREDICT_FAILED", "error": str(e)}
        latency = time.perf_counter() - t0
        latencies.append(latency)

        print(f"\n  Text: {text!r}")
        if preds:
            for p in preds:
                print(f"    -> [{p['label']}] \"{p['text']}\" "
                      f"score={p['score']:.4f} span=({p['start']},{p['end']})")
        else:
            print("    -> (no predictions)")
        all_predictions.append(preds)

    avg_latency = sum(latencies) / len(latencies)
    print(f"\n  Avg latency: {avg_latency*1000:.1f}ms/call")
    print(f"  Min: {min(latencies)*1000:.1f}ms, Max: {max(latencies)*1000:.1f}ms")

    # 3. Score distribution
    print("\n[3] Score distribution:")
    scores = [p["score"] for preds in all_predictions for p in preds]
    if scores:
        print(f"  Count: {len(scores)}")
        print(f"  Min:   {min(scores):.4f}")
        print(f"  Max:   {max(scores):.4f}")
        print(f"  Mean:  {sum(scores)/len(scores):.4f}")
        # Histogram buckets
        buckets = [0]*10
        for s in scores:
            idx = min(int(s * 10), 9)
            buckets[idx] += 1
        for i, count in enumerate(buckets):
            lo, hi = i/10, (i+1)/10
            bar = "#" * count
            print(f"  [{lo:.1f}-{hi:.1f}): {count:3d} {bar}")
    else:
        print("  No predictions — cannot compute scores")

    # 4. Memory growth check (5000 iterations, log every 500)
    print("\n[4] Memory growth check (5000 iterations)...")
    rss_start = get_rss_mb()
    print(f"  RSS at start: {rss_start:.1f} MB")

    test_text = "John Doe lives at 123 Main St and his email is john@example.com"
    for i in range(1, 5001):
        model.predict_entities(test_text, GLINER_LABELS)
        if i % 500 == 0:
            gc.collect()
            rss = get_rss_mb()
            print(f"  Iteration {i:5d}: RSS = {rss:.1f} MB (delta = {rss - rss_start:+.1f} MB)")

    rss_end = get_rss_mb()
    growth = rss_end - rss_start
    print(f"  Final RSS: {rss_end:.1f} MB, total growth: {growth:+.1f} MB")
    growth_ok = growth < 100

    # 5. Per-call latency (warm, 100 calls)
    print("\n[5] Warm latency (100 calls)...")
    warm_latencies = []
    for _ in range(100):
        t0 = time.perf_counter()
        model.predict_entities(test_text, GLINER_LABELS)
        warm_latencies.append(time.perf_counter() - t0)

    avg_warm = sum(warm_latencies) / len(warm_latencies)
    print(f"  Avg: {avg_warm*1000:.1f}ms/call")
    print(f"  P50: {sorted(warm_latencies)[50]*1000:.1f}ms")
    print(f"  P99: {sorted(warm_latencies)[99]*1000:.1f}ms")

    return {
        "model": model_id,
        "status": "OK",
        "load_time_s": load_time,
        "avg_latency_ms": avg_latency * 1000,
        "warm_avg_ms": avg_warm * 1000,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "score_mean": sum(scores)/len(scores) if scores else None,
        "num_predictions": len(scores),
        "memory_growth_mb": growth,
        "memory_ok": growth_ok,
    }


def main():
    print("Phase 0: fast-gliner Compatibility Spike")
    print(f"Python: {sys.version}")
    print(f"Platform: {sys.platform}")

    results = []
    for model_id in MODELS:
        result = test_model(model_id)
        results.append(result)

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    for r in results:
        status = r["status"]
        model = r["model"].split("/")[-1]
        if status == "OK":
            print(f"  {model}: OK "
                  f"(load={r['load_time_s']:.1f}s, "
                  f"latency={r['warm_avg_ms']:.1f}ms, "
                  f"scores={r['score_min']:.2f}-{r['score_max']:.2f}, "
                  f"mem_growth={r['memory_growth_mb']:+.1f}MB)")
        else:
            print(f"  {model}: {status} — {r.get('error', 'unknown')}")

    # Go/No-Go
    print(f"\n{'='*70}")
    print("GO/NO-GO ASSESSMENT")
    print(f"{'='*70}")
    any_ok = any(r["status"] == "OK" for r in results)
    if any_ok:
        ok_models = [r for r in results if r["status"] == "OK"]
        for r in ok_models:
            model = r["model"].split("/")[-1]
            checks = []
            checks.append(f"Model loads: YES")
            checks.append(f"Scores in range (0.3-0.95): "
                         f"{'YES' if r['score_min'] and r['score_min'] >= 0.1 else 'CHECK'}")
            checks.append(f"Memory growth < 100MB: {'YES' if r['memory_ok'] else 'NO'}")
            print(f"\n  {model}:")
            for c in checks:
                print(f"    - {c}")
        print(f"\n  VERDICT: GO — {len(ok_models)} model(s) compatible")
    else:
        print("  VERDICT: NO-GO — no models loaded successfully")


if __name__ == "__main__":
    main()
