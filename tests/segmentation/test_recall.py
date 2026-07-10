"""Tests for answer_recall — the recall-answer dispatcher (SCR-243, U8).

Covers: on-device answer short-circuits cloud; consented-cloud fallback when the
on-device chain is unavailable; consent-off / no-provider → unavailable; the
fail-closed stripped gate; and a cloud provider lacking answer() degrading rather
than raising (R8).

These fixtures are local to this file — the shared ``_fixtures.py`` is off-limits.
"""

from __future__ import annotations

import pytest

from screencap.segmentation.generation import Evidence
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE
from screencap.segmentation.recall import answer_recall


def _ev(text: str = "you edited main.py", stripped: bool = True) -> Evidence:
    return Evidence(text=text, stripped=stripped)


class _Chain:
    """Fake on-device chain returning a fixed result."""

    def __init__(self, result) -> None:
        self._result = result

    def answer(self, prompt, evidence):
        return self._result


class _SpyCloud:
    """Fake cloud provider implementing the generation seam; records calls."""

    def __init__(self, result: str = "cloud answer") -> None:
        self.calls: list = []
        self._result = result

    def answer(self, prompt, evidence):
        self.calls.append((prompt, evidence))
        return self._result


@pytest.fixture()
def wire(monkeypatch):
    """Wire the dispatcher: the on-device chain result + the consented-cloud path."""
    from screencap import config
    from screencap.segmentation import routing

    def _set(*, on_device, cloud=None, recall_consent: bool = True):
        monkeypatch.setattr(routing, "build_answer_provider", lambda: _Chain(on_device))
        monkeypatch.setattr(
            config, "get_llm_cloud_provider", lambda: "gemini" if cloud is not None else None
        )
        monkeypatch.setattr(config, "get_recall_cloud_consent", lambda: recall_consent)
        monkeypatch.setattr(config, "get_summary_cloud_consent", lambda: False)
        if cloud is not None:
            monkeypatch.setattr(
                "screencap.segmentation.provider.get_provider", lambda name: cloud
            )

    return _set


def test_on_device_answer_returned_no_cloud(wire):
    spy = _SpyCloud()
    wire(on_device="on-device answer", cloud=spy)
    assert answer_recall("q", _ev()) == "on-device answer"
    assert spy.calls == []  # cloud never called


def test_cloud_fallback_when_consented(wire):
    spy = _SpyCloud("from cloud")
    wire(on_device=PROVIDER_UNAVAILABLE, cloud=spy, recall_consent=True)
    assert answer_recall("q", _ev()) == "from cloud"
    assert len(spy.calls) == 1


def test_no_cloud_when_consent_off(wire):
    spy = _SpyCloud()
    wire(on_device=PROVIDER_UNAVAILABLE, cloud=spy, recall_consent=False)
    assert answer_recall("q", _ev()) is PROVIDER_UNAVAILABLE
    assert spy.calls == []


def test_no_cloud_when_no_provider_configured(wire):
    wire(on_device=PROVIDER_UNAVAILABLE, cloud=None, recall_consent=True)
    assert answer_recall("q", _ev()) is PROVIDER_UNAVAILABLE


@pytest.mark.privacy
def test_unmarked_evidence_refused_builds_nothing(monkeypatch):
    from screencap.segmentation import routing

    built: list = []
    monkeypatch.setattr(
        routing, "build_answer_provider", lambda: built.append(1) or _Chain("x")
    )
    assert answer_recall("q", _ev(stripped=False)) is PROVIDER_UNAVAILABLE
    assert built == []  # the chain was never built


def test_cloud_provider_without_answer_degrades(wire):
    class _SegmentOnly:
        def segment(self, activity_summary):
            return None

    wire(on_device=PROVIDER_UNAVAILABLE, cloud=_SegmentOnly(), recall_consent=True)
    # A cloud provider lacking answer() must degrade to unavailable, not raise (R8).
    assert answer_recall("q", _ev()) is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Review hardening: size caps, duck-typed evidence, and never-raises.
# ---------------------------------------------------------------------------

import screencap.segmentation.routing as _routing  # noqa: E402


def test_oversized_evidence_is_unavailable_and_builds_nothing(monkeypatch):
    built: list = []
    monkeypatch.setattr(_routing, "build_answer_provider", lambda: built.append(1) or _Chain("x"))
    huge = Evidence(text="x" * (600 * 1024), stripped=True)
    assert answer_recall("q", huge) is PROVIDER_UNAVAILABLE
    assert built == []  # capped before build/egress


def test_oversized_prompt_is_unavailable_and_builds_nothing(monkeypatch):
    built: list = []
    monkeypatch.setattr(_routing, "build_answer_provider", lambda: built.append(1) or _Chain("x"))
    assert answer_recall("p" * (20 * 1024), _ev()) is PROVIDER_UNAVAILABLE
    assert built == []


@pytest.mark.privacy
def test_non_str_evidence_text_refused_without_raising(monkeypatch):
    built: list = []
    monkeypatch.setattr(_routing, "build_answer_provider", lambda: built.append(1) or _Chain("x"))

    class _Bytesy:
        stripped = True
        text = b"raw bytes"  # R12: bytes must never ride inside evidence text

    # A duck-typed object bypassing Evidence.__post_init__ must be refused at the
    # gate — not AttributeError out of the never-raises entry point.
    assert answer_recall("q", _Bytesy()) is PROVIDER_UNAVAILABLE
    assert built == []


def test_raising_chain_degrades_not_raises(monkeypatch):
    class _Boom:
        def answer(self, prompt, evidence):
            raise RuntimeError("routing/config exploded")

    monkeypatch.setattr(_routing, "build_answer_provider", lambda: _Boom())
    assert answer_recall("q", _ev()) is PROVIDER_UNAVAILABLE  # never raises


def test_build_answer_provider_raising_degrades(monkeypatch):
    def _boom():
        raise ValueError("corrupt config.toml")

    monkeypatch.setattr(_routing, "build_answer_provider", _boom)
    assert answer_recall("q", _ev()) is PROVIDER_UNAVAILABLE  # config raise → unavailable


def test_raising_cloud_provider_degrades(wire):
    class _RaisingCloud:
        def answer(self, prompt, evidence):
            raise RuntimeError("cloud api exploded")

    wire(on_device=PROVIDER_UNAVAILABLE, cloud=_RaisingCloud(), recall_consent=True)
    assert answer_recall("q", _ev()) is PROVIDER_UNAVAILABLE


def test_unknown_cloud_provider_name_degrades(monkeypatch):
    # A bogus configured cloud name → get_provider ValueError → unavailable.
    from screencap import config

    monkeypatch.setattr(_routing, "build_answer_provider", lambda: _Chain(PROVIDER_UNAVAILABLE))
    monkeypatch.setattr(config, "get_llm_cloud_provider", lambda: "bogus-provider")
    monkeypatch.setattr(config, "get_recall_cloud_consent", lambda: True)
    monkeypatch.setattr(config, "get_summary_cloud_consent", lambda: False)
    assert answer_recall("q", _ev()) is PROVIDER_UNAVAILABLE


def test_non_str_cloud_result_coerced_to_unavailable(wire):
    class _WeirdCloud:
        def answer(self, prompt, evidence):
            return 12345  # non-str

    wire(on_device=PROVIDER_UNAVAILABLE, cloud=_WeirdCloud(), recall_consent=True)
    assert answer_recall("q", _ev()) is PROVIDER_UNAVAILABLE
