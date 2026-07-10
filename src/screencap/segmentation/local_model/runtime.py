"""Runtime selection + constrained-generation adapters for the downloaded model (U1).

Selects MLX on Apple Silicon and llama.cpp on Intel (KTD2) and exposes one narrow
adapter per runtime: ``generate(model_path, prompt) -> dict | None``. The heavy
backends (``mlx_lm`` / ``llama_cpp``) are imported **lazily** inside the adapters,
so importing this module stays light and cloud-free.

Structured-output asymmetry (KTD2)
----------------------------------
llama.cpp constrains JSON natively via a JSON-schema→GBNF grammar
(:func:`build_json_grammar`), so its output is schema-shaped by construction. MLX
has no first-party schema-constrained decoding, so :class:`MlxRuntime` uses a
bounded prompt-plus-parse-plus-retry loop and leans on the shared validator
(``validate_llm_tasks``, applied by the U2 provider) as the common net. Either way
a runtime returns the *parsed JSON object* or ``None`` — never raises for an
ordinary model/backend failure.

The real backend calls (:meth:`MlxRuntime._raw_default`,
:meth:`LlamaCppRuntime._raw_default`) cannot run in CI (no native lib, no model);
they are covered by the U12 eval on real hardware. The parse/select/factory logic
below is CI-tested by injecting a fake ``raw_generate``.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import platform
import re
from typing import Callable, Protocol, runtime_checkable

from screencap.segmentation.schema import _RESPONSE_SCHEMA

log = logging.getLogger(__name__)

#: Runtime identifiers.
RUNTIME_MLX = "mlx"
RUNTIME_LLAMACPP = "llamacpp"

# `platform.machine()` reports "arm64" on macOS Apple Silicon; accept "aarch64"
# too for robustness across environments.
_ARM_MACHINES = frozenset({"arm64", "aarch64"})

# Bounded retries for the MLX prompt-plus-parse loop (no native grammar → the
# model can emit non-JSON; a small retry recovers transient formatting misses
# without unbounded latency on the once-per-recording path).
_MLX_MAX_ATTEMPTS = 3

# A generous output ceiling — the task-list JSON is compact (KTD-sized), but a
# runaway generation is bounded here rather than by wall-clock alone.
_MAX_OUTPUT_TOKENS = 2048

# Default context window for the llama.cpp model load. The activity summary is
# ~20-50 KB of JSON; 8k tokens comfortably holds it plus the instructions.
_LLAMACPP_N_CTX = 8192


def _mlx_importable() -> bool:
    """True when ``mlx_lm`` can be imported (Apple Silicon with the extra installed)."""
    return importlib.util.find_spec("mlx_lm") is not None


def select_runtime(
    machine: str | None = None, mlx_available: bool | None = None
) -> str:
    """Return the runtime for this host: MLX on Apple Silicon, llama.cpp on Intel.

    ``machine`` and ``mlx_available`` are injectable for tests; production passes
    neither. On Apple Silicon MLX is preferred but falls back to llama.cpp when
    ``mlx_lm`` is not importable (KTD2). x86_64 always resolves to llama.cpp
    (MLX has no Intel build).
    """
    mach = (machine if machine is not None else platform.machine()).lower()
    if mach in _ARM_MACHINES:
        avail = mlx_available if mlx_available is not None else _mlx_importable()
        return RUNTIME_MLX if avail else RUNTIME_LLAMACPP
    return RUNTIME_LLAMACPP


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _extract_json(text: str | None) -> dict | None:
    """Parse a JSON object out of possibly-chatty model output.

    Strips a Markdown code fence, then tries a direct parse, then falls back to
    the first balanced ``{...}`` span. Returns the object or ``None`` when nothing
    parses (never raises).
    """
    if not text:
        return None
    stripped = _FENCE_RE.sub("", text.strip())
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: scan for the first balanced object.
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(stripped)):
        c = stripped[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(stripped[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


@runtime_checkable
class RuntimeAdapter(Protocol):
    """One local-inference backend. Neither method raises for a backend error.

    ``generate`` is JSON-bound (segmentation): it returns the parsed tasks dict
    or ``None``. ``generate_text`` is the free-form recall-answer path (SCR-243,
    KTD8): it returns raw model text or ``None`` — no JSON grammar, no parse,
    no retry.
    """

    def generate(self, model_path: str, prompt: str) -> dict | None:
        ...

    def generate_text(self, model_path: str, prompt: str) -> str | None:
        ...


def get_runtime(name: str) -> RuntimeAdapter:
    """Return the adapter for ``name`` (``"mlx"`` or ``"llamacpp"``).

    Any other name is rejected with a clear :class:`ValueError`. Adapters import
    their backend lazily, so this factory stays import-light.
    """
    if name == RUNTIME_MLX:
        return MlxRuntime()
    if name == RUNTIME_LLAMACPP:
        return LlamaCppRuntime()
    raise ValueError(
        f"Unknown runtime: {name!r}. Known runtimes: {RUNTIME_MLX!r}, {RUNTIME_LLAMACPP!r}."
    )


def build_json_grammar(schema: dict):
    """Compile ``schema`` to a llama.cpp GBNF grammar (native constrained decode).

    Delegates to ``llama_cpp.llama_grammar.LlamaGrammar.from_json_schema`` — the
    battle-tested converter — rather than hand-rolling GBNF. Requires
    ``llama-cpp-python`` (raises :class:`ImportError` otherwise); callers on the
    generation path already have it, and the grammar-build test skips when it is
    not installed.
    """
    from llama_cpp.llama_grammar import LlamaGrammar

    return LlamaGrammar.from_json_schema(json.dumps(schema))


class MlxRuntime:
    """Apple Silicon backend via ``mlx-lm`` (prompt-plus-parse-plus-retry).

    Split into an injectable ``load(model_path) -> handle | None`` and
    ``gen(handle, prompt) -> str | None`` (mirrors the ``GeminiProvider.raw_call``
    seam) so the parse/retry logic is CI-testable without the native library or a
    model. The split matters at runtime too: the model is loaded **once** and only
    *generation* retries on a JSON-parse miss — otherwise a retry would re-read the
    whole ~2 GB model from disk. Both seams default to live ``mlx-lm`` calls.
    """

    def __init__(
        self,
        load: Callable[[str], object | None] | None = None,
        gen: Callable[[object, str], str | None] | None = None,
    ) -> None:
        self._load = load if load is not None else self._load_default
        self._gen = gen if gen is not None else self._gen_default

    def generate(self, model_path: str, prompt: str) -> dict | None:
        handle = self._load(model_path)
        if handle is None:
            return None  # unavailable / load failed
        for attempt in range(_MLX_MAX_ATTEMPTS):
            text = self._gen(handle, prompt)
            if text is None:
                return None  # generation error — do not retry
            parsed = _extract_json(text)
            if parsed is not None:
                return parsed
            log.info("mlx output did not parse as JSON (attempt %d)", attempt + 1)
        return None

    def generate_text(self, model_path: str, prompt: str) -> str | None:
        """Free-form generation (SCR-243, KTD8): raw model text, no JSON parse/retry.

        Reuses the same load/gen seams as :meth:`generate`, but returns the raw
        string (or ``None`` on load/generation failure or empty output) — the
        JSON grammar/parse/retry loop is segmentation-only.
        """
        handle = self._load(model_path)
        if handle is None:
            return None
        text = self._gen(handle, prompt)
        return text if isinstance(text, str) and text.strip() else None

    @staticmethod
    def _load_default(model_path: str) -> object | None:
        """Load the model once. Not exercised in CI (needs the lib + a model)."""
        try:
            from mlx_lm import load
        except ImportError:
            log.info("mlx-lm not installed; runtime unavailable")
            return None
        try:
            return load(model_path)
        except Exception:
            log.warning("mlx-lm failed to load model at %s", model_path, exc_info=True)
            return None

    @staticmethod
    def _gen_default(handle: object, prompt: str) -> str | None:
        """Generate from an already-loaded model. Not exercised in CI."""
        try:
            from mlx_lm import generate as mlx_generate

            model, tokenizer = handle
            return mlx_generate(
                model, tokenizer, prompt=prompt, max_tokens=_MAX_OUTPUT_TOKENS
            )
        except Exception:
            log.warning("mlx-lm generation failed", exc_info=True)
            return None


class LlamaCppRuntime:
    """Intel (and universal-fallback) backend via ``llama-cpp-python``.

    Uses native JSON-schema-constrained decoding so the output is schema-shaped by
    construction. ``raw_generate`` is injectable for CI, defaulting to a live
    ``llama-cpp-python`` call that applies the ``_RESPONSE_SCHEMA`` grammar.
    """

    def __init__(
        self,
        raw_generate: Callable[[str, str], str | None] | None = None,
        raw_text_generate: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._raw = raw_generate if raw_generate is not None else self._raw_default
        self._raw_text = (
            raw_text_generate if raw_text_generate is not None
            else self._raw_text_default
        )

    def generate(self, model_path: str, prompt: str) -> dict | None:
        text = self._raw(model_path, prompt)
        if text is None:
            return None
        return _extract_json(text)

    def generate_text(self, model_path: str, prompt: str) -> str | None:
        """Free-form generation (SCR-243, KTD8): a grammar-free completion.

        Distinct from :meth:`generate`, which forces the ``_RESPONSE_SCHEMA``
        JSON grammar — free-form answers must not be schema-constrained. Returns
        raw text or ``None``.
        """
        text = self._raw_text(model_path, prompt)
        return text if isinstance(text, str) and text.strip() else None

    @staticmethod
    def _raw_default(model_path: str, prompt: str) -> str | None:
        """Live ``llama-cpp-python`` call. Not exercised in CI (needs the lib + a model)."""
        try:
            from llama_cpp import Llama
        except ImportError:
            log.info("llama-cpp-python not installed; runtime unavailable")
            return None
        try:
            llm = Llama(model_path=model_path, n_ctx=_LLAMACPP_N_CTX, verbose=False)
            resp = llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object", "schema": _RESPONSE_SCHEMA},
                temperature=0.1,
                max_tokens=_MAX_OUTPUT_TOKENS,
            )
            return resp["choices"][0]["message"]["content"]
        except Exception:
            log.warning("llama-cpp-python generation failed", exc_info=True)
            return None

    @staticmethod
    def _raw_text_default(model_path: str, prompt: str) -> str | None:
        """Live grammar-free ``llama-cpp-python`` completion (no JSON schema).

        The free-form recall-answer analogue of :meth:`_raw_default` — same load,
        but no ``response_format`` grammar. Not exercised in CI (needs the lib +
        a model).
        """
        try:
            from llama_cpp import Llama
        except ImportError:
            log.info("llama-cpp-python not installed; runtime unavailable")
            return None
        try:
            llm = Llama(model_path=model_path, n_ctx=_LLAMACPP_N_CTX, verbose=False)
            resp = llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=_MAX_OUTPUT_TOKENS,
            )
            return resp["choices"][0]["message"]["content"]
        except Exception:
            log.warning("llama-cpp-python text generation failed", exc_info=True)
            return None
