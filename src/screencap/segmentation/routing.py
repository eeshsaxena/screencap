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


def build_prose_provider(
    recording_dir: "Path | str | None" = None,
) -> LLMProvider:
    """Return the on-device-class provider for the day-diary prose kind (U3, KTD-4).

    Mirrors :func:`build_answer_provider`'s shape: it returns **on-device-class
    only** — the block-bullet / narrative model calls run on-device, and the
    consented-cloud enrichment fallback (reusing the description text the cloud
    prompt already produces) is owned by the caller/dispatcher, not here. REMOTE
    BYO endpoints are excluded (a REMOTE endpoint resolves to
    :class:`UnavailableProvider`) so diary evidence never egresses off-box via
    this seam, and a cloud active provider yields :class:`UnavailableProvider`
    too (the on-device-class step has no cloud backend).

    Only the Apple Foundation Models backend exposes the ``call_block_bullets``
    verb the consolidator uses, so the on-device case returns a recording-bound
    :class:`OnDeviceProvider` directly (the ``recording_dir`` context the verb's
    budget/stop plumbing rides). Any other backend lacks the verb, so the
    consolidator's per-block bullet call falls through to the honest app-level
    heuristic (R6/AE3).
    """
    from screencap import config
    from screencap.segmentation.endpoint import LOCAL, classify_endpoint
    from screencap.segmentation.providers.chained import UnavailableProvider

    name = config.get_llm_provider()

    if name == "on-device":
        from screencap.segmentation.providers.ondevice import OnDeviceProvider

        return OnDeviceProvider(recording_dir=recording_dir)

    if name == "local-server":
        endpoint = config.get_local_server_endpoint()
        if endpoint and classify_endpoint(endpoint) == LOCAL:
            from screencap.segmentation.providers.local_server import LocalServerProvider

            return LocalServerProvider()
        return UnavailableProvider()

    # Downloaded / cloud / anything else has no on-device-class bullet backend;
    # the caller falls through to the app-level heuristic (never off-box here).
    return UnavailableProvider()


def _downloaded_model_installed() -> bool:
    try:
        from screencap.models import is_model_installed

        return is_model_installed()
    except ImportError:
        return False
