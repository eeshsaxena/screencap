#!/usr/bin/env python3
"""Footprint, latency/throughput, and entity-coverage delta (U7).

The non-accuracy axes the verdict weighs (R8, R9). Four modes, each runnable
independently so the GLiNER side measures now (model cached) and the
privacy-filter side measures once ``.venv-pf`` + weights are in place:

* ``coverage-delta`` — **pure, no model.** Tabulate each backend's native labels
  against ScreenCap's ``EntityType`` set: privacy-filter adds url/date/
  account_number/secret (no ``EntityType`` home) and lacks SSN/credit-card
  (currently produced by GLiNER's ``ssn``/``credit card``). Feeds AE3.
* ``disk --backend {gliner,privacy-filter}`` — on-disk size from the HF cache
  (the *bundled-binary* reality: this is what a CLI would have to ship/download).
* ``rss --backend {gliner,privacy-filter}`` — resident memory of the loaded model
  (peak RSS delta across load + a warmup inference).
* ``latency --backend {gliner,privacy-filter} --inputs-jsonl ...`` — per-block CPU
  inference time over Tier-2-shaped inputs: median + p90 + blocks/sec. The MoE's
  50M active params do **not** guarantee fast CPU inference — measure, don't assume.

Footprint is integration cost, not just a parameter count: a transformers+torch
backend reopens the minos/PyInstaller smoke-test matrix recorded in
``docs/solutions/build-errors/`` (``minos`` is contagious; the frozen binary
negatively asserts onnxruntime/pip exclusion). Cite those as the real cost — this
script measures the data; it does not act on it.

Run GLiNER-side modes in the repo venv; privacy-filter-side in ``.venv-pf``.
"""

from __future__ import annotations

import argparse
import math
import os
import resource
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_setup  # noqa: F401,E402  (import for side effect: bootstraps sys.path)
from io_utils import load_input_texts  # noqa: E402

# HF repo ids -> local cache dir stem (models--<org>--<name>).
GLINER_REPO = "knowledgator/gliner-pii-base-v1.0"
PRIVACY_FILTER_REPO = "openai/privacy-filter"

# privacy-filter ONNX variants (model card file tree) and their realistic CLI
# trade-off. q4f16 is the smallest practical CPU variant; safetensors is the
# unquantized reference.
PF_VARIANTS = {
    "q4f16": "onnx/model_q4f16.onnx",
    "q4": "onnx/model_q4.onnx",
    "quantized": "onnx/model_quantized.onnx",  # int8
    "fp16": "onnx/model_fp16.onnx",
    "safetensors": "model.safetensors",
}


# ---------------------------------------------------------------------------
# Disk footprint (HF cache)
# ---------------------------------------------------------------------------


def _hf_hub_dir() -> Path:
    base = os.environ.get("HF_HOME")
    if base:
        return Path(base) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _repo_cache_dir(repo_id: str) -> Path:
    return _hf_hub_dir() / ("models--" + repo_id.replace("/", "--"))


def _dir_size_bytes(path: Path, *, follow_symlinks: bool = True) -> int:
    """Sum real on-disk bytes under ``path``.

    The HF cache stores weights in ``blobs/`` and exposes them as symlinks under
    ``snapshots/``. Summing ``snapshots`` with ``follow_symlinks`` double-counts
    nothing (each blob is one file) and reflects what a fresh download fetches.
    Dedupe by resolved real path so a file linked twice is counted once.
    """
    seen: set[Path] = set()
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path, followlinks=follow_symlinks):
        for name in files:
            fp = Path(root) / name
            try:
                real = fp.resolve()
                if real in seen:
                    continue
                seen.add(real)
                total += real.stat().st_size
            except OSError:
                continue
    return total


