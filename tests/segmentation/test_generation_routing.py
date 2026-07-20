"""Tests for the generation chain + answer router (SCR-243, U6/U7).

U6 — ``ChainedGenerationProvider`` cascade + ``UnavailableGenerationProvider``.
U7 — ``build_answer_provider`` config routing (on-device-class only; LOCAL BYO
only; the REMOTE-exclusion privacy boundary).

These fixtures are local to this file — the shared ``_fixtures.py`` is off-limits.
"""

from __future__ import annotations

import pytest

from screencap.segmentation.generation import Evidence
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE
from screencap.segmentation.providers.chained import (
    ChainedGenerationProvider,
    UnavailableGenerationProvider,
)


def _ev() -> Evidence:
    return Evidence(text="you edited main.py", stripped=True)


class _Fixed:
    """A generation backend returning a fixed result and recording its calls."""

    def __init__(self, result, sink: list) -> None:
        self._result = result
        self._sink = sink

    def answer(self, prompt, evidence):
        self._sink.append(self)
        return self._result


# ---------------------------------------------------------------------------
# U6 — ChainedGenerationProvider
# ---------------------------------------------------------------------------


def test_first_answer_stops_chain():
    calls: list = []
    a, b = _Fixed("answer A", calls), _Fixed("answer B", calls)
    assert ChainedGenerationProvider([a, b]).answer("q", _ev()) == "answer A"
    assert calls == [a]  # b never called


def test_cascades_past_unavailable():
    calls: list = []
    a, b = _Fixed(PROVIDER_UNAVAILABLE, calls), _Fixed("answer B", calls)
    assert ChainedGenerationProvider([a, b]).answer("q", _ev()) == "answer B"
    assert calls == [a, b]


def test_all_unavailable_is_unavailable():
    calls: list = []
    chain = ChainedGenerationProvider(
        [_Fixed(PROVIDER_UNAVAILABLE, calls), _Fixed(PROVIDER_UNAVAILABLE, calls)]
    )
    assert chain.answer("q", _ev()) is PROVIDER_UNAVAILABLE


def test_empty_chain_is_unavailable():
    assert ChainedGenerationProvider([]).answer("q", _ev()) is PROVIDER_UNAVAILABLE


def test_unavailable_generation_provider_always_unavailable():
    assert UnavailableGenerationProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE


def test_chain_treats_empty_or_non_str_as_unavailable():
    # Defensive (KTD9): a misbehaving backend returning "", whitespace, or a
    # non-str must be cascaded past — never leaked as a blank answer and never
    # allowed to suppress a healthy downstream backend.
    calls: list = []

    class _Bad:
        def __init__(self, r) -> None:
            self._r = r

        def answer(self, prompt, evidence):
            calls.append(self)
            return self._r

    good = _Fixed("real answer", calls)
    assert ChainedGenerationProvider([_Bad(""), _Bad(None), good]).answer("q", _ev()) == "real answer"
    assert ChainedGenerationProvider([_Bad("")]).answer("q", _ev()) is PROVIDER_UNAVAILABLE
    assert ChainedGenerationProvider([_Bad(None)]).answer("q", _ev()) is PROVIDER_UNAVAILABLE
    assert ChainedGenerationProvider([_Bad("   ")]).answer("q", _ev()) is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# U7 — build_answer_provider (config routing; LOCAL-only BYO; REMOTE excluded)
# ---------------------------------------------------------------------------

from screencap.segmentation.providers.downloaded import DownloadedProvider  # noqa: E402
from screencap.segmentation.providers.local_server import LocalServerProvider  # noqa: E402
from screencap.segmentation.providers.ondevice import OnDeviceProvider  # noqa: E402
from screencap.segmentation.routing import build_answer_provider  # noqa: E402


@pytest.fixture()
def cfg(monkeypatch):
    """Set the active provider, BYO endpoint, and downloaded-installed flag."""
    from screencap import config
    from screencap.segmentation import routing

    def _set(provider: str, *, endpoint: str | None = None, installed: bool = False):
        monkeypatch.setattr(config, "get_llm_provider", lambda: provider)
        monkeypatch.setattr(config, "get_local_server_endpoint", lambda: endpoint)
        monkeypatch.setattr(routing, "_downloaded_model_installed", lambda: installed)

    return _set


def test_on_device_builds_afm_chain(cfg):
    cfg("on-device")
    prov = build_answer_provider()
    assert isinstance(prov, ChainedGenerationProvider)
    assert isinstance(prov._backends[0], OnDeviceProvider)
    assert len(prov._backends) == 1  # downloaded not installed


def test_on_device_chain_includes_downloaded_when_installed(cfg):
    cfg("on-device", installed=True)
    prov = build_answer_provider()
    assert isinstance(prov, ChainedGenerationProvider)
    assert any(isinstance(b, DownloadedProvider) for b in prov._backends)


def test_downloaded_active_provider(cfg):
    cfg("downloaded")
    assert isinstance(build_answer_provider(), DownloadedProvider)


def test_local_server_local_endpoint_participates(cfg):
    cfg("local-server", endpoint="http://127.0.0.1:1234")
    assert isinstance(build_answer_provider(), LocalServerProvider)


@pytest.mark.privacy
def test_local_server_remote_endpoint_excluded(cfg):
    # KTD5: a REMOTE BYO endpoint must NOT join the answer chain.
    cfg("local-server", endpoint="http://evil.example.com:1234")
    assert isinstance(build_answer_provider(), UnavailableGenerationProvider)


def test_cloud_active_provider_has_no_on_device_backend(cfg):
    cfg("gemini")
    assert isinstance(build_answer_provider(), UnavailableGenerationProvider)


# ---------------------------------------------------------------------------
# U3 — build_prose_provider (DIARY_PROSE on-device-class routing, KTD-4)
# ---------------------------------------------------------------------------

from screencap.segmentation.providers.chained import UnavailableProvider  # noqa: E402
from screencap.segmentation.routing import build_prose_provider  # noqa: E402


def test_prose_on_device_exposes_bullet_verb(cfg):
    cfg("on-device")
    prov = build_prose_provider()
    # The on-device backend exposes the block-bullet verb the consolidator calls.
    assert isinstance(prov, OnDeviceProvider)
    assert hasattr(prov, "call_block_bullets")


def test_prose_local_server_local_endpoint_participates(cfg):
    cfg("local-server", endpoint="http://127.0.0.1:1234")
    assert isinstance(build_prose_provider(), LocalServerProvider)


@pytest.mark.privacy
def test_prose_local_server_remote_endpoint_excluded(cfg):
    # KTD-4/KTD-5: a REMOTE BYO endpoint must NOT be a prose backend (no egress).
    cfg("local-server", endpoint="http://evil.example.com:1234")
    prov = build_prose_provider()
    assert isinstance(prov, UnavailableProvider)
    assert not hasattr(prov, "call_block_bullets")


@pytest.mark.privacy
def test_prose_cloud_active_provider_has_no_on_device_bullet_backend(cfg):
    # A cloud active provider yields no on-device-class bullet backend → the
    # consolidator falls to the app-level heuristic (cloud never egresses here).
    cfg("gemini")
    prov = build_prose_provider()
    assert isinstance(prov, UnavailableProvider)
    assert not hasattr(prov, "call_block_bullets")
