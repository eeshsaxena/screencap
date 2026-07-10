"""Pluggable generation (prompt→answer) interface — sibling to the segmentation
provider seam (SCR-243).

Where :class:`~screencap.segmentation.provider.LLMProvider` covers the batch
session→named-task ``segment`` call, this module covers the interactive
free-form ``answer(prompt, evidence)`` call that Conversational Recall Chat
depends on. It is a *separate* seam kept deliberately narrow: the segment
contract (a validated tasks dict / ``None`` / ``PROVIDER_UNAVAILABLE``) stays
exactly as narrow as its own docstring promises, and generation-capable
backends implement BOTH protocols on the same concrete class.

Cloud-free & import-light: this module imports nothing vendor-specific and
re-uses the shared :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE`
sentinel, so importing it drags in nothing heavy.

Interface contract
------------------
``answer(prompt: str, evidence: Evidence) -> str | PROVIDER_UNAVAILABLE``

- a **str** — the model ran and produced a grounded answer. A grounded "I can't
  answer from this evidence" is a normal, non-empty string.
- :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` — the provider
  could not run at all (helper/model missing, OS below the floor, timeout,
  crash, refused unmarked input, empty/whitespace output). There is deliberately
  no ``None`` third state: unlike segmentation there is no "ran but produced no
  usable structured output" outcome to distinguish (KTD9).

Fail-closed privacy contract (R11)
----------------------------------
Every backend accepts ONLY privacy-stripped evidence: it refuses any
:class:`Evidence` whose ``stripped`` flag is not ``True`` WITHOUT spawning a
helper or making a cloud call. The strip itself runs in the consumer (Chat
retrieval); this seam only enforces the marker. ``Evidence.text`` must be a
``str`` — no frame/image bytes can ride inside it (R12). ``Evidence(stripped=True)``
must never be constructed inside ``segmentation/`` (only the consumer mints it);
``tests/segmentation/test_generation.py`` pins that with an AST guard (KTD11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union, runtime_checkable

from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable

__all__ = [
    "Evidence",
    "GenerationProvider",
    "GenerationResult",
    "PROVIDER_UNAVAILABLE",
]


@dataclass(frozen=True)
class Evidence:
    """Privacy-stripped evidence handed to a generation backend.

    ``text`` is the retrieved, already-stripped context the answer must be
    grounded in; ``stripped`` is the fail-closed marker (R11) — a backend
    refuses any ``Evidence`` whose ``stripped`` is not ``True``. ``stripped``
    defaults ``False`` so an un-set marker fails closed.

    ``text`` MUST be a ``str`` (enforced in :meth:`__post_init__`): the
    never-cloud-frames rule (R12) rests on evidence being text only, so no
    frame/image bytes can ride inside it.
    """

    text: str
    stripped: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("Evidence.text must be a str (R12: text-only evidence)")


#: The full return type of :meth:`GenerationProvider.answer`.
GenerationResult = Union[str, ProviderUnavailable]


@runtime_checkable
class GenerationProvider(Protocol):
    """Narrow interface every generation backend implements (sibling to
    :class:`~screencap.segmentation.provider.LLMProvider`).

    ``answer`` takes the user prompt and stripped :class:`Evidence` and returns
    a grounded answer string, or
    :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` when it could
    not run. It never raises for an ordinary model/API error.
    """

    def answer(self, prompt: str, evidence: Evidence) -> GenerationResult:
        ...