def measure_disk(repo_id: str, *, variant_file: str | None = None) -> dict:
    """On-disk size for a repo (whole cache, or a single variant file)."""
    cache_dir = _repo_cache_dir(repo_id)
    if not cache_dir.exists():
        return {"repo": repo_id, "cached": False, "bytes": 0, "human": "not cached"}
    if variant_file:
        # Find the variant within snapshots (resolve through the symlink to blob).
        matches = list(cache_dir.glob(f"snapshots/*/{variant_file}"))
        size = 0
        for m in matches:
            try:
                size += m.resolve().stat().st_size
            except OSError:
                pass
        # Variant ONNX often ships external .onnx_data sidecars — include them.
        sidecars = list(cache_dir.glob(f"snapshots/*/{variant_file}_data*"))
        for s in sidecars:
            try:
                size += s.resolve().stat().st_size
            except OSError:
                pass
        return {
            "repo": repo_id,
            "cached": True,
            "variant": variant_file,
            "bytes": size,
            "human": _human(size),
        }
    size = _dir_size_bytes(cache_dir)
    return {"repo": repo_id, "cached": True, "bytes": size, "human": _human(size)}


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


# ---------------------------------------------------------------------------
# Resident memory
# ---------------------------------------------------------------------------


def _peak_rss_bytes() -> int:
    """Peak RSS of this process. ru_maxrss is bytes on macOS, KiB on Linux."""
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return maxrss if sys.platform == "darwin" else maxrss * 1024


def measure_rss_gliner() -> dict:
    """Peak RSS delta from loading GLiNER + one warmup inference (repo env)."""
    before = _peak_rss_bytes()
    from run_gliner import _build_detect

    detect = _build_detect("ner")
    detect("Warmup: email john@example.com and call +1 415 555 1212")
    after = _peak_rss_bytes()
    return {"backend": "gliner", "rss_after_bytes": after, "rss_after": _human(after),
            "rss_delta_bytes": after - before, "rss_delta": _human(after - before)}


def measure_rss_privacy_filter(model_id: str, device: str) -> dict:
    """Peak RSS delta from loading privacy-filter + one warmup inference (.venv-pf)."""
    before = _peak_rss_bytes()
    from run_privacy_filter import _build_pipeline

    pipe = _build_pipeline(model_id, device)
    pipe("Warmup: email john@example.com and call +1 415 555 1212")
    after = _peak_rss_bytes()
    return {"backend": "privacy-filter", "rss_after_bytes": after, "rss_after": _human(after),
            "rss_delta_bytes": after - before, "rss_delta": _human(after - before)}


# ---------------------------------------------------------------------------
# Latency / throughput
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (pct in [0, 100]); ``values`` need not be sorted.

    Rank = ceil(pct/100 * N), 1-indexed (so p100 -> max, p50 of 5 -> 3rd value).
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(pct / 100 * len(ordered))
    k = max(0, min(len(ordered) - 1, rank - 1))
    return ordered[k]


def measure_latency(detect, inputs: list[tuple[str, str]], *, warmup: int = 2) -> dict:
    """Per-block wall-clock over ``inputs``. ``detect(text)`` runs one block.

    Reports median + p90 per-block seconds and blocks/sec. Block-shaped inputs
    match production (the scrubber detects per OCR/AX block, not whole screens).
    """
    from screencap.privacy import normalize_text

    texts = [normalize_text(t) for _id, t in inputs]
    for t in texts[:warmup]:  # warm caches / lazy graph build
        detect(t)
    per_block: list[float] = []
    t_start = time.perf_counter()
    for t in texts:
        t0 = time.perf_counter()
        detect(t)
        per_block.append(time.perf_counter() - t0)
    total = time.perf_counter() - t_start
    n = len(per_block)
    return {
        "n_blocks": n,
        "total_seconds": total,
        "median_ms": statistics.median(per_block) * 1000 if per_block else 0.0,
        "p90_ms": _percentile(per_block, 90) * 1000,
        "blocks_per_sec": n / total if total else 0.0,
    }


def _gliner_detect_for_latency():
    from run_gliner import _build_detect

    detect = _build_detect("ner")
    return lambda text: detect(text)


def _privacy_filter_detect_for_latency(model_id: str, device: str):
    from run_privacy_filter import _build_pipeline

    pipe = _build_pipeline(model_id, device)
    return lambda text: pipe(text) if text else []


# ---------------------------------------------------------------------------
# Coverage delta (pure — no model)
# ---------------------------------------------------------------------------


