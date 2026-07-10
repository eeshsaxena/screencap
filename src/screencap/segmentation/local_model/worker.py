"""Subprocess inference worker for the downloaded local model (U2, KTD3).

Runs as ``python -m screencap.segmentation.local_model.worker``, spawned per
recording by :class:`~screencap.segmentation.providers.downloaded.DownloadedProvider`
so a ~2 GB model never stays resident in the all-day daemon and a model crash/OOM
is isolated from it (KTD3).

IPC contract
------------
- **stdin**: one JSON object ``{"prompt": str, "model_path": str}``. The parent
  builds the (privacy-stripped) prompt; the worker only runs the model.
- **stdout**: one JSON envelope —
  ``{"status": "ok", "result": {<raw tasks dict>}}`` when the model produced a
  JSON object, or ``{"status": "unavailable", "reason": str}`` when it could not
  run or produced nothing usable. The parent maps ``unavailable`` to
  ``PROVIDER_UNAVAILABLE`` and validates ``result`` itself.
- **stderr**: minimal; the parent does **not** log it verbatim (it can carry
  recording-derived text). The worker keeps it terse.

The worker imports the heavy runtime (``mlx_lm`` / ``llama_cpp``) only inside the
:mod:`~screencap.segmentation.local_model.runtime` adapters, and runs offline
(``HF_HUB_OFFLINE`` is set by the parent) so it can never fetch anything at
inference time.
"""

from __future__ import annotations

import json
import sys


def _emit(envelope: dict) -> int:
    sys.stdout.write(json.dumps(envelope))
    sys.stdout.flush()
    return 0


def _unavailable(reason: str) -> int:
    return _emit({"status": "unavailable", "reason": reason})


def main(argv: list[str] | None = None) -> int:
    """Read the stdin request, run the model, emit the stdout envelope."""
    raw = sys.stdin.read()
    if not raw.strip():
        return _unavailable("empty-request")
    try:
        request = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _unavailable("bad-request-json")
    if not isinstance(request, dict):
        return _unavailable("bad-request-shape")

    prompt = request.get("prompt")
    model_path = request.get("model_path")
    if not isinstance(prompt, str) or not isinstance(model_path, str):
        return _unavailable("missing-prompt-or-model-path")

    # Import lazily: importing the runtime is light, but its backends load only
    # inside generate(). Any import failure → unavailable (never a traceback on
    # stdout).
    try:
        from screencap.segmentation.local_model.runtime import (
            get_runtime,
            select_runtime,
        )
    except Exception:  # pragma: no cover - defensive
        return _unavailable("runtime-import-failed")

    # Mode discriminator (SCR-243): absent / "segment" → the JSON tasks path
    # (unchanged); "generate_text" → the free-form recall-answer path. Keeping
    # the default as segment leaves the existing request shape byte-compatible.
    mode = request.get("mode", "segment")

    try:
        runtime = get_runtime(select_runtime())
        if mode == "generate_text":
            result: dict | str | None = runtime.generate_text(model_path, prompt)
        else:
            result = runtime.generate(model_path, prompt)
    except Exception:
        # generate*/ are documented never to raise, but guard the boundary so a
        # surprise never crashes the worker with a traceback the parent can't parse.
        return _unavailable("generation-crashed")

    if result is None:
        return _unavailable("no-usable-output")
    return _emit({"status": "ok", "result": result})


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main(sys.argv[1:]))
