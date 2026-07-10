"""Tests for the answer/generation seam across the provider backends (U1).

The recall-answer capability is net-new on every backend: a
``answer(prompt, evidence) -> str | PROVIDER_UNAVAILABLE`` call that mirrors
``segment()``'s contract — never raises for an ordinary model/API error, and
returns the DISTINCT ``PROVIDER_UNAVAILABLE`` sentinel (never an exception) when
the backend cannot run at all. These tests drive the Python side against
injected/mock clients — no real network or model calls, Vision-free.

Covers:
- ``get_answer_provider`` selection precedence (env > config.toml > default).
- Each backend's ``answer``: text on a mocked success; the unavailable sentinel
  (not an exception) on a mocked backend failure.
- On-device ``answer`` reports unavailable (SCR-243 helper generation path is
  absent) and keeps the fail-closed ``stripped`` gate.
- The degradation resolver routes a ``str | PROVIDER_UNAVAILABLE`` answer with
  the SAME consent/sentinel logic — on-device-unavailable + consented recall
  degrades to cloud rather than raising.

These fixtures are local to this file — the shared ``_fixtures.py`` is off-limits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from screencap.segmentation.consent import (
    ConsentPolicy,
    ExecutionTarget,
    TaskKind,
)
from screencap.segmentation.degrade import DegradeAction, resolve_answer
from screencap.segmentation.provider import (
    PROVIDER_UNAVAILABLE,
    AnswerProvider,
    get_answer_provider,
)

# ---------------------------------------------------------------------------
# Fixtures — a minimal evidence bundle + prompt.
# ---------------------------------------------------------------------------

_PROMPT = "Answer only from the evidence. What was the refund error?"


def _stripped_evidence() -> dict:
    """A stripped evidence bundle (as U3 hands the dispatcher), ALLOW-only."""
    return {
        "stripped": True,
        "snippets": [
            {"text": "Refund failed: gateway timeout", "recording": "rec-1",
             "timestamp_ms": 1000},
        ],
        "figures": [],
    }


# ---------------------------------------------------------------------------
# config.get_answer_provider — selection precedence (env > toml > default)
# ---------------------------------------------------------------------------

class TestAnswerProviderSelection:
    def test_default_is_on_device(self):
        import screencap.config as cfg
        from screencap.segmentation.providers.ondevice import OnDeviceProvider

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_LLM_PROVIDER"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {}
            provider = get_answer_provider("on-device")
            assert isinstance(provider, OnDeviceProvider)
            assert isinstance(provider, AnswerProvider)

    def test_env_selects_gemini(self):
        from screencap.segmentation.providers.gemini import GeminiProvider

        provider = get_answer_provider("gemini")
        assert isinstance(provider, GeminiProvider)
        assert isinstance(provider, AnswerProvider)

    def test_unknown_name_raises_value_error(self):
        with pytest.raises(ValueError) as exc:
            get_answer_provider("totally-bogus")
        assert "totally-bogus" in str(exc.value)

    def test_selection_from_config_getter(self):
        """The active provider resolves env > config > default via config."""
        import screencap.config as cfg
        from screencap.config import get_llm_provider

        with mock.patch.dict(os.environ, {"SCREENCAP_LLM_PROVIDER": "gemini"}):
            cfg._config_cache = {"llm_provider": "on-device"}
            name = get_llm_provider()
            assert name == "gemini"
            assert isinstance(get_answer_provider(name), AnswerProvider)


# ---------------------------------------------------------------------------
# Gemini answer — text on success, sentinel (not raise) on failure
# ---------------------------------------------------------------------------

class TestGeminiAnswer:
    def test_returns_text_on_mocked_success(self):
        provider_cls = _gemini()
        provider = provider_cls(raw_answer=lambda prompt: "The refund error was a gateway timeout.")
        result = provider.answer(_PROMPT, _stripped_evidence())
        assert result == "The refund error was a gateway timeout."

    def test_backend_failure_returns_sentinel_not_exception(self):
        provider_cls = _gemini()
        provider = provider_cls(raw_answer=lambda prompt: None)
        result = provider.answer(_PROMPT, _stripped_evidence())
        assert result is PROVIDER_UNAVAILABLE

    def test_raw_answer_exception_does_not_propagate(self):
        provider_cls = _gemini()

        def _boom(prompt):
            raise RuntimeError("network down")

        provider = provider_cls(raw_answer=_boom)
        # Never raises for an ordinary model/API error.
        result = provider.answer(_PROMPT, _stripped_evidence())
        assert result is PROVIDER_UNAVAILABLE

    def test_default_backend_missing_sdk_returns_sentinel(self):
        provider_cls = _gemini()
        provider = provider_cls()  # default raw_answer = live call
        with mock.patch.dict(os.environ, {"GOOGLE_GENAI_API_KEY": "k"}):
            # Missing SDK → unavailable, never an exception.
            assert provider.answer(_PROMPT, _stripped_evidence()) is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# On-device answer — SCR-243 generation path absent → unavailable
# ---------------------------------------------------------------------------

class TestOnDeviceAnswer:
    def test_reports_unavailable_helper_generation_absent(self):
        from screencap.segmentation.providers.ondevice import OnDeviceProvider

        result = OnDeviceProvider().answer(_PROMPT, _stripped_evidence())
        # SCR-243 generation path does not exist yet: unavailable, not a crash.
        assert result is PROVIDER_UNAVAILABLE

    def test_fail_closed_on_unmarked_evidence(self):
        from screencap.segmentation.providers.ondevice import OnDeviceProvider

        unmarked = {"snippets": []}  # no stripped=True marker
        result = OnDeviceProvider().answer(_PROMPT, unmarked)
        assert result is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Downloaded answer — worker route, text on success / sentinel on failure
# ---------------------------------------------------------------------------

def _write_worker(tmp_path: Path, body: str) -> Path:
    import stat

    script = tmp_path / "fake_answer_worker.py"
    script.write_text("#!" + sys.executable + "\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


class TestDownloadedAnswer:
    def test_returns_text_on_mocked_worker_success(self, tmp_path, monkeypatch):
        from screencap.segmentation.providers.downloaded import DownloadedProvider

        # A fake worker that echoes an answer envelope on stdout.
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'ok', 'answer': 'A gateway timeout.'}))\n"
        )
        worker = _write_worker(tmp_path, body)
        monkeypatch.setenv("SCREENCAP_DOWNLOADED_WORKER", str(worker))
        monkeypatch.setenv("SCREENCAP_LOCAL_MODEL_PATH", str(tmp_path))
        monkeypatch.setenv("SCREENCAP_DOWNLOADED_WORKER_TIMEOUT", "10")

        result = DownloadedProvider().answer(_PROMPT, _stripped_evidence())
        assert result == "A gateway timeout."

    def test_no_model_installed_returns_sentinel(self, monkeypatch):
        from screencap.segmentation.providers.downloaded import DownloadedProvider

        env = {k: v for k, v in os.environ.items()
               if k not in ("SCREENCAP_LOCAL_MODEL_PATH", "SCREENCAP_DOWNLOADED_WORKER")}
        with mock.patch.dict(os.environ, env, clear=True):
            result = DownloadedProvider().answer(_PROMPT, _stripped_evidence())
            assert result is PROVIDER_UNAVAILABLE

    def test_fail_closed_on_unmarked_evidence(self, monkeypatch):
        from screencap.segmentation.providers.downloaded import DownloadedProvider

        monkeypatch.setenv("SCREENCAP_LOCAL_MODEL_PATH", "/does/not/matter")
        result = DownloadedProvider().answer(_PROMPT, {"snippets": []})
        assert result is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Local-server answer — endpoint route, text on success / sentinel on failure
# ---------------------------------------------------------------------------

class TestLocalServerAnswer:
    def test_returns_text_on_mocked_success(self):
        from screencap.segmentation.providers.local_server import LocalServerProvider

        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234/v1",
            raw_answer=lambda endpoint, prompt: "A gateway timeout.",
        )
        result = provider.answer(_PROMPT, _stripped_evidence())
        assert result == "A gateway timeout."

    def test_backend_failure_returns_sentinel(self):
        from screencap.segmentation.providers.local_server import LocalServerProvider

        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234/v1",
            raw_answer=lambda endpoint, prompt: None,
        )
        result = provider.answer(_PROMPT, _stripped_evidence())
        assert result is PROVIDER_UNAVAILABLE

    def test_no_endpoint_returns_sentinel(self):
        from screencap.segmentation.providers.local_server import LocalServerProvider

        env = {k: v for k, v in os.environ.items()
               if k != "SCREENCAP_LOCAL_SERVER_ENDPOINT"}
        with mock.patch.dict(os.environ, env, clear=True):
            provider = LocalServerProvider(
                raw_answer=lambda endpoint, prompt: "unused",
            )
            assert provider.answer(_PROMPT, _stripped_evidence()) is PROVIDER_UNAVAILABLE

    def test_fail_closed_on_unmarked_evidence(self):
        from screencap.segmentation.providers.local_server import LocalServerProvider

        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234/v1",
            raw_answer=lambda endpoint, prompt: "leaked",
        )
        result = provider.answer(_PROMPT, {"snippets": []})
        assert result is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# resolve_answer — same consent/sentinel routing as segmentation
# ---------------------------------------------------------------------------

class TestResolveAnswer:
    def test_text_answer_is_used(self):
        policy = ConsentPolicy()
        decision = resolve_answer("A grounded answer.", policy)
        assert decision.action is DegradeAction.USE_PROVIDER
        assert decision.answer == "A grounded answer."

    def test_unavailable_with_consented_cloud_degrades_to_cloud(self):
        """On-device unavailable + recall consent + cloud provider → CLOUD, not raise."""
        policy = ConsentPolicy(
            cloud_provider="gemini",
            recall_cloud_consent=True,
        )
        decision = resolve_answer(PROVIDER_UNAVAILABLE, policy)
        assert decision.action is DegradeAction.CLOUD

    def test_unavailable_without_consent_is_none(self):
        policy = ConsentPolicy(cloud_provider="gemini", recall_cloud_consent=False)
        decision = resolve_answer(PROVIDER_UNAVAILABLE, policy)
        assert decision.action is DegradeAction.NONE

    def test_unavailable_no_cloud_provider_is_none(self):
        policy = ConsentPolicy(cloud_provider=None, recall_cloud_consent=True)
        decision = resolve_answer(PROVIDER_UNAVAILABLE, policy)
        assert decision.action is DegradeAction.NONE

    def test_routes_via_recall_answer_kind(self):
        """resolve_answer routes on TaskKind.RECALL_ANSWER (never DAY_SPLIT)."""
        policy = ConsentPolicy(cloud_provider="gemini", recall_cloud_consent=True)
        # RECALL_ANSWER with on-device unavailable + consent → CLOUD.
        assert (
            policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=False)
            is ExecutionTarget.CLOUD
        )
        assert resolve_answer(PROVIDER_UNAVAILABLE, policy).action is DegradeAction.CLOUD


# Helper: import the Gemini provider class lazily so a collection error surfaces
# in a test, not at module import.
def _gemini():
    from screencap.segmentation.providers.gemini import GeminiProvider

    return GeminiProvider
