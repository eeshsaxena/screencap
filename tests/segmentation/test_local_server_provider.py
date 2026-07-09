"""Bring-your-own local-server backend (U7, R4/R5, SCR-239).

Drives the provider against an injected ``raw_call`` (no live server): validated
output, connection-failure → unavailable, the fail-closed strip gate, sanitizer +
confidence-gate parity with the downloaded path, and the localhost→127.0.0.1 pin.
"""

from __future__ import annotations

import json
import sys

import pytest

from screencap.segmentation.provider import (
    PROVIDER_UNAVAILABLE,
    LLMProvider,
    get_provider,
)
from screencap.segmentation.providers.local_server import LocalServerProvider, _pin_localhost


def _stripped() -> dict:
    return {
        "stripped": True,
        "summary": {"timeline": [{"t": "0:00:00", "app": "VS Code"}]},
        "session_start": 1000.0,
        "session_end": 4600.0,
        "time_map": {"0:00:00": 1000.0, "0:30:00": 2800.0},
    }


def _raw_result() -> dict:
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


class TestSegment:
    def test_valid_response_returns_validated_tasks(self):
        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234", raw_call=lambda ep, p: _raw_result()
        )
        result = provider.segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"
        assert result["tasks"][0]["start_ts"] == 1000.0

    def test_connection_failure_is_unavailable(self):
        # raw_call None = couldn't run → unavailable so a LOCAL endpoint degrades.
        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234", raw_call=lambda ep, p: None
        )
        assert provider.segment(_stripped()) is PROVIDER_UNAVAILABLE

    def test_no_endpoint_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("SCREENCAP_LOCAL_SERVER_ENDPOINT", raising=False)
        provider = LocalServerProvider(endpoint=None, raw_call=lambda ep, p: _raw_result())
        assert provider.segment(_stripped()) is PROVIDER_UNAVAILABLE

    def test_empty_tasks_is_none(self):
        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234",
            raw_call=lambda ep, p: {"tasks": [], "summary": {}, "tags": []},
        )
        assert provider.segment(_stripped()) is None


@pytest.mark.privacy
class TestPrivacy:
    def test_unmarked_summary_refused(self):
        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234", raw_call=lambda ep, p: _raw_result()
        )
        data = _stripped()
        del data["stripped"]
        assert provider.segment(data) is PROVIDER_UNAVAILABLE

    def test_injected_name_is_sanitized(self):
        bad = _raw_result()
        bad["tasks"][0]["name"] = "Fix <script>x</script>\x07login"
        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234", raw_call=lambda ep, p: bad
        )
        result = provider.segment(_stripped())
        assert "<script>" not in result["tasks"][0]["name"]
        assert "\x07" not in result["tasks"][0]["name"]

    def test_low_confidence_is_blanked(self):
        low = _raw_result()
        low["tasks"][0]["confidence"] = "low"
        provider = LocalServerProvider(
            endpoint="http://127.0.0.1:1234", raw_call=lambda ep, p: low
        )
        result = provider.segment(_stripped())
        assert result["tasks"][0]["name"] == ""

    def test_remote_endpoint_refused_at_send_time_without_egress(self):
        # The provider re-classifies the exact endpoint it is about to POST (KTD8):
        # a REMOTE value resolved from config must never reach raw_call.
        called = {"n": 0}

        def raw(ep, p):
            called["n"] += 1
            return _raw_result()

        provider = LocalServerProvider(endpoint="http://evil.example.com:1234", raw_call=raw)
        assert provider.segment(_stripped()) is PROVIDER_UNAVAILABLE
        assert called["n"] == 0  # nothing egressed off-box

    def test_endpoint_passed_to_raw_call_has_localhost_pinned(self):
        seen = {}

        def raw(ep, p):
            seen["endpoint"] = ep
            return _raw_result()

        LocalServerProvider(endpoint="http://localhost:1234/v1", raw_call=raw).segment(
            _stripped()
        )
        assert seen["endpoint"] == "http://127.0.0.1:1234/v1"


