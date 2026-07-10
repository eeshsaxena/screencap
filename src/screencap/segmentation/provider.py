"""Pluggable LLM provider interface for session→named-task segmentation.

This is the seam that makes "any LLM" possible (R1, R2): every Intelligence
segmentation call goes through a single :class:`LLMProvider` interface, and the
current Gemini call becomes one backend behind it (see
``screencap.segmentation.providers.gemini``). Selecting the active backend is
config-only (``config.get_llm_provider`` → :func:`get_provider`).

The interface is deliberately **cloud-free** — this module imports no
``google`` / ``genai`` (nor any other vendor SDK). Backends own their heavy
imports lazily inside their methods, so importing this module (or the factory)
never drags in a provider SDK. The on-device backend (U5) is a subprocess
client that shells out to a bundled Swift helper; importing it pulls nothing
heavy either.

Interface contract
------------------
``segment(activity_summary: dict) -> dict | None | ProviderUnavailable``

``activity_summary`` is the full activity-data dict produced by
:func:`screencap.segmentation.activity_summary.build_activity_summary` — it
carries ``summary`` (the compact timeline/transcript fed to the model),
``session_start``, ``session_end`` and ``time_map`` (needed to convert the
model's relative timestamps back to Unix). A provider formats its prompt from
``summary``, calls the model, then runs the raw output through
:func:`screencap.segmentation.validate.validate_llm_tasks`. It returns the
**validated** tasks dict (``{"tasks": [...], "summary": {...}, "tags": [...]}``)
or ``None`` when the model ran but the result is unusable (empty/invalid
output). A provider never raises for an ordinary model/API failure.

Two failure shapes, deliberately distinct
------------------------------------------
- ``None`` — the provider **ran** but produced no usable tasks (empty/invalid
  output that could not be repaired). The recording is left unnamed; a caller
  does NOT try a different backend. This is the Gemini backend's only failure
  return.
- :data:`PROVIDER_UNAVAILABLE` — the provider **could not run at all** (helper
  binary missing, OS below the model floor, subprocess timed out / crashed,
  Apple Intelligence not enabled). This is a distinct singleton so U7's
  degradation ladder can route on it (day-split → idle-gap heuristic; a
  consented summary → cloud) rather than silently leaving the recording
  unnamed. Only the on-device backend returns it today; cloud backends never
  do (a cloud API failure is an ordinary ``None``).
"""

from __future__ import annotations

from typing import Protocol, Union, runtime_checkable


class ProviderUnavailable:
    """Singleton sentinel: the provider could not run at all (see module doc).

    Distinct from ``None`` (ran, no usable output) and from a tasks dict (ran,
    produced tasks). Use the module-level :data:`PROVIDER_UNAVAILABLE` instance
    and identity-compare (``result is PROVIDER_UNAVAILABLE``); the class is not
    meant to be re-instantiated. Falsy so a truthiness check treats it like the
    other "no tasks" outcomes, but identity is the contract U7 routes on.
    """

    _instance: "ProviderUnavailable | None" = None

    def __new__(cls) -> "ProviderUnavailable":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "PROVIDER_UNAVAILABLE"


#: The distinct "provider could not run" sentinel U7's degradation routes on.
PROVIDER_UNAVAILABLE = ProviderUnavailable()

#: The full return type of :meth:`LLMProvider.segment`.
SegmentResult = Union[dict, None, ProviderUnavailable]


@runtime_checkable
class LLMProvider(Protocol):
    """Narrow interface every segmentation backend implements.

    ``segment`` takes the full activity-data dict (see module docstring) and
    returns a validated tasks dict, ``None`` (ran, no usable output), or
    :data:`PROVIDER_UNAVAILABLE` (could not run). It never raises for an
    ordinary model/API error.
    """

    def segment(self, activity_summary: dict) -> SegmentResult:
        ...


def get_provider(name: str) -> LLMProvider:
    """Return the backend for ``name`` (the factory / registry seam).

    ``"gemini"`` → :class:`~screencap.segmentation.providers.gemini.GeminiProvider`
    (the reconciled BYO-key Gemini, R10).
    ``"openai"`` / ``"anthropic"`` → the BYO API-key backends
    (:class:`~screencap.segmentation.providers.openai.OpenAIProvider` /
    :class:`~screencap.segmentation.providers.anthropic.AnthropicProvider`), thin
    HTTP against the user's own account.
    ``"on-device"`` →
    :class:`~screencap.segmentation.providers.ondevice.OnDeviceProvider` (the
    Apple Foundation Models Swift-helper subprocess client).
    ``"openai-cli"`` / ``"anthropic-cli"`` / ``"gemini-cli"`` →
    :class:`~screencap.segmentation.providers.cli_delegate.CliDelegateProvider`
    (BYO delegation to the user's installed vendor CLI). Any other name is
    rejected with a clear :class:`ValueError`.

    Backend modules are imported lazily so this factory (and the interface
    module) stay cloud-free and import-light at import time.
    """
    if name == "gemini":
        from screencap.segmentation.providers.gemini import GeminiProvider

        return GeminiProvider()
    if name == "on-device":
        from screencap.segmentation.providers.ondevice import OnDeviceProvider

        return OnDeviceProvider()
    if name == "downloaded":
        from screencap.segmentation.providers.downloaded import DownloadedProvider

        return DownloadedProvider()
    if name == "local-server":
        from screencap.segmentation.providers.local_server import LocalServerProvider

        return LocalServerProvider()
    if name == "openai":
        # BYO API-key backend (U3): the user's own OpenAI account over thin HTTP.
        from screencap.segmentation.providers.openai import OpenAIProvider

        return OpenAIProvider()
    if name == "anthropic":
        # BYO API-key backend (U3): the user's own Anthropic account over thin HTTP.
        from screencap.segmentation.providers.anthropic import AnthropicProvider

        return AnthropicProvider()
    if name in ("openai-cli", "anthropic-cli", "gemini-cli"):
        # BYO CLI-delegation backends (U4): shell out to the user's installed
        # ``codex`` / ``claude`` / ``gemini`` CLI. Cloud-fallback ids only.
        from screencap.segmentation.providers.cli_delegate import CliDelegateProvider

        return CliDelegateProvider(name)
    raise ValueError(
        f"Unknown LLM provider: {name!r}. Known providers: 'gemini', 'openai', "
        "'anthropic', 'on-device', 'downloaded', 'local-server', 'openai-cli', "
        "'anthropic-cli', 'gemini-cli'."
    )
