"""Gemini BYO-key reconciliation (U3, R10).

The Gemini backend's key source is repointed from an ``os.environ`` read to the
shared Keychain via :func:`screencap.segmentation.secrets.load_key`. These tests
pin:

* a **stored Keychain key is used** (mocked ``secrets.load_key``) and passed to the
  SDK explicitly (``api_key=...``), so a Keychain secret never rides the process env;
* the backend does **not depend on** ``GOOGLE_GENAI_API_KEY`` being in
  ``os.environ`` — a stored key works with the env var absent;
* ``GOOGLE_GENAI_API_KEY`` survives only as a **dev override**: it is used when no
  Keychain key is stored, and the Keychain key **wins** when both are present;
* Gemini remains the single BYO-key Gemini entry (one factory id → one backend, R10).

Marked ``@pytest.mark.privacy`` (Vision-free) where a privacy invariant is asserted.
"""

from __future__ import annotations

import types

import pytest

from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, get_provider
from screencap.segmentation.providers import gemini as gemini_mod
from screencap.segmentation.providers.gemini import GeminiProvider


def _stripped() -> dict:
    return {
        "stripped": True,
        "summary": {"timeline": [{"t": "0:00:00", "app": "VS Code"}]},
        "session_start": 1000.0,
        "session_end": 4600.0,
        "time_map": {"0:00:00": 1000.0, "0:30:00": 2800.0},
    }


def _fake_genai(captured: dict):
    """A minimal stub of ``google.genai`` + ``google.genai.types``.

    ``genai.Client(api_key=...)`` records the key it was handed; ``generate_content``
    returns a fixed JSON tasks string so ``segment`` completes without a network call.
    Returns (genai_module, types_module) to inject into ``sys.modules``.
    """
    import json as _json

    tasks_json = _json.dumps(
        {
            "tasks": [
                {"start_time": "0:00:00", "end_time": "0:30:00", "name": "Fix login",
                 "description": "d", "category": "development", "apps_used": ["VS Code"],
                 "confidence": "high"},
            ],
            "summary": {"overview": "o", "primary_focus": "development",
                        "time_breakdown": {}, "key_accomplishments": []},
            "tags": ["python"],
        }
    )

    class _Models:
        def generate_content(self, **kw):
            return types.SimpleNamespace(text=tasks_json)

    class _Client:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.models = _Models()

    class _Types:
        @staticmethod
        def GenerateContentConfig(**kw):
            return kw

    gtypes = _Types()
    # ``from google.genai import types`` reads the ``types`` attribute off the
    # ``google.genai`` module object, so expose it on the genai stub too.
    genai = types.SimpleNamespace(Client=_Client, types=gtypes)
    return genai, gtypes


def _inject_genai(monkeypatch, captured: dict) -> None:
    """Inject the stub ``google.genai`` packages so the lazy import resolves them."""
    import sys

    genai, gtypes = _fake_genai(captured)
    google_pkg = types.ModuleType("google")
    google_pkg.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", gtypes)


# ---------------------------------------------------------------------------
# _load_gemini_key — Keychain wins; env is a dev-only fallback
# ---------------------------------------------------------------------------


class TestLoadGeminiKey:
    def test_keychain_key_wins_over_env(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "keychain-key")
        monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "env-key")
        assert gemini_mod._load_gemini_key() == "keychain-key"

    def test_env_used_only_when_no_keychain_key(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: None)
        monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "env-key")
        assert gemini_mod._load_gemini_key() == "env-key"

    def test_reads_gemini_vendor(self, monkeypatch):
        from screencap.segmentation import secrets

        seen: list = []
        monkeypatch.setattr(
            secrets, "load_key", lambda vendor: (seen.append(vendor), "k")[1]
        )
        gemini_mod._load_gemini_key()
        assert seen == ["gemini"]

    def test_none_when_neither_source_has_key(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: None)
        monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)
        assert gemini_mod._load_gemini_key() is None

    def test_keychain_error_falls_back_to_env(self, monkeypatch):
        from screencap.segmentation import secrets

        def _boom(vendor):
            raise RuntimeError("keychain locked")

        monkeypatch.setattr(secrets, "load_key", _boom)
        monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "env-key")
        assert gemini_mod._load_gemini_key() == "env-key"


