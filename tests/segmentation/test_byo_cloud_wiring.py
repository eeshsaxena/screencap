"""U5 — cloud-eligible tasks reach the selected BYO cloud provider, guards intact.

Pins the routing invariants for a configured **bring-your-own** cloud provider —
across BOTH mechanism classes (a pasted-key id like ``openai`` and a CLI-delegation
id like ``gemini-cli``):

* ``resolve(SUMMARY)`` and ``resolve(RECALL_ANSWER)`` reach the BYO provider as
  the consented, on-device-unavailable fallback (R6/R8/R10);
* ``resolve(DAY_SPLIT)`` stays on-device/heuristic and NEVER the BYO provider
  (R7 over R5 / KTD6) — pinned both through :meth:`ConsentPolicy.resolve` and the
  day-split-specific :func:`resolve_day_split` guard;
* ``resolve(FRAMES)`` is NEVER, for every configuration (R9);
* the RECALL/Chat dispatcher (``answer_recall`` → ``_cloud_fallback``) reaches the
  BYO provider on fallback and short-circuits to on-device otherwise — the BYO ids
  flow through the existing consented-cloud dispatch unchanged now that they are
  registered in ``get_provider`` (KTD2);
* the payload handed to a BYO cloud provider carries no frame bytes / image paths.

Vision-free; no live provider calls (a fake is injected at the ``get_provider`` /
routing seam). CI runs only ``pytest -m privacy``, so the guard tests are marked.
"""

from __future__ import annotations

import json

import pytest

from screencap.segmentation.consent import (
    ConsentPolicy,
    ExecutionTarget,
    TaskKind,
)
from screencap.segmentation.degrade import (
    DegradeAction,
    resolve,
    resolve_day_split,
)
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE

# The two BYO mechanism classes: a pasted-key id and a CLI-delegation id. Every
# routing invariant must hold identically for both.
_BYO_IDS = ("openai", "gemini-cli")


# ---------------------------------------------------------------------------
# resolve() matrix — a BYO cloud provider is a consented SUMMARY/RECALL fallback,
# but NEVER a day-split target and NEVER for frames (R6/R7/R8/R9/R10).
# ---------------------------------------------------------------------------


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_summary_reaches_byo_provider_as_consented_fallback(byo_id):
    """R6/R8: SUMMARY resolves to CLOUD (the BYO provider) only as the consented,
    on-device-unavailable fallback; on-device is preferred whenever available."""
    policy = ConsentPolicy(cloud_provider=byo_id, summary_cloud_consent=True)
    # On-device preferred whenever available — never the BYO cloud.
    assert (
        policy.resolve(TaskKind.SUMMARY, on_device_available=True)
        is ExecutionTarget.ON_DEVICE
    )
    # On-device unavailable + consented + BYO provider configured → CLOUD.
    assert (
        policy.resolve(TaskKind.SUMMARY, on_device_available=False)
        is ExecutionTarget.CLOUD
    )


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_recall_reaches_byo_provider_as_consented_fallback(byo_id):
    """R10: RECALL_ANSWER resolves to CLOUD (the BYO provider) only as the
    consented, on-device-unavailable fallback."""
    policy = ConsentPolicy(cloud_provider=byo_id, recall_cloud_consent=True)
    assert (
        policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=True)
        is ExecutionTarget.ON_DEVICE
    )
    assert (
        policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=False)
        is ExecutionTarget.CLOUD
    )


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_day_split_never_the_byo_provider(byo_id):
    """R7 over R5 / KTD6: DAY_SPLIT is on-device/heuristic only — a configured +
    consented BYO cloud provider can NEVER pull it off-device."""
    policy = ConsentPolicy(
        cloud_provider=byo_id,
        summary_cloud_consent=True,
        recall_cloud_consent=True,
    )
    # Available → on-device; unavailable → heuristic. Never CLOUD, either way.
    assert (
        policy.resolve(TaskKind.DAY_SPLIT, on_device_available=True)
        is ExecutionTarget.ON_DEVICE
    )
    assert (
        policy.resolve(TaskKind.DAY_SPLIT, on_device_available=False)
        is ExecutionTarget.HEURISTIC
    )
    # And the day-split degradation guard re-asserts never-cloud for this BYO id.
    d = resolve_day_split(PROVIDER_UNAVAILABLE, policy)
    assert d.action is DegradeAction.HEURISTIC
    # The consent layer never even offers CLOUD for a day-split.
    assert (
        policy.resolve(TaskKind.DAY_SPLIT, on_device_available=False)
        is not ExecutionTarget.CLOUD
    )


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_frames_never_the_byo_provider(byo_id):
    """R9: FRAMES resolve to NEVER for every configuration, with a BYO provider
    configured and every consent row on."""
    policy = ConsentPolicy(
        cloud_provider=byo_id,
        summary_cloud_consent=True,
        recall_cloud_consent=True,
    )
    for on_device in (True, False):
        assert (
            policy.resolve(TaskKind.FRAMES, on_device_available=on_device)
            is ExecutionTarget.NEVER
        )
    # And the degrade resolver never routes FRAMES to CLOUD either.
    assert resolve(TaskKind.FRAMES, PROVIDER_UNAVAILABLE, policy).action is not (
        DegradeAction.CLOUD
    )


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_summary_and_recall_degrade_to_cloud_for_byo_ids(byo_id):
    """The degrade resolver routes a consented, on-device-unavailable SUMMARY /
    RECALL_ANSWER to DegradeAction.CLOUD for a BYO provider id (the seam the
    SUMMARY dispatch + recall._cloud_fallback act on)."""
    policy = ConsentPolicy(
        cloud_provider=byo_id,
        summary_cloud_consent=True,
        recall_cloud_consent=True,
    )
    assert (
        resolve(TaskKind.SUMMARY, PROVIDER_UNAVAILABLE, policy).action
        is DegradeAction.CLOUD
    )
    assert (
        resolve(TaskKind.RECALL_ANSWER, PROVIDER_UNAVAILABLE, policy).action
        is DegradeAction.CLOUD
    )


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_summary_not_cloud_when_consent_off_even_with_byo_configured(byo_id):
    """With SUMMARY consent OFF, a configured BYO provider is never selected —
    the fallback resolves to NONE, not CLOUD (consent gates the BYO path)."""
    policy = ConsentPolicy(cloud_provider=byo_id, summary_cloud_consent=False)
    assert (
        policy.resolve(TaskKind.SUMMARY, on_device_available=False)
        is ExecutionTarget.NONE
    )
    assert (
        resolve(TaskKind.SUMMARY, PROVIDER_UNAVAILABLE, policy).action
        is DegradeAction.NONE
    )


