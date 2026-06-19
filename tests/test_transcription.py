"""Unit tests for ``screencap.transcription`` backend resolution (SCR-32).

Backend detection and local-model selection were extracted out of the CLI so
they can be exercised directly here, without a ``CliRunner`` round trip.
"""

from __future__ import annotations

from screencap import transcription


def _make_faster_whisper_model(hf_cache, name):
    (hf_cache / f"models--Systran--faster-whisper-{name}").mkdir(parents=True)


def _make_whisper_pt(xdg_cache, stem):
    whisper_dir = xdg_cache / "whisper"
    whisper_dir.mkdir(parents=True, exist_ok=True)
    (whisper_dir / f"{stem}.pt").write_bytes(b"")


def test_detect_cached_models_ranks_best_first_and_filters_unknown(tmp_path, monkeypatch):
    hf_cache = tmp_path / "hf"
    hf_cache.mkdir()
    xdg_cache = tmp_path / "xdg"
    xdg_cache.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hf_cache))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    monkeypatch.delenv("HF_HOME", raising=False)

    _make_faster_whisper_model(hf_cache, "base")  # faster-whisper backend
    _make_faster_whisper_model(hf_cache, "gigantic")  # not a known model → ignored
    _make_whisper_pt(xdg_cache, "small")  # openai-whisper backend
    _make_whisper_pt(xdg_cache, "tiny.en")  # locale-suffixed filename → stem parsed to "tiny"

    # Rank order is large > medium > small > base > tiny.
    assert transcription._detect_cached_models() == ["small", "base", "tiny"]


def test_detect_cached_models_empty_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "missing-hf"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "missing-xdg"))
    monkeypatch.delenv("HF_HOME", raising=False)

    assert transcription._detect_cached_models() == []


def test_select_local_model_explicit_skips_detection(monkeypatch):
    monkeypatch.setattr(transcription, "_ensure_whisper_backend", lambda: None)

    def _should_not_run():
        raise AssertionError("explicit --model must not consult the model cache")

    monkeypatch.setattr(transcription, "_detect_cached_models", _should_not_run)

    assert transcription._select_local_model("medium") == ("local", "medium")


def test_select_local_model_autopicks_best_cached(monkeypatch):
    monkeypatch.setattr(transcription, "_ensure_whisper_backend", lambda: None)
    monkeypatch.setattr(transcription, "_detect_cached_models", lambda: ["base", "tiny"])

    assert transcription._select_local_model() == ("local", "base")
