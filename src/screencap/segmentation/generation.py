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

Multimodal evidence channel (SCR-272, R12)
------------------------------------------
Frame bytes ride to a provider through ONE typed, masked-verified channel:
:class:`MaskedFrame`. ``Evidence.text`` stays ``str``-only — image bytes never
hide inside it — and any frame that leaves does so as a :class:`MaskedFrame`
whose ``masked`` provenance marker is ``True`` (default ``False`` = fail-closed).
Both provider paths run the same checkpoint, :func:`verify_masked_frames`, before
frames may leave: the recall :meth:`GenerationProvider.answer` path carries them
inside :attr:`Evidence.masked_frames` (verified in :meth:`Evidence.__post_init__`),
and the :meth:`~screencap.segmentation.provider.LLMProvider.segment` path (which
does NOT use :class:`Evidence`) takes an optional ``masked_frames`` kwarg — never
routing frames through the untyped ``activity_summary`` dict. A ``MaskedFrame``
with ``masked=True`` may be minted by ONLY the U1 producer
(``screencap.segmentation.frame_egress.produce_egress_frames``);
``tests/segmentation/test_generation.py`` pins that with an AST guard mirroring
KTD11, so an unmarked/raw frame structurally cannot reach a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union, runtime_checkable

from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable

__all__ = [
    "Evidence",
    "GenerationProvider",
    "GenerationResult",
    "MaskedFrame",
    "PROVIDER_UNAVAILABLE",
    "verify_masked_frames",
]


@dataclass(frozen=True)
class MaskedFrame:
    """The ONE type that may carry frame bytes to a provider (SCR-272, R12).

    ``jpeg_bytes`` is the masked, metadata-stripped JPEG; ``timestamp_ms`` is the
    frame's epoch time in milliseconds (``int(round(ts * 1000))``, matching the
    content index / ``frame.nearest`` pointers). ``masked`` is the fail-closed
    provenance marker: it defaults ``False`` so an unmarked/raw frame cannot ride,
    and only the U1 producer
    (``screencap.segmentation.frame_egress.produce_egress_frames``) may mint a
    ``MaskedFrame(masked=True)`` — an AST guard in
    ``tests/segmentation/test_generation.py`` pins that (mirroring KTD11).

    Both provider paths run every frame through :func:`verify_masked_frames`
    before it may leave, so a ``masked``-falsy frame structurally cannot reach a
    model.
    """

    jpeg_bytes: bytes
    timestamp_ms: int
    masked: bool = False


def verify_masked_frames(
    masked_frames: "tuple[MaskedFrame, ...]",
) -> "tuple[MaskedFrame, ...]":
    """Fail-closed guard: every frame must be a ``MaskedFrame`` marked ``masked``.

    The single checkpoint BOTH provider paths run before frame bytes may leave to
    a provider — the recall :class:`Evidence` carrier (via
    :meth:`Evidence.__post_init__`) and the ``segment`` path (summary/day-split,
    which does not use :class:`Evidence`). Raises :class:`TypeError` if any element
    is not a :class:`MaskedFrame`, or is a :class:`MaskedFrame` whose ``masked``
    provenance marker is falsy — so an unmarked/raw frame structurally cannot ride
    to a model (R12). Returns ``masked_frames`` unchanged when every frame is
    marked, for use as a pass-through in a caller expression.
    """
    for frame in masked_frames:
        if not isinstance(frame, MaskedFrame):
            raise TypeError(
                "masked_frames may carry only MaskedFrame instances "
                "(R12: frame bytes ride only through the typed masked channel)"
            )
        if not frame.masked:
            raise TypeError(
                "masked_frames may carry only masked=True frames "
                "(fail-closed: an unmarked/raw frame cannot reach a provider)"
            )
    return masked_frames


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

    ``masked_frames`` is the typed multimodal channel for the recall answer path
    (SCR-272): frame bytes ride here as :class:`MaskedFrame` values, never inside
    ``text``. :meth:`__post_init__` runs them through :func:`verify_masked_frames`,
    so an :class:`Evidence` structurally cannot carry an unmarked/raw frame. It
    defaults to the empty tuple (a text-only :class:`Evidence`).
    """

    text: str
    stripped: bool = False
    masked_frames: "tuple[MaskedFrame, ...]" = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("Evidence.text must be a str (R12: text-only evidence)")
        verify_masked_frames(self.masked_frames)


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

    ``masked_frames`` is the optional typed multimodal channel (SCR-272): masked
    frame bytes may ride only as :class:`MaskedFrame` values, each of which a
    backend must clear through :func:`verify_masked_frames` before egress. It
    defaults empty; frames also ride inside :attr:`Evidence.masked_frames` on the
    recall path, and both carriers are verified by the same guard.
    """

    def answer(
        self,
        prompt: str,
        evidence: Evidence,
        *,
        masked_frames: "tuple[MaskedFrame, ...]" = (),
    ) -> GenerationResult:
        ...
