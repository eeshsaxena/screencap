"""Shared generation finish: grounding-prompt build + untrusted-output sanitize
(SCR-243, U2).

The cloud / downloaded / local-server generation backends share one prompt seam
and one output-hardening seam — the generation analogue of ``local_finish.py``
(segmentation). Keeping both here means the grounding framing and the
untrusted-output rule cannot be applied to only one backend.

``build_answer_prompt`` composes the Python-side grounding instructions + the
user question + the (stripped) evidence into one model input. The Swift helper
(``macos/IntelligenceHelper``) carries its own mirror copy of the grounding
instructions for the on-device path (KTD3) — the two are kept in sync by
convention, exactly as segmentation duplicates ``promptInstructions`` /
``_LLM_PROMPT``.

``sanitize_answer`` is the text analogue of
:func:`~screencap.segmentation.sanitize.sanitize_tasks` (KTD10/KTD12): the answer
is recording-derived, attacker-influenceable model output surfaced to Chat and,
downstream, an MCP/daemon consumer, so every backend runs its output through this
before returning. It cannot scrub *semantic* prompt injection ("ignore prior
instructions…") — the grounding framing is the only mitigation there, and the
consumer must treat the answer as untrusted.
"""

from __future__ import annotations

import re

# Bound the answer length a runaway / hostile model can return (UI / transport
# DoS guard), re-applied after markup stripping.
_MAX_ANSWER_LEN = 8000

# Control chars (C0 minus tab/newline, plus DEL and C1) — never legitimate in a
# rendered answer and prime injection vectors. Mirrors sanitize.py.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Strip whole ``<...>`` spans so an injected ``<script>`` / fake tag can't reach a
# Markdown/HTML consumer. Mirrors sanitize.py.
_MARKUP_RE = re.compile(r"<[^>]*>")

_GROUNDING_INSTRUCTIONS = (
    "You are answering a question about the user's own recorded computer "
    "activity, using ONLY the evidence provided below. Ground every claim in "
    "that evidence. If the evidence does not contain enough to answer, say so "
    "plainly and briefly — do not guess, invent, or draw on outside knowledge. "
    "Keep the answer concise."
)


def build_answer_prompt(prompt: str, evidence) -> str:
    """Compose grounding instructions + question + stripped evidence into the
    model input used by the cloud / downloaded / local-server backends.

    ``evidence`` is an :class:`~screencap.segmentation.generation.Evidence`; only
    its ``text`` is interpolated (the caller has already enforced the stripped
    gate before reaching here).
    """
    return (
        f"{_GROUNDING_INSTRUCTIONS}\n\n"
        f"EVIDENCE:\n{evidence.text}\n\n"
        f"QUESTION:\n{prompt}\n\n"
        "ANSWER:"
    )


def sanitize_answer(text: str) -> str:
    """Harden a model-generated answer string before it leaves a backend.

    Strips angle-bracket markup and control characters and bounds the length — a
    text analogue of :func:`~screencap.segmentation.sanitize.sanitize_tasks`
    (KTD10/KTD12). Returns the sanitized string (possibly empty; the caller maps
    an empty/whitespace result to ``PROVIDER_UNAVAILABLE``).
    """
    if not isinstance(text, str):
        return ""
    cleaned = _MARKUP_RE.sub("", text)
    cleaned = _CONTROL_RE.sub("", cleaned)
    return cleaned[:_MAX_ANSWER_LEN]
