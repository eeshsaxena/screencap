"""Chained on-device provider — the AFM → downloaded ladder (U8, KTD7, SCR-239).

When the active provider is on-device, day-splitting tries Apple Foundation Models
first, then the downloaded model (if opted in + available), cascading **only on**
:data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE`. A genuine ``None``
(a backend ran and declined to name) **stops** the chain — it is a fail-open
no-tasks result, never a fall-through to the next backend. The final result flows
through the unchanged ``resolve_day_split``, so the consent policy's
``on_device_available`` still means "any on-device backend available."
"""

from __future__ import annotations

from screencap.segmentation.generation import Evidence, GenerationProvider, GenerationResult
from screencap.segmentation.provider import (
    PROVIDER_UNAVAILABLE,
    LLMProvider,
    ProviderUnavailable,
    SegmentResult,
)


class ChainedOnDeviceProvider:
    """Try each on-device backend in order; cascade only on ``PROVIDER_UNAVAILABLE``."""

    def __init__(self, backends: list[LLMProvider]) -> None:
        self._backends = backends

    def segment(self, activity_summary: dict) -> SegmentResult:
        # If the chain is empty, nothing on-device is available (→ heuristic).
        result: SegmentResult = PROVIDER_UNAVAILABLE
        for backend in self._backends:
            result = backend.segment(activity_summary)
            if result is PROVIDER_UNAVAILABLE:
                continue  # this backend could not run — try the next
            # A tasks dict (use it) OR None (ran, declined — fail-open) stops here.
            return result
        return result


class UnavailableProvider:
    """A provider that always reports unavailable.

    Used by the day-split router for a configuration that is not eligible to
    day-split on-device (a REMOTE BYO endpoint, or a cloud provider) — so
    day-split degrades to the idle-gap heuristic rather than ever touching cloud.
    """

    def segment(self, activity_summary: dict) -> ProviderUnavailable:
        return PROVIDER_UNAVAILABLE


class ChainedGenerationProvider:
    """Try each on-device-class generation backend in order; cascade only on
    ``PROVIDER_UNAVAILABLE`` (SCR-243, U6).

    The answer-path analogue of :class:`ChainedOnDeviceProvider`. A ``str`` (a
    grounded answer, including a grounded refusal) stops the chain and is
    returned; only :data:`PROVIDER_UNAVAILABLE` falls through to the next
    backend. An empty chain returns :data:`PROVIDER_UNAVAILABLE`. There is no
    ``None`` state — the generation contract is two-state (KTD9).
    """

    def __init__(self, backends: list[GenerationProvider]) -> None:
        self._backends = backends

    def answer(self, prompt: str, evidence: Evidence) -> GenerationResult:
        # Empty chain → nothing on-device-class is available.
        for backend in self._backends:
            result = backend.answer(prompt, evidence)
            # Only a non-empty str is a real answer that stops the chain. A
            # backend that returns PROVIDER_UNAVAILABLE — or (defensively) None,
            # "", whitespace, or any non-str from a future/buggy backend — is
            # treated as "could not run": cascade to the next rather than leak a
            # blank/invalid answer or suppress a healthy downstream backend (KTD9).
            if isinstance(result, str) and result.strip():
                return result
        return PROVIDER_UNAVAILABLE


class UnavailableGenerationProvider:
    """A generation provider that always reports unavailable (SCR-243, U6).

    The answer-path analogue of :class:`UnavailableProvider` — used by the
    answer router for a configuration with no on-device-class generation
    backend (a REMOTE BYO endpoint, or a cloud-only active provider), so the
    dispatcher falls through to the consented-cloud step rather than an
    on-device answer.
    """

    def answer(self, prompt: str, evidence: Evidence) -> ProviderUnavailable:
        return PROVIDER_UNAVAILABLE