class TestPinAndFactory:
    def test_pin_localhost_rewrites_only_localhost(self):
        assert _pin_localhost("http://localhost:11434") == "http://127.0.0.1:11434"
        assert _pin_localhost("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
        assert _pin_localhost("http://192.168.1.5:11434") == "http://192.168.1.5:11434"

    def test_factory_wires_local_server(self):
        provider = get_provider("local-server")
        assert isinstance(provider, LocalServerProvider)
        assert isinstance(provider, LLMProvider)


class TestDefaultRawCall:
    """The live ``_default_raw_call`` path (URL de-dup + streaming size cap) via a
    stubbed ``requests`` module — not otherwise exercised (tests inject ``raw_call``)."""

    @staticmethod
    def _fake_requests(*, status: int, chunks: list[bytes]):
        import types

        captured: dict = {}

        class _Resp:
            status_code = status

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_content(self, chunk_size: int = 0):
                yield from chunks

        def post(url, **kw):
            captured["url"] = url
            captured["kwargs"] = kw
            return _Resp()

        return types.SimpleNamespace(post=post), captured

    @staticmethod
    def _ok_body() -> bytes:
        inner = json.dumps({"tasks": [], "summary": {}, "tags": []})
        return json.dumps({"choices": [{"message": {"content": inner}}]}).encode()

    def test_v1_base_url_is_not_doubled(self, monkeypatch):
        fake, captured = self._fake_requests(status=200, chunks=[self._ok_body()])
        monkeypatch.setitem(sys.modules, "requests", fake)

        out = LocalServerProvider._default_raw_call("http://127.0.0.1:1234/v1", "p")
        assert out == {"tasks": [], "summary": {}, "tags": []}
        assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
        # KTD8 connect-time guarantees still hold on the streaming path.
        assert captured["kwargs"]["allow_redirects"] is False
        assert captured["kwargs"]["stream"] is True

    def test_bare_base_url_gets_v1(self, monkeypatch):
        fake, captured = self._fake_requests(status=200, chunks=[self._ok_body()])
        monkeypatch.setitem(sys.modules, "requests", fake)

        LocalServerProvider._default_raw_call("http://127.0.0.1:1234/", "p")
        assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"

    def test_oversized_body_is_rejected(self, monkeypatch):
        from screencap.segmentation.providers import local_server

        big = b"x" * (local_server._MAX_RESPONSE_BYTES + 1)
        fake, _ = self._fake_requests(status=200, chunks=[big])
        monkeypatch.setitem(sys.modules, "requests", fake)

        assert LocalServerProvider._default_raw_call("http://127.0.0.1:1234", "p") is None


# ===========================================================================
# Free-form answer path (SCR-243, U9)
# ===========================================================================

from screencap.segmentation.generation import Evidence  # noqa: E402


def _ev(text: str = "you edited main.py", stripped: bool = True) -> Evidence:
    return Evidence(text=text, stripped=stripped)


class TestAnswer:
    def test_local_endpoint_returns_sanitized_text(self):
        p = LocalServerProvider(
            endpoint="http://127.0.0.1:1234",
            answer_raw_call=lambda ep, prompt: "You edited <b>main.py</b>.",
        )
        out = p.answer("what did I do?", _ev())
        assert "<" not in out and ">" not in out  # markup neutralized (KTD10)
        assert "main.py" in out

    @pytest.mark.privacy
    def test_unmarked_evidence_refused_without_call(self):
        calls: list = []
        p = LocalServerProvider(
            endpoint="http://127.0.0.1:1234",
            answer_raw_call=lambda ep, prompt: (calls.append(prompt), "x")[1],
        )
        assert p.answer("q", _ev(stripped=False)) is PROVIDER_UNAVAILABLE
        assert calls == []

    @pytest.mark.privacy
    def test_remote_endpoint_never_egresses(self):
        # A REMOTE endpoint must fail closed at send time — evidence never leaves box.
        calls: list = []
        p = LocalServerProvider(
            endpoint="http://evil.example.com:1234",
            answer_raw_call=lambda ep, prompt: (calls.append(prompt), "x")[1],
        )
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE
        assert calls == []

    def test_no_endpoint_is_unavailable(self, monkeypatch):
        from screencap import config

        monkeypatch.setattr(config, "get_local_server_endpoint", lambda: None)
        p = LocalServerProvider(endpoint=None, answer_raw_call=lambda ep, prompt: "x")
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_none_from_raw_call_is_unavailable(self):
        p = LocalServerProvider(
            endpoint="http://127.0.0.1:1234", answer_raw_call=lambda ep, prompt: None
        )
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_empty_answer_is_unavailable(self):
        p = LocalServerProvider(
            endpoint="http://127.0.0.1:1234", answer_raw_call=lambda ep, prompt: "   "
        )
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE
