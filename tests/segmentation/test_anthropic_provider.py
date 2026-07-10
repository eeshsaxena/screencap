"""Anthropic API-key backend (BYO cloud, U3).

Drives :class:`~screencap.segmentation.providers.anthropic.AnthropicProvider`
against a **stubbed ``requests`` module** (never the real vendor API), covering
the seam parse/validate shapes and the real ``_call_anthropic`` / ``_answer_anthropic``
path (key-source, host-pinning, ``x-api-key`` header not URL, auth failure).

Privacy-marked, Vision-free (`@pytest.mark.privacy`, CI's only lane):
- the key is read from ``secrets.load_key`` (Keychain), not ``os.environ``;
- only text reaches the request body — no frame bytes/paths.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from screencap.segmentation.generation import Evidence
from screencap.segmentation.provider import (
    PROVIDER_UNAVAILABLE,
    LLMProvider,
    get_provider,
)
from screencap.segmentation.providers import anthropic as anthropic_mod
from screencap.segmentation.providers.anthropic import AnthropicProvider

_PINNED_HOST = "https://api.anthropic.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stripped() -> dict:
    return {
        "stripped": True,
        "summary": {"timeline": [{"t": "0:00:00", "app": "VS Code"}]},
        "session_start": 1000.0,
        "session_end": 4600.0,
        "time_map": {"0:00:00": 1000.0, "0:30:00": 2800.0},
    }


def _raw_tasks() -> dict:
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


def _ev(text: str = "You edited main.py.", stripped: bool = True) -> Evidence:
    return Evidence(text=text, stripped=stripped)


def _messages_body(text: str) -> bytes:
    """The Anthropic Messages response envelope: a list of text content blocks."""
    return json.dumps({"content": [{"type": "text", "text": text}]}).encode()


def _fake_requests(*, status: int, body: bytes):
    captured: dict = {}

    class _Resp:
        status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_content(self, chunk_size: int = 0):
            yield body

    def post(url, **kw):
        captured["url"] = url
        captured["kwargs"] = kw
        return _Resp()

    return types.SimpleNamespace(post=post), captured


# ---------------------------------------------------------------------------
# segment / answer via the injected seams (no HTTP)
# ---------------------------------------------------------------------------


class TestSegmentSeam:
    def test_valid_output_yields_validated_tasks(self):
        p = AnthropicProvider(raw_call=lambda prompt: _raw_tasks())
        result = p.segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"
        assert result["tasks"][0]["start_ts"] == 1000.0

    def test_empty_valid_response_is_none(self):
        p = AnthropicProvider(raw_call=lambda prompt: {"tasks": [], "summary": {}, "tags": []})
        assert p.segment(_stripped()) is None

    def test_raw_none_is_unavailable(self):
        p = AnthropicProvider(raw_call=lambda prompt: None)
        assert p.segment(_stripped()) is PROVIDER_UNAVAILABLE


class TestAnswerSeam:
    def test_returns_sanitized_text(self):
        p = AnthropicProvider(answer_raw_call=lambda prompt: "You edited <i>main.py</i>.")
        out = p.answer("what did I do?", _ev())
        assert isinstance(out, str)
        assert "<" not in out and ">" not in out
        assert "main.py" in out

    def test_none_from_raw_call_is_unavailable(self):
        p = AnthropicProvider(answer_raw_call=lambda prompt: None)
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_empty_answer_is_unavailable(self):
        p = AnthropicProvider(answer_raw_call=lambda prompt: "   ")
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Real _call_anthropic path — key source, host pinning, auth / network failure
# ---------------------------------------------------------------------------


class TestRealCallPath:
    def test_missing_key_makes_no_http_call(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: None)
        tripwire = types.SimpleNamespace(
            post=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("HTTP attempted with no key")
            )
        )
        monkeypatch.setitem(sys.modules, "requests", tripwire)
        assert AnthropicProvider().segment(_stripped()) is PROVIDER_UNAVAILABLE
        assert AnthropicProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_segment_hits_pinned_host_with_key_in_header(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-ant-KEY")
        fake, captured = _fake_requests(
            status=200, body=_messages_body(json.dumps(_raw_tasks()))
        )
        monkeypatch.setitem(sys.modules, "requests", fake)

        result = AnthropicProvider().segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"
        # Pinned host, key in x-api-key header (never URL), version header present.
        assert captured["url"].startswith(_PINNED_HOST)
        assert "sk-ant-KEY" not in captured["url"]
        headers = captured["kwargs"]["headers"]
        assert headers["x-api-key"] == "sk-ant-KEY"
        assert headers["anthropic-version"] == anthropic_mod._ANTHROPIC_VERSION

    def test_answer_hits_pinned_host_with_key_in_header(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-ans-KEY")
        fake, captured = _fake_requests(status=200, body=_messages_body("You edited main.py."))
        monkeypatch.setitem(sys.modules, "requests", fake)

        out = AnthropicProvider().answer("what did I do?", _ev())
        assert out == "You edited main.py."
        assert captured["url"].startswith(_PINNED_HOST)
        assert captured["kwargs"]["headers"]["x-api-key"] == "sk-ans-KEY"
        assert "sk-ans-KEY" not in captured["url"]

    def test_json_fenced_content_is_unwrapped(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-x")
        fenced = "```json\n" + json.dumps(_raw_tasks()) + "\n```"
        fake, _ = _fake_requests(status=200, body=_messages_body(fenced))
        monkeypatch.setitem(sys.modules, "requests", fake)
        result = AnthropicProvider().segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"

    def test_auth_failure_401_is_unavailable(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-bad")
        fake, _ = _fake_requests(status=401, body=b"{}")
        monkeypatch.setitem(sys.modules, "requests", fake)
        assert AnthropicProvider().segment(_stripped()) is PROVIDER_UNAVAILABLE
        assert AnthropicProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_network_error_is_unavailable(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-net")

        def _boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=_boom))
        assert AnthropicProvider().segment(_stripped()) is PROVIDER_UNAVAILABLE
        assert AnthropicProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_unparseable_content_is_unavailable(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-x")
        fake, _ = _fake_requests(status=200, body=_messages_body("not json at all {{{"))
        monkeypatch.setitem(sys.modules, "requests", fake)
        assert AnthropicProvider().segment(_stripped()) is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Privacy lane (Vision-free)
# ---------------------------------------------------------------------------


@pytest.mark.privacy
class TestPrivacy:
    def test_unmarked_summary_refused_without_call(self):
        calls: list = []
        p = AnthropicProvider(raw_call=lambda prompt: (calls.append(prompt), _raw_tasks())[1])
        data = _stripped()
        data["stripped"] = False
        assert p.segment(data) is PROVIDER_UNAVAILABLE
        assert calls == []

    def test_unmarked_evidence_refused_without_call(self):
        calls: list = []
        p = AnthropicProvider(answer_raw_call=lambda prompt: (calls.append(prompt), "x")[1])
        assert p.answer("q", _ev(stripped=False)) is PROVIDER_UNAVAILABLE
        assert calls == []

    def test_key_read_from_secrets_not_environ(self, monkeypatch):
        from screencap.segmentation import secrets

        seen_vendor: list = []

        def _load(vendor):
            seen_vendor.append(vendor)
            return "sk-from-keychain"

        monkeypatch.setattr(secrets, "load_key", _load)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-should-be-ignored")
        fake, captured = _fake_requests(
            status=200, body=_messages_body(json.dumps(_raw_tasks()))
        )
        monkeypatch.setitem(sys.modules, "requests", fake)

        AnthropicProvider().segment(_stripped())
        assert seen_vendor == ["anthropic"]
        assert captured["kwargs"]["headers"]["x-api-key"] == "sk-from-keychain"
        assert "sk-env-should-be-ignored" not in json.dumps(captured["kwargs"]["headers"])

    def test_only_text_reaches_the_request_body(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-x")
        fake, captured = _fake_requests(status=200, body=_messages_body("ok answer"))
        monkeypatch.setitem(sys.modules, "requests", fake)

        AnthropicProvider().answer("what did I do?", _ev(text="SENTINEL-EVIDENCE-TEXT"))
        serialized = json.dumps(captured["kwargs"]["json"])
        assert "SENTINEL-EVIDENCE-TEXT" in serialized
        for marker in (".jpg", ".png", "screenshots/", "frame", "image"):
            assert marker not in serialized.lower()


# ---------------------------------------------------------------------------
# Import-lightness + factory
# ---------------------------------------------------------------------------


class TestImportAndFactory:
    def test_requests_not_imported_at_module_import(self):
        import subprocess

        code = (
            "import sys; "
            "import screencap.segmentation.providers.anthropic; "
            "assert 'requests' not in sys.modules, 'requests imported at import time'; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout

    def test_factory_wires_anthropic(self):
        provider = get_provider("anthropic")
        assert isinstance(provider, AnthropicProvider)
        assert isinstance(provider, LLMProvider)

    def test_pinned_host_constant(self):
        assert anthropic_mod._MESSAGES_URL.startswith(_PINNED_HOST + "/")
