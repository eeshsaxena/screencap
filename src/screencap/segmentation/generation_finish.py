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

import html
import re

# Bound the answer length a runaway / hostile model can return (UI / transport
# DoS guard), re-applied after escaping.
_MAX_ANSWER_LEN = 8000

# Control chars (C0 minus tab/newline, plus DEL and C1) — never legitimate in a
# rendered answer and prime injection vectors. Mirrors sanitize.py.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Request-size caps for the free-form answer path (KTD10). Bound the request
# before any subprocess spawn or cloud egress. Shared by the dispatcher AND
# every backend so the cap travels with each egress point, not only the
# dispatcher (a caller reaching a backend directly is still bounded).
_MAX_EVIDENCE_BYTES = 512 * 1024
_MAX_PROMPT_BYTES = 16 * 1024


def evidence_gate_ok(prompt: str, evidence: object) -> bool:
    """The single fail-closed gate every backend and the dispatcher share.

    Returns ``True`` only when the request may proceed. Fail-closed on:
    - a missing/non-``True`` ``stripped`` marker (R11 — strict identity, so a
      truthy-but-not-``True`` value like ``1`` is refused);
    - a non-``str`` ``evidence.text`` or ``prompt`` (R12 — no frame/image bytes
      ride inside evidence; also keeps every backend's ``never raises`` contract
      robust against a duck-typed caller object, since the encode/format below
      would otherwise ``AttributeError``);
    - a request over the size caps (KTD10 DoS/egress guard).

    Duck-typed on purpose (``getattr``) — it never raises on a malformed caller
    object; it returns ``False`` and the caller returns ``PROVIDER_UNAVAILABLE``.
    """
    if getattr(evidence, "stripped", False) is not True:
        return False
    text = getattr(evidence, "text", None)
    if not isinstance(text, str) or not isinstance(prompt, str):
        return False
    if len(text.encode("utf-8")) > _MAX_EVIDENCE_BYTES:
        return False
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        return False
    return True


_GROUNDING_INSTRUCTIONS = (
    "You are answering a question about the user's own recorded computer "
    "activity, using ONLY the evidence provided below. Ground every claim in "
    "that evidence. If the evidence does not contain enough to answer, say so "
    "plainly and briefly — do not guess, invent, or draw on outside knowledge. "
    "When the evidence is a timeline of apps and window titles, summarize what "
    "the user was doing in your own words, but name only the apps, sites, and "
    "durations that appear in the evidence — never invent an app, site, or number. "
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

    Strips control characters, then **HTML-escapes** ``&`` / ``<`` / ``>`` and
    bounds the length (KTD10). Escaping (rather than deleting ``<...>`` spans)
    neutralizes injected markup like ``<script>`` losslessly: legitimate prose
    such as ``x < y`` survives as ``x &lt; y`` instead of being partly deleted,
    and a lone/unbalanced ``<`` cannot dangle through to a Markdown/HTML
    consumer. Quotes are left as-is (``quote=False``) — they are common in prose
    and not a markup vector here. Semantic prompt-injection is out of scope (the
    consumer must treat the answer as untrusted). Returns the sanitized string
    (possibly empty; the caller maps an empty/whitespace result to
    ``PROVIDER_UNAVAILABLE``).
    """
    if not isinstance(text, str):
        return ""
    cleaned = _CONTROL_RE.sub("", text)
    cleaned = html.escape(cleaned, quote=False)
    return cleaned[:_MAX_ANSWER_LEN]
