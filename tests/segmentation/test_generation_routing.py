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