# ---------------------------------------------------------------------------
# segment uses the stored Keychain key, no env dependency
# ---------------------------------------------------------------------------


@pytest.mark.privacy
class TestSegmentUsesKeychainKey:
    def test_stored_key_used_without_env(self, monkeypatch):
        from screencap.segmentation import secrets

        # A stored Keychain key, and NO env var — proving no env dependency.
        monkeypatch.setattr(secrets, "load_key", lambda vendor: "keychain-secret")
        monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)
        captured: dict = {}
        _inject_genai(monkeypatch, captured)

        result = GeminiProvider().segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"
        # The key was passed to the SDK explicitly (api_key=), not via the env.
        assert captured["api_key"] == "keychain-secret"

    def test_no_key_anywhere_is_none_without_env(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: None)
        monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)
        # segment returns None (ran-but-unusable) when the model is skipped.
        assert GeminiProvider().segment(_stripped()) is None


# ---------------------------------------------------------------------------
# Single-entry (R10)
# ---------------------------------------------------------------------------


@pytest.mark.privacy
class TestFailClosedStrippedGate:
    """The LOCAL BYO path (``get_provider("gemini")``) fail-closes on an
    activity summary not marked ``stripped=True`` — matching the
    openai/anthropic/cli_delegate siblings (R7/R8). The Cloud Run construction
    (``GeminiProvider()``, default) stays un-gated so its server-side, un-stripped
    activity data still segments."""

    def _unstripped(self) -> dict:
        s = _stripped()
        s.pop("stripped", None)  # no stripped marker at all
        return s

    def test_local_byo_refuses_unstripped_without_calling_model(self, monkeypatch):
        called: list = []

        def _raw(prompt):
            called.append(prompt)
            return {"tasks": []}

        # get_provider("gemini") opts into require_stripped=True. Inject a raw_call
        # spy on the SAME object to prove the model is NEVER reached on refusal.
        provider = get_provider("gemini")
        provider._raw_call = _raw  # type: ignore[attr-defined]
        assert provider.segment(self._unstripped()) is None
        assert called == []  # fail-closed: no model call on unmarked input

    def test_cloud_run_default_still_segments_unstripped(self, monkeypatch):
        # The Cloud Run caller constructs GeminiProvider(raw_call=...) with the
        # default require_stripped=False, so un-stripped activity data segments.
        provider = GeminiProvider(raw_call=lambda prompt: _raw_tasks())
        result = provider.segment(self._unstripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"


def _raw_tasks() -> dict:
    """A well-formed raw tasks object (relative timestamps, pre-validation)."""
    return {
        "tasks": [
            {"start_time": "0:00:00", "end_time": "0:30:00", "name": "Fix login",
             "description": "d", "category": "development", "apps_used": ["VS Code"],
             "confidence": "high"},
        ],
        "summary": {"overview": "o", "primary_focus": "development",
                    "time_breakdown": {}, "key_accomplishments": []},
        "tags": ["python"],
    }


class TestSingleGeminiEntry:
    def test_gemini_factory_id_maps_to_one_backend(self):
        assert isinstance(get_provider("gemini"), GeminiProvider)

    def test_answer_without_key_is_unavailable(self, monkeypatch):
        from screencap.segmentation import secrets
        from screencap.segmentation.generation import Evidence

        monkeypatch.setattr(secrets, "load_key", lambda vendor: None)
        monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)
        out = GeminiProvider().answer("q", Evidence(text="x", stripped=True))
        assert out is PROVIDER_UNAVAILABLE
