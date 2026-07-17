"""Day-split provider routing (U8, KTD7/KTD8, SCR-239).

Builds the provider the terminal day-split path calls, from the configured active
provider. The routing — not the ``on_device_available`` boolean — is the guard
that keeps a REMOTE endpoint or a cloud provider out of day-split: only
on-device-class backends are ever placed in the day-split provider set.

- ``on-device`` → a :class:`ChainedOnDeviceProvider` of Apple Foundation Models
  then, when a downloaded model is installed (opt-in), the downloaded backend.
- ``downloaded`` → the downloaded backend.
- ``local-server`` → the BYO backend **only when its endpoint classifies LOCAL**
  (loopback literal); a REMOTE endpoint is not eligible to day-split, so it
  resolves to :class:`UnavailableProvider` → the idle-gap heuristic.
- anything else (e.g. ``gemini``) → :class:`UnavailableProvider` (cloud never
  day-splits, R5 over the ladder).
"""

from __future__ import annotations

from pathlib import Path

from screencap.segmentation.generation import GenerationProvider
from screencap.segmentation.provider import LLMProvider


def build_day_split_provider(
    recording_dir: "Path | str | None" = None,
    stop_event: "object | None" = None,
    is_live: bool = False,
    manifests: "list[dict] | None" = None,
) -> LLMProvider:
    """Return the provider to call for day-split, per the configured active provider.

    ``recording_dir`` / ``stop_event`` / ``is_live`` are the SCR-275 U4
    per-recording context (KTD-1 transport): the terminal stage supplies them
    at call time so the on-device backend can run the heuristic-first windowed
    pipeline (with cooperative stop checks) instead of the legacy whole-day
    call. ``manifests`` optionally rides the same context: the caller's
    already-loaded chunk manifests, so the on-device backend need not re-read
    them from disk (``None`` → the backend loads its own). Callers that pass
    none (legacy callers) get the old behavior; the downloaded / BYO backends
    ignore the context entirely.
    """
    from screencap import config
    from screencap.segmentation.endpoint import LOCAL, classify_endpoint
    from screencap.segmentation.providers.chained import (
        ChainedOnDeviceProvider,
        UnavailableProvider,
    )

    name = config.get_llm_provider()

    if name == "on-device":
        from screencap.segmentation.providers.ondevice import OnDeviceProvider

        backends: list[LLMProvider] = [OnDeviceProvider(
            recording_dir=recording_dir,
            stop_event=stop_event,
            is_live=is_live,
            manifests=manifests,
        )]
        if _downloaded_model_installed():
            from screencap.segmentation.providers.downloaded import DownloadedProvider

            backends.append(DownloadedProvider())
        return ChainedOnDeviceProvider(backends)

    if name == "downloaded":
        from screencap.segmentation.providers.downloaded import DownloadedProvider

        return DownloadedProvider()

    if name == "local-server":
        endpoint = config.get_local_server_endpoint()
        if endpoint and classify_endpoint(endpoint) == LOCAL:
            from screencap.segmentation.providers.local_server import LocalServerProvider

            return LocalServerProvider()
        # REMOTE endpoint (or none) — never day-splits; degrade to the heuristic.
        return UnavailableProvider()

    # Any other active provider (e.g. a cloud one) never day-splits.
    return UnavailableProvider()


def build_answer_provider() -> GenerationProvider:
    """Return the on-device-class generation provider for the configured active
    provider (SCR-243, U7).

    Mirrors :func:`build_day_split_provider`, but for the recall-answer path and
    returning **on-device-class only** — the consented-cloud fallback lives in
    the dispatcher (``answer_recall``), not here (KTD6). REMOTE BYO endpoints are
    excluded (KTD5): a REMOTE endpoint resolves to
    :class:`UnavailableGenerationProvider` so evidence never egresses off-box via
    the answer chain.
    """
    from screencap import config
    from screencap.segmentation.endpoint import LOCAL, classify_endpoint
    from screencap.segmentation.providers.chained import (
        ChainedGenerationProvider,
        UnavailableGenerationProvider,
    )

    name = config.get_llm_provider()

    if name == "on-device":
        from screencap.segmentation.providers.ondevice import OnDeviceProvider

        backends: list[GenerationProvider] = [OnDeviceProvider()]
        if _downloaded_model_installed():
            from screencap.segmentation.providers.downloaded import DownloadedProvider

            backends.append(DownloadedProvider())
        return ChainedGenerationProvider(backends)

    if name == "downloaded":
        from screencap.segmentation.providers.downloaded import DownloadedProvider

        return DownloadedProvider()

    if name == "local-server":
        endpoint = config.get_local_server_endpoint()
        if endpoint and classify_endpoint(endpoint) == LOCAL:
            from screencap.segmentation.providers.local_server import LocalServerProvider

            return LocalServerProvider()
        # REMOTE endpoint (or none) — no on-device-class answer backend (KTD5).
        return UnavailableGenerationProvider()

    # Any other active provider (e.g. a cloud one) has no on-device-class answer
    # backend; the dispatcher handles the consented-cloud step.
    return UnavailableGenerationProvider()


def _downloaded_model_installed() -> bool:
    try:
        from screencap.models import is_model_installed

        return is_model_installed()
    except ImportError:
        return False
