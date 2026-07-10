"""U1 — runtime selection + adapter interface (SCR-239).

Covers ``select_runtime`` (arch → runtime, with the MLX-unavailable fallback),
the ``get_runtime`` factory, the injectable adapter parse/retry logic, and the
JSON-extraction helper. The real backend calls are not exercised here — no native
lib, no model in CI; they are covered by the U12 eval on real hardware.
"""

from __future__ import annotations

import pytest

from screencap.segmentation.local_model import runtime
from screencap.segmentation.local_model.runtime import (
    RUNTIME_LLAMACPP,
    RUNTIME_MLX,
    LlamaCppRuntime,
    MlxRuntime,
    _extract_json,
    get_runtime,
    select_runtime,
)


class TestSelectRuntime:
    def test_apple_silicon_prefers_mlx_when_available(self):
        assert select_runtime(machine="arm64", mlx_available=True) == RUNTIME_MLX

    def test_apple_silicon_falls_back_to_llamacpp_without_mlx(self):
        assert select_runtime(machine="arm64", mlx_available=False) == RUNTIME_LLAMACPP

    def test_aarch64_treated_as_apple_silicon(self):
        assert select_runtime(machine="aarch64", mlx_available=True) == RUNTIME_MLX

    def test_intel_always_llamacpp(self):
        # Even if mlx were somehow reported available, x86_64 has no MLX build.
        assert select_runtime(machine="x86_64", mlx_available=True) == RUNTIME_LLAMACPP

    def test_case_insensitive_machine(self):
        assert select_runtime(machine="ARM64", mlx_available=True) == RUNTIME_MLX

    def test_falls_back_when_mlx_import_probe_fails(self, monkeypatch):
        # arm64 host but the probe says mlx_lm is not importable → llama.cpp.
        monkeypatch.setattr(runtime, "_mlx_importable", lambda: False)
        assert select_runtime(machine="arm64") == RUNTIME_LLAMACPP


class TestGetRuntime:
    def test_returns_mlx_adapter(self):
        assert isinstance(get_runtime(RUNTIME_MLX), MlxRuntime)

    def test_returns_llamacpp_adapter(self):
        assert isinstance(get_runtime(RUNTIME_LLAMACPP), LlamaCppRuntime)

    def test_unknown_runtime_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown runtime"):
            get_runtime("gemini")


class TestExtractJson:
    def test_plain_object(self):
        assert _extract_json('{"tasks": []}') == {"tasks": []}

    def test_strips_markdown_fence(self):
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_finds_object_in_chatty_output(self):
        text = 'Here are the tasks:\n{"tasks": [{"name": "x"}]}\nHope that helps!'
        assert _extract_json(text) == {"tasks": [{"name": "x"}]}

    def test_returns_none_on_garbage(self):
        assert _extract_json("not json at all") is None

    def test_returns_none_on_empty(self):
        assert _extract_json("") is None
        assert _extract_json(None) is None

    def test_rejects_non_object_json(self):
        # A bare array is valid JSON but not the object shape we expect.
        assert _extract_json("[1, 2, 3]") is None


class TestAdapterGenerate:
    """Adapter parse/retry logic via an injected fake backend (no native lib)."""

    def test_mlx_returns_dict_on_valid_backend_output(self):
        rt = MlxRuntime(load=lambda mp: "h", gen=lambda h, p: '{"tasks": [{"name": "Fix login"}]}')
        assert rt.generate("model", "prompt") == {"tasks": [{"name": "Fix login"}]}

    def test_mlx_returns_none_on_backend_error(self):
        # gen returning None signals a generation error.
        rt = MlxRuntime(load=lambda mp: "h", gen=lambda h, p: None)
        assert rt.generate("model", "prompt") is None

    def test_mlx_load_failure_returns_none_without_generating(self):
        gen_calls = {"n": 0}

        def gen(_h, _p):
            gen_calls["n"] += 1
            return "{}"

        rt = MlxRuntime(load=lambda mp: None, gen=gen)  # load unavailable → None
        assert rt.generate("model", "prompt") is None
        assert gen_calls["n"] == 0  # never generate when the model didn't load

    def test_mlx_loads_once_and_retries_generation_only(self):
        # A parse miss retries GENERATION without reloading the model.
        load_calls = {"n": 0}
        outputs = iter(["not json", "```json\n{\"tasks\": []}\n```"])

        def load(_mp):
            load_calls["n"] += 1
            return "handle"

        rt = MlxRuntime(load=load, gen=lambda h, p: next(outputs))
        assert rt.generate("model", "prompt") == {"tasks": []}
        assert load_calls["n"] == 1  # loaded once, not per retry

    def test_mlx_gives_up_after_max_attempts_of_bad_json(self):
        rt = MlxRuntime(load=lambda mp: "h", gen=lambda h, p: "still not json")
        assert rt.generate("model", "prompt") is None

    def test_mlx_does_not_retry_on_generation_error(self):
        calls = {"n": 0}

        def gen(_h, _p):
            calls["n"] += 1
            return None

        assert MlxRuntime(load=lambda mp: "h", gen=gen).generate("model", "prompt") is None
        assert calls["n"] == 1  # None is a hard error, not a retryable parse miss

    def test_llamacpp_returns_dict_on_valid_backend_output(self):
        rt = LlamaCppRuntime(raw_generate=lambda p, prompt: '{"tasks": []}')
        assert rt.generate("model", "prompt") == {"tasks": []}

    def test_llamacpp_returns_none_on_backend_error(self):
        rt = LlamaCppRuntime(raw_generate=lambda p, prompt: None)
        assert rt.generate("model", "prompt") is None


