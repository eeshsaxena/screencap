"""OpenAI API-key backend (BYO cloud, U3).

Drives :class:`~screencap.segmentation.providers.openai.OpenAIProvider` against a
**stubbed ``requests`` module** (never the real vendor API) via two paths:

* the injectable ``raw_call`` / ``answer_raw_call`` seams for the parse / validate
  / sanitize shapes (no HTTP);
* a stubbed ``sys.modules['requests']`` for the real ``_call_openai`` /
  ``_answer_openai`` path (key-source, host-pinning, header-not-URL, auth failure).

Privacy-marked, Vision-free (`@pytest.mark.privacy`, CI's only lane):
- the key is read from ``secrets.load_key`` (Keychain), not ``os.environ``, and
  never reaches the process env the backend touches;
- only text (activity_summary / Evidence.text) reaches the request body — no
  frame bytes or paths.
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
from screencap.segmentation.providers import openai as openai_mod
from screencap.segmentation.providers.openai import OpenAIProvider

_PINNED_HOST = "https://api.openai.com"


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


def _chat_body(content: str) -> bytes:
    """The OpenAI Chat Completions response envelope wrapping ``content``."""
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def _fake_requests(*, status: int, body: bytes):
    """A stub ``requests`` module capturing the POST args; returns (module, captured)."""
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
        p = OpenAIProvider(raw_call=lambda prompt: _raw_tasks())
        result = p.segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"
        assert result["tasks"][0]["start_ts"] == 1000.0

    def test_empty_valid_response_is_none(self):
        p = OpenAIProvider(raw_call=lambda prompt: {"tasks": [], "summary": {}, "tags": []})
        assert p.segment(_stripped()) is None

    def test_raw_none_is_unavailable(self):
        p = OpenAIProvider(raw_call=lambda prompt: None)
        assert p.segment(_stripped()) is PROVIDER_UNAVAILABLE


class TestAnswerSeam:
    def test_returns_sanitized_text(self):
        p = OpenAIProvider(answer_raw_call=lambda prompt: "You edited <i>main.py</i>.")
        out = p.answer("what did I do?", _ev())
        assert isinstance(out, str)
        assert "<" not in out and ">" not in out  # markup neutralized (KTD10)
        assert "main.py" in out

    def test_none_from_raw_call_is_unavailable(self):
        p = OpenAIProvider(answer_raw_call=lambda prompt: None)
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_empty_answer_is_unavailable(self):
        p = OpenAIProvider(answer_raw_call=lambda prompt: "   ")
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Real _call_openai path — key source, host pinning, auth / network failure
# ---------------------------------------------------------------------------


class TestRealCallPath:
    def test_missing_key_makes_no_http_call(self, monkeypatch):
        """A missing key returns unavailable WITHOUT importing/using requests."""
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: None)
        # A tripwire requests that fails the test if any HTTP is attempted.
        tripwire = types.SimpleNamespace(
            post=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("HTTP attempted with no key")
            )
        )
        monkeypatch.setitem(sys.modules, "requests", tripwire)
        assert OpenAIProvider().segment(_stripped()) is PROVIDER_UNAVAILABLE
        assert OpenAIProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_segment_hits_pinned_host_with_key_in_header(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-test-KEY")
        fake, captured = _fake_requests(
            status=200, body=_chat_body(json.dumps(_raw_tasks()))
        )
        monkeypatch.setitem(sys.modules, "requests", fake)

        result = OpenAIProvider().segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"
        # Pinned host, key in the Authorization header — never the URL.
        assert captured["url"].startswith(_PINNED_HOST)
        assert "sk-test-KEY" not in captured["url"]
        assert captured["kwargs"]["headers"]["Authorization"] == "Bearer sk-test-KEY"

    def test_answer_hits_pinned_host_with_key_in_header(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-answer-KEY")
        fake, captured = _fake_requests(status=200, body=_chat_body("You edited main.py."))
        monkeypatch.setitem(sys.modules, "requests", fake)

        out = OpenAIProvider().answer("what did I do?", _ev())
        assert out == "You edited main.py."
        assert captured["url"].startswith(_PINNED_HOST)
        assert "sk-answer-KEY" not in captured["url"]
        assert captured["kwargs"]["headers"]["Authorization"] == "Bearer sk-answer-KEY"

    def test_auth_failure_401_is_unavailable(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-bad")
        fake, _ = _fake_requests(status=401, body=b"{}")
        monkeypatch.setitem(sys.modules, "requests", fake)
        assert OpenAIProvider().segment(_stripped()) is PROVIDER_UNAVAILABLE
        assert OpenAIProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_network_error_is_unavailable(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-net")

        def _boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=_boom))
        assert OpenAIProvider().segment(_stripped()) is PROVIDER_UNAVAILABLE
        assert OpenAIProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_unparseable_content_is_unavailable(self, monkeypatch):
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-x")
        fake, _ = _fake_requests(status=200, body=_chat_body("this is not json {{{"))
        monkeypatch.setitem(sys.modules, "requests", fake)
        assert OpenAIProvider().segment(_stripped()) is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Privacy lane (Vision-free)
# ---------------------------------------------------------------------------


@pytest.mark.privacy
class TestPrivacy:
    def test_unmarked_summary_refused_without_call(self):
        calls: list = []
        p = OpenAIProvider(raw_call=lambda prompt: (calls.append(prompt), _raw_tasks())[1])
        data = _stripped()
        data["stripped"] = False
        assert p.segment(data) is PROVIDER_UNAVAILABLE
        assert calls == []  # fail-closed: no request on unmarked input

    def test_unmarked_evidence_refused_without_call(self):
        calls: list = []
        p = OpenAIProvider(answer_raw_call=lambda prompt: (calls.append(prompt), "x")[1])
        assert p.answer("q", _ev(stripped=False)) is PROVIDER_UNAVAILABLE
        assert calls == []

    def test_key_read_from_secrets_not_environ(self, monkeypatch):
        """The key comes from ``secrets.load_key``, never ``os.environ`` (R3)."""
        from screencap.segmentation import secrets

        seen_vendor: list = []

        def _load(vendor):
            seen_vendor.append(vendor)
            return "sk-from-keychain"

        monkeypatch.setattr(secrets, "load_key", _load)
        # An env var that must NOT be consulted as a key source.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-should-be-ignored")
        fake, captured = _fake_requests(
            status=200, body=_chat_body(json.dumps(_raw_tasks()))
        )
        monkeypatch.setitem(sys.modules, "requests", fake)

        OpenAIProvider().segment(_stripped())
        assert seen_vendor == ["openai"]  # read via the Keychain seam, per-vendor
        auth = captured["kwargs"]["headers"]["Authorization"]
        assert auth == "Bearer sk-from-keychain"
        assert "sk-env-should-be-ignored" not in auth

    def test_only_text_reaches_the_request_body(self, monkeypatch):
        """Only the stripped text evidence reaches the payload — no frame bytes/paths."""
        from screencap.segmentation import secrets

        monkeypatch.setattr(secrets, "load_key", lambda vendor: "sk-x")
        fake, captured = _fake_requests(status=200, body=_chat_body("ok answer"))
        monkeypatch.setitem(sys.modules, "requests", fake)

        OpenAIProvider().answer("what did I do?", _ev(text="SENTINEL-EVIDENCE-TEXT"))
        body = captured["kwargs"]["json"]
        serialized = json.dumps(body)
        assert "SENTINEL-EVIDENCE-TEXT" in serialized
        # No frame/image markers rode along in the body.
        for marker in (".jpg", ".png", "screenshots/", "frame", "image"):
            assert marker not in serialized.lower()


# ---------------------------------------------------------------------------
# Import-lightness + factory
# ---------------------------------------------------------------------------


class TestImportAndFactory:
    def test_requests_not_imported_at_module_import(self):
        # The default call imports requests lazily; importing the module must not.
        import subprocess

        code = (
            "import sys; "
            "import screencap.segmentation.providers.openai; "
            "assert 'requests' not in sys.modules, 'requests imported at import time'; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout

    def test_factory_wires_openai(self):
        provider = get_provider("openai")
        assert isinstance(provider, OpenAIProvider)
        assert isinstance(provider, LLMProvider)

    def test_pinned_host_constant(self):
        # Guard the pin: the host is hardcoded, not env/config-derived.
        assert openai_mod._CHAT_COMPLETIONS_URL.startswith(_PINNED_HOST + "/")