def coverage_delta_rows() -> list[tuple[str, str, str]]:
    """(backend, native_label, mapped) rows for the R9 coverage-delta table."""
    from label_maps import (
        OPENAI_ENTITY_MAPPING,
        map_gliner_label,
    )

    from screencap.privacy.entity_mapping import GLINER_ENTITY_MAPPING

    rows: list[tuple[str, str, str]] = []
    for native in sorted(OPENAI_ENTITY_MAPPING):
        rows.append(("privacy-filter", native, OPENAI_ENTITY_MAPPING[native]))
    for native in sorted(GLINER_ENTITY_MAPPING):
        mapped = map_gliner_label(native) or "DROPPED"
        rows.append(("gliner", native, mapped))
    return rows


def print_coverage_delta() -> None:
    from screencap.privacy import EntityType

    rows = coverage_delta_rows()
    print("\n## Entity-coverage delta (R9)\n")
    print("| Backend | Native label | Maps to |")
    print("|---|---|---|")
    for backend, native, mapped in rows:
        print(f"| {backend} | `{native}` | {mapped} |")

    pf_types = {m for b, _n, m in rows if b == "privacy-filter"}
    gl_types = {m for b, _n, m in rows if b == "gliner"}
    real = {v for k, v in vars(EntityType).items() if not k.startswith("_") and isinstance(v, str)}
    pf_real = pf_types & real
    gl_real = gl_types & real
    print("\n**privacy-filter NER reaches EntityTypes:** " + ", ".join(sorted(pf_real)))
    print("**GLiNER NER reaches EntityTypes:** " + ", ".join(sorted(gl_real)))
    gap = sorted(gl_real - pf_real)
    print(
        "\n**Gap (GLiNER NER emits, privacy-filter NER does not):** "
        + (", ".join(gap) if gap else "none")
        + ". These fall to ScreenCap's regex/secrets layer for privacy-filter — "
        "whether that compensates is the U6 full-pipeline question (AE3)."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_json(label: str, payload: dict) -> None:
    import json

    print(f"\n# {label}")
    print(json.dumps(payload, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="SCR-28 footprint / latency / coverage (U7)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("coverage-delta", help="Print the entity-coverage delta table (pure)")

    p_disk = sub.add_parser("disk", help="On-disk size from the HF cache")
    p_disk.add_argument("--backend", required=True, choices=["gliner", "privacy-filter"])
    p_disk.add_argument(
        "--variant",
        choices=list(PF_VARIANTS),
        help="privacy-filter ONNX variant to size (default: whole repo cache)",
    )

    p_rss = sub.add_parser("rss", help="Resident memory of the loaded model")
    p_rss.add_argument("--backend", required=True, choices=["gliner", "privacy-filter"])
    p_rss.add_argument("--model", default=PRIVACY_FILTER_REPO)
    p_rss.add_argument("--device", default="cpu")

    p_lat = sub.add_parser("latency", help="Per-block latency/throughput over inputs")
    p_lat.add_argument("--backend", required=True, choices=["gliner", "privacy-filter"])
    p_lat.add_argument("--inputs-jsonl", required=True)
    p_lat.add_argument("--model", default=PRIVACY_FILTER_REPO)
    p_lat.add_argument("--device", default="cpu")

    args = parser.parse_args()

    if args.command == "coverage-delta":
        print_coverage_delta()
        return

    if args.command == "disk":
        if args.backend == "gliner":
            _print_json("GLiNER disk footprint", measure_disk(GLINER_REPO))
        else:
            variant_file = PF_VARIANTS[args.variant] if args.variant else None
            _print_json(
                "privacy-filter disk footprint",
                measure_disk(PRIVACY_FILTER_REPO, variant_file=variant_file),
            )
        return

    if args.command == "rss":
        if args.backend == "gliner":
            _print_json("GLiNER RSS", measure_rss_gliner())
        else:
            _print_json("privacy-filter RSS", measure_rss_privacy_filter(args.model, args.device))
        return

    if args.command == "latency":
        inputs = load_input_texts(args.inputs_jsonl)
        if args.backend == "gliner":
            detect = _gliner_detect_for_latency()
            label = "GLiNER latency"
        else:
            detect = _privacy_filter_detect_for_latency(args.model, args.device)
            label = "privacy-filter latency"
        _print_json(f"{label} (n={len(inputs)} blocks)", measure_latency(detect, inputs))
        return


if __name__ == "__main__":
    main()