class TestGrammarBuild:
    def test_json_schema_compiles_to_grammar(self):
        # Grammar build needs llama-cpp-python (an opt-in extra); skip when absent.
        pytest.importorskip("llama_cpp")
        from screencap.segmentation.local_model.runtime import build_json_grammar
        from screencap.segmentation.schema import _RESPONSE_SCHEMA

        grammar = build_json_grammar(_RESPONSE_SCHEMA)  # must not raise
        assert grammar is not None


# ---------------------------------------------------------------------------
# Free-form text generation (SCR-243, U10/KTD8) — grammar-free, no JSON parse.
# ---------------------------------------------------------------------------


class TestAdapterGenerateText:
    def test_mlx_returns_raw_text(self):
        rt = MlxRuntime(load=lambda mp: "h", gen=lambda h, p: "You edited main.py.")
        assert rt.generate_text("model", "prompt") == "You edited main.py."

    def test_mlx_does_not_json_parse(self):
        # Free-form prose that is NOT JSON must pass through unchanged (unlike generate()).
        rt = MlxRuntime(load=lambda mp: "h", gen=lambda h, p: "just prose, no braces")
        assert rt.generate_text("model", "prompt") == "just prose, no braces"

    def test_mlx_load_failure_returns_none(self):
        rt = MlxRuntime(load=lambda mp: None, gen=lambda h, p: "x")
        assert rt.generate_text("model", "prompt") is None

    def test_mlx_empty_output_returns_none(self):
        rt = MlxRuntime(load=lambda mp: "h", gen=lambda h, p: "   ")
        assert rt.generate_text("model", "prompt") is None

    def test_mlx_gen_error_returns_none(self):
        rt = MlxRuntime(load=lambda mp: "h", gen=lambda h, p: None)
        assert rt.generate_text("model", "prompt") is None

    def test_llamacpp_returns_raw_text(self):
        rt = LlamaCppRuntime(raw_text_generate=lambda mp, p: "You ran pytest.")
        assert rt.generate_text("model", "prompt") == "You ran pytest."

    def test_llamacpp_empty_returns_none(self):
        assert LlamaCppRuntime(raw_text_generate=lambda mp, p: "").generate_text("m", "p") is None

    def test_llamacpp_none_returns_none(self):
        assert LlamaCppRuntime(raw_text_generate=lambda mp, p: None).generate_text("m", "p") is None


class _FakeRuntime:
    def __init__(self, tasks=None, text=None):
        self._tasks, self._text = tasks, text

    def generate(self, model_path, prompt):
        return self._tasks

    def generate_text(self, model_path, prompt):
        return self._text


class TestWorkerModeRouting:
    def _run(self, monkeypatch, request: dict, fake: _FakeRuntime) -> dict:
        import io
        import json

        from screencap.segmentation.local_model import runtime as rt_mod
        from screencap.segmentation.local_model import worker

        monkeypatch.setattr(rt_mod, "select_runtime", lambda: "mlx")
        monkeypatch.setattr(rt_mod, "get_runtime", lambda name: fake)
        out = io.StringIO()
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
        monkeypatch.setattr("sys.stdout", out)
        worker.main([])
        return json.loads(out.getvalue())

    def test_generate_text_mode_emits_text_result(self, monkeypatch):
        env = self._run(
            monkeypatch,
            {"mode": "generate_text", "prompt": "p", "model_path": "m"},
            _FakeRuntime(text="hi there"),
        )
        assert env == {"status": "ok", "result": "hi there"}

    def test_absent_mode_defaults_to_segment(self, monkeypatch):
        # Backward compatibility: the existing request shape (no "mode") still
        # routes to generate() and returns the tasks dict unchanged.
        env = self._run(
            monkeypatch,
            {"prompt": "p", "model_path": "m"},
            _FakeRuntime(tasks={"tasks": []}),
        )
        assert env == {"status": "ok", "result": {"tasks": []}}

    def test_generate_text_none_is_unavailable(self, monkeypatch):
        env = self._run(
            monkeypatch,
            {"mode": "generate_text", "prompt": "p", "model_path": "m"},
            _FakeRuntime(text=None),
        )
        assert env["status"] == "unavailable"
