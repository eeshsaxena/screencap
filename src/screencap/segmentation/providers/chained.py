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
