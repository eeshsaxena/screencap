"""Bring-your-own local-server backend (U7, R4/R5, SCR-239).

Drives the provider against an injected ``raw_call`` (no live server): validated
output, connection-failure → unavailable, the fail-closed strip gate, sanitizer +
confidence-gate parity with the downloaded path, and the localhost→127.0.0.1 pin.
"""

from __future__ import annotations

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
