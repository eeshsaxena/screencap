"""Pluggable LLM provider interface for session→named-task segmentation.

This is the seam that makes "any LLM" possible (R1, R2): every Intelligence
segmentation call goes through a single :class:`LLMProvider` interface, and the
current Gemini call becomes one backend behind it (see
``screencap.segmentation.providers.gemini``). Selecting the active backend is
config-only (``config.get_llm_provider`` → :func:`get_provider`).

The interface is deliberately **cloud-free** — this module imports no
``google`` / ``genai`` (nor any other vendor SDK). Backends own their heavy
imports lazily inside their methods, so importing this module (or the factory)
never drags in a provider SDK. The on-device backend (U5) lands later behind
the same interface.

Interface contract
------------------
``segment(activity_summary: dict) -> dict | None``

``activity_summary`` is the full activity-data dict produced by
:func:`screencap.segmentation.activity_summary.build_activity_summary` — it
carries ``summary`` (the compact timeline/transcript fed to the model),
``session_start``, ``session_end`` and ``time_map`` (needed to convert the
model's relative timestamps back to Unix). A provider formats its prompt from
``summary``, calls the model, then runs the raw output through
:func:`screencap.segmentation.validate.validate_llm_tasks`. It returns the
**validated** tasks dict (``{"tasks": [...], "summary": {...}, "tags": [...]}``)
or ``None`` when the model is unavailable, fails, or produces output that does
not validate. A provider never raises for an ordinary model/API failure — it
returns ``None`` so callers can fall back.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Narrow interface every segmentation backend implements.

    ``segment`` takes the full activity-data dict (see module docstring) and
    returns a validated tasks dict, or ``None`` on any failure (never raises
    for an ordinary model/API error).
    """

    def segment(self, activity_summary: dict) -> dict | None:
        ...


def get_provider(name: str) -> LLMProvider:
    """Return the backend for ``name`` (the factory / registry seam).

    ``"gemini"`` → :class:`~screencap.segmentation.providers.gemini.GeminiProvider`.
    ``"on-device"`` is a recognized name but its backend lands in U5 — it raises
    a clear :class:`NotImplementedError` placeholder for now. Any other name is
    rejected with a clear :class:`ValueError`.

    The backend module is imported lazily so this factory (and the interface
    module) stay cloud-free at import time.
    """
    if name == "gemini":
        from screencap.segmentation.providers.gemini import GeminiProvider

        return GeminiProvider()
    if name == "on-device":
        raise NotImplementedError(
            "The on-device segmentation provider is not implemented yet "
            "(lands in U5 as a Foundation Models Swift helper). Select "
            "'gemini' or configure a cloud provider until then."
        )
    raise ValueError(
        f"Unknown LLM provider: {name!r}. Known providers: 'gemini', 'on-device'."
    )