# ---------------------------------------------------------------------------
# RECALL / Chat dispatch — the BYO id flows through recall._cloud_fallback
# unchanged (registered in get_provider), and only on the on-device fallback.
# ---------------------------------------------------------------------------


class _SpyCloud:
    """A BYO cloud provider implementing the generation seam; records the payload."""

    def __init__(self, result: str = "byo cloud answer") -> None:
        self.calls: list = []
        self._result = result

    def answer(self, prompt, evidence):  # noqa: ANN001, ANN201
        self.calls.append((prompt, evidence))
        return self._result


class _Chain:
    """Fake on-device answer chain returning a fixed result."""

    def __init__(self, result) -> None:  # noqa: ANN001
        self._result = result

    def answer(self, prompt, evidence):  # noqa: ANN001, ANN201
        return self._result


def _wire_recall(monkeypatch, *, on_device, byo_id, recall_consent=True):
    """Wire answer_recall: the on-device chain result + the consented BYO fallback.

    Patches ``get_provider`` to return a spy for the configured BYO id, mirroring
    how ``recall._cloud_fallback`` resolves the cloud provider by name.
    """
    from screencap import config
    from screencap.segmentation import routing

    spy = _SpyCloud()
    monkeypatch.setattr(routing, "build_answer_provider", lambda: _Chain(on_device))
    monkeypatch.setattr(config, "get_llm_cloud_provider", lambda: byo_id)
    monkeypatch.setattr(config, "get_recall_cloud_consent", lambda: recall_consent)
    monkeypatch.setattr(config, "get_summary_cloud_consent", lambda: False)

    def _get(name):  # noqa: ANN001
        assert name == byo_id, f"unexpected provider name {name!r}"
        return spy

    monkeypatch.setattr("screencap.segmentation.provider.get_provider", _get)
    return spy


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_recall_reaches_byo_provider_on_fallback(monkeypatch, byo_id):
    """A configured BYO provider is reached by answer_recall's cloud fallback when
    on-device is unavailable and recall consent is on — with only stripped text."""
    from screencap.segmentation.generation import Evidence
    from screencap.segmentation.recall import answer_recall

    spy = _wire_recall(monkeypatch, on_device=PROVIDER_UNAVAILABLE, byo_id=byo_id)
    ev = Evidence(text="you edited main.py", stripped=True)

    assert answer_recall("what did I do?", ev) == "byo cloud answer"
    assert len(spy.calls) == 1
    # The BYO provider received only the stripped evidence — no frame bytes/paths.
    _prompt, handed_evidence = spy.calls[0]
    assert isinstance(handed_evidence, Evidence)
    assert handed_evidence.stripped is True
    for marker in ("\xff\xd8\xff", "\x89PNG", ".jpg", ".png", "screenshots/"):
        assert marker not in handed_evidence.text


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_recall_uses_on_device_and_skips_byo_when_available(monkeypatch, byo_id):
    """When the on-device chain answers, the BYO cloud provider is never reached —
    cloud is a fallback only for the on-device-unavailable state."""
    from screencap.segmentation.generation import Evidence
    from screencap.segmentation.recall import answer_recall

    spy = _wire_recall(monkeypatch, on_device="on-device answer", byo_id=byo_id)
    ev = Evidence(text="you edited main.py", stripped=True)

    assert answer_recall("q", ev) == "on-device answer"
    assert spy.calls == []  # BYO cloud never consulted.


@pytest.mark.privacy
@pytest.mark.parametrize("byo_id", _BYO_IDS)
def test_recall_no_byo_when_consent_off(monkeypatch, byo_id):
    """Recall consent OFF → the BYO fallback is never selected even when configured."""
    from screencap.segmentation.generation import Evidence
    from screencap.segmentation.recall import answer_recall

    spy = _wire_recall(
        monkeypatch, on_device=PROVIDER_UNAVAILABLE, byo_id=byo_id, recall_consent=False,
    )
    ev = Evidence(text="you edited main.py", stripped=True)

    assert answer_recall("q", ev) is PROVIDER_UNAVAILABLE
    assert spy.calls == []


# ---------------------------------------------------------------------------
# get_provider registers the BYO ids (the routing precondition U5 relies on).
# ---------------------------------------------------------------------------


def test_get_provider_constructs_every_byo_id():
    """Every BYO id (key + CLI classes) is constructible via get_provider — the
    precondition for the consented-cloud dispatch reaching it (KTD2)."""
    from screencap.segmentation.provider import get_provider

    for byo_id in ("openai", "anthropic", "gemini", "openai-cli", "anthropic-cli",
                   "gemini-cli"):
        provider = get_provider(byo_id)
        assert hasattr(provider, "segment"), f"{byo_id} lacks segment()"
