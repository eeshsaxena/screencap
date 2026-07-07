"""Downloadable local-model inference (U1+, SCR-239).

The opt-in downloadable small model (MLX on Apple Silicon, llama.cpp on Intel)
that produces named tasks on-device without macOS 26. This sub-package is a
sibling of the shipped providers and is imported only when the ``downloaded``
provider actually runs — the heavy runtimes (``mlx_lm`` / ``llama_cpp``) are
imported lazily inside the adapters, so importing the segmentation package (or
this one) never drags them in.

Submodules:

- ``runtime`` — ``select_runtime()`` picks MLX on Apple Silicon and llama.cpp on
  Intel (KTD2); ``get_runtime(name)`` returns the matching adapter, each exposing
  a narrow ``generate(model_path, prompt) -> dict | None`` (constrained JSON on
  llama.cpp, prompt-plus-parse-plus-retry on MLX).
"""
