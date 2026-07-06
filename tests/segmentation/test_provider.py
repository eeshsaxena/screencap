"""Tests for the pluggable LLM provider interface + Gemini backend (U2).

Covers:
- ``config.get_llm_provider`` selection precedence (env > config.toml > default).
- ``GeminiProvider.segment``: validated tasks on a mocked raw response;
  ``None`` (not raise) on raw-call failure; ``None`` on output that fails
  validation.
- The ``get_provider`` factory: name→backend mapping, the on-device
  ``NotImplementedError`` placeholder, and a clear error for an unknown name.
- Import-lightness: importing the interface + backend modules pulls no ``google``
  SDK (backends import ``genai`` lazily).

These fixtures are local to this file — the shared ``_fixtures.py`` is off-limits.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from screencap.segmentation.provider import LLMProvider, get_provider
from screencap.segmentation.providers.gemini import GeminiProvider


# ---------------------------------------------------------------------------
# Fixtures — a minimal activity-data dict as ``build_activity_summary`` returns.
# ---------------------------------------------------------------------------

def _activity_data() -> dict:
    """Full activity-data dict: ``summary`` + bounds + ``time_map``.

    Session spans Unix [1000, 4600] (3600s). ``time_map`` maps the relative
    timestamps the LLM emits back to Unix; entries absent from the map fall back
    to ``session_start + _parse_relative_time(rel)`` inside the validator.
    """
    return {
        "summary": {
            "duration": "1h 0m 0s",
            "timeline": [
                {"time": "0:00:00", "app": "VS Code", "cat": "CODE",
                 "title": "main.py"},
            ],
            "transcript": [],
        },
        "entries": [],
        "session_start": 1000.0,
        "session_end": 4600.0,
        "time_map": {
            "0:00:00": 1000.0,
            "0:30:00": 2800.0,
            "1:00:00": 4600.0,
        },
    }


def _raw_llm_output() -> dict:
    """A well-formed raw model response (relative timestamps, pre-validation)."""
    return {
        "tasks": [
            {"start_time": "0:00:00", "end_time": "0:30:00",
             "name": "Implement auth", "description": "Wrote auth.py.",
             "category": "development", "apps_used": ["VS Code"],
             "confidence": "high"},
            {"start_time": "0:30:00", "end_time": "1:00:00",
             "name": "Coordinate review", "description": "Pinged team on Slack.",
             "category": "communication", "apps_used": ["Slack"],
             "confidence": "medium"},
        ],
        "summary": {
            "overview": "Built auth then coordinated review.",
            "primary_focus": "development",
            "time_breakdown": {"development": 50, "communication": 50},
            "key_accomplishments": ["Shipped auth"],
        },
        "tags": ["python", "auth"],
    }


# ---------------------------------------------------------------------------
# config.get_llm_provider — selection precedence
# ---------------------------------------------------------------------------

class TestGetLlmProvider:
    def test_default_is_on_device(self):
        import screencap.config as cfg
        from screencap.config import get_llm_provider

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_LLM_PROVIDER"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {}
            assert get_llm_provider() == "on-device"

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_llm_provider

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_LLM_PROVIDER"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"llm_provider": "gemini"}
            assert get_llm_provider() == "gemini"

    def test_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_llm_provider

        with mock.patch.dict(os.environ, {"SCREENCAP_LLM_PROVIDER": "gemini"}):
            cfg._config_cache = {"llm_provider": "on-device"}
            assert get_llm_provider() == "gemini"

    def test_env_whitespace_stripped(self):
        from screencap.config import get_llm_provider

        with mock.patch.dict(os.environ, {"SCREENCAP_LLM_PROVIDER": "  gemini  "}):
            assert get_llm_provider() == "gemini"


# ---------------------------------------------------------------------------
# GeminiProvider.segment — happy path + error paths (raw call injected)
# ---------------------------------------------------------------------------

class TestGeminiSegment:
    def test_returns_validated_tasks(self):
        """Mocked raw response → validated tasks with relative→Unix conversion."""
        provider = GeminiProvider(raw_call=lambda prompt: _raw_llm_output())
        result = provider.segment(_activity_data())

        assert result is not None
        assert len(result["tasks"]) == 2
        # Validator converts relative → Unix using the time_map.
        assert result["tasks"][0]["start_ts"] == 1000.0
        assert result["tasks"][0]["end_ts"] == 2800.0
        assert result["tasks"][0]["name"] == "Implement auth"
        # Validation adds derived_name / rest_after_s and normalizes tags.
        assert result["tasks"][0]["derived_name"] == "implement-auth"
        assert result["summary"]["primary_focus"] == "development"
        assert result["tags"] == ["python", "auth"]

    def test_prompt_receives_summary_subdict(self):
        """The prompt is formatted from ``activity_summary['summary']`` only."""
        seen = {}

        def _capture(prompt):
            seen["prompt"] = prompt
            return _raw_llm_output()

        GeminiProvider(raw_call=_capture).segment(_activity_data())
        # The compact timeline (summary) is in the prompt; internal bounds are not.
        assert "0:00:00" in seen["prompt"]
        assert "VS Code" in seen["prompt"]
        assert "session_start" not in seen["prompt"]

    def test_raw_call_returns_none_yields_none(self):
        """Raw model failure (None) propagates as None — no raise."""
        provider = GeminiProvider(raw_call=lambda prompt: None)
        assert provider.segment(_activity_data()) is None

    def test_validation_crash_is_swallowed(self):
        """A malformed raw shape that crashes the validator → None, not raise.

        A non-dict task item makes ``validate_llm_tasks`` raise ``TypeError``
        (``"start_time" not in 42``); ``segment`` wraps validation in
        try/except and returns None instead of propagating.
        """
        provider = GeminiProvider(raw_call=lambda prompt: {"tasks": [42]})
        assert provider.segment(_activity_data()) is None

    def test_invalid_output_fails_validation_yields_none(self):
        """Raw output that fails validation (empty tasks) → None."""
        provider = GeminiProvider(raw_call=lambda prompt: {"tasks": [], "summary": {}})
        assert provider.segment(_activity_data()) is None

    def test_overlapping_tasks_rejected(self):
        """Overlapping tasks are rejected by the validator → None."""
        bad = {
            "tasks": [
                {"start_time": "0:00:00", "end_time": "0:30:00", "name": "A",
                 "description": "d", "category": "development",
                 "apps_used": [], "confidence": "high"},
                {"start_time": "0:10:00", "end_time": "0:40:00", "name": "B",
                 "description": "d", "category": "development",
                 "apps_used": [], "confidence": "high"},
            ],
            "summary": {},
            "tags": [],
        }
        provider = GeminiProvider(raw_call=lambda prompt: bad)
        assert provider.segment(_activity_data()) is None


class TestGeminiDefaultRawCall:
    """The default (live) backend swallows API/SDK failures → None, never raises.

    ``google.genai`` is not installed in CI/dev, so ``_call_gemini`` hits its
    ``ImportError`` branch — the point being that a real model/API failure never
    escapes as an exception; ``segment`` yields ``None`` and callers fall back.
    """

    def test_default_backend_missing_sdk_returns_none(self):
        provider = GeminiProvider()  # default raw_call = live _call_gemini
        with mock.patch.dict(os.environ, {"GOOGLE_GENAI_API_KEY": "k"}):
            # Missing SDK → _call_gemini returns None → segment returns None.
            assert provider.segment(_activity_data()) is None

    def test_call_gemini_returns_none_not_raises(self):
        """The raw call itself returns None on any failure, never propagates."""
        provider = GeminiProvider()
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_GENAI_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert provider._call_gemini("prompt") is None


# ---------------------------------------------------------------------------
# get_provider factory
# ---------------------------------------------------------------------------

class TestGetProviderFactory:
    def test_gemini_maps_to_gemini_provider(self):
        provider = get_provider("gemini")
        assert isinstance(provider, GeminiProvider)
        # Structurally satisfies the interface.
        assert isinstance(provider, LLMProvider)

    def test_on_device_raises_not_implemented(self):
        with pytest.raises(NotImplementedError) as exc:
            get_provider("on-device")
        assert "on-device" in str(exc.value)
        assert "U5" in str(exc.value)

    def test_unknown_name_raises_value_error(self):
        with pytest.raises(ValueError) as exc:
            get_provider("totally-bogus")
        assert "totally-bogus" in str(exc.value)


# ---------------------------------------------------------------------------
# Import-lightness — the interface + backend stay cloud-free at import time.
# ---------------------------------------------------------------------------

class TestImportLightness:
    def test_provider_modules_do_not_import_genai(self):
        """Importing the interface + backend must not import the Gemini SDK.

        Backends import ``google.genai`` lazily inside their methods, so it must
        be absent from ``sys.modules`` right after import. (The bare ``google``
        namespace package is registered at interpreter startup by unrelated
        deps, so we assert on ``google.genai`` specifically.) Run in a clean
        subprocess so cross-test imports don't mask a stray import.
        """
        import subprocess
        import sys

        code = (
            "import sys; "
            "import screencap.segmentation.provider; "
            "import screencap.segmentation.providers.gemini; "
            "assert 'google.genai' not in sys.modules, "
            "'google.genai imported at provider import time'; "
            "print('OK')"
        )
        env = dict(os.environ)
        # Ensure the src layout is importable in the subprocess.
        src_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "src",
        )
        env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout
