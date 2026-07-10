"""Answer-side attribution validator (U4) — a BOUNDED, HEURISTIC v1.

Grounding (R4/R6) is a *behavioral* property (KTD3). Single-pass retrieval makes
the evidence BOUND structural — the model only ever sees the retrieved, stripped
set — but faithful USE of that evidence is not guaranteed by architecture. This
module is the net-new answer-side check the plan calls for, run AFTER the model
answers and BEFORE the answer is surfaced: the dispatch blanks a failing answer to
a refusal. The shipped task-schema validator (``validate_llm_tasks``) is for
prose-free task JSON and deliberately does NOT apply to a recall answer.

What v1 is — and is NOT (be honest)
-----------------------------------
This is a **heuristic** gate, not research-grade natural-language claim mapping.
It enforces three concrete, testable rules:

  (a) **Number match** — every numeric / duration figure the answer STATES must
      be backed: it must appear either in a computed bundle figure (a
      ``WindowAggregate``, echoed VERBATIM per R5) or verbatim in an evidence
      snippet (a number the operator saw on screen). A number backed by neither is
      a fabrication → reject.
  (b) **Refusal gate** — when the bundle is empty (no evidence AND no figures), the
      answer MUST be a refusal. A confident answer over no evidence → reject (AE1).
  (c) **Grounding overlap** — a non-refusal answer's substantive tokens must
      overlap the evidence/figure text. An answer that asserts specifics present in
      NONE of the evidence is flagged as ungrounded.

**Extension point (deferred):** a future upgrade replaces the token-overlap
heuristic (rule c) and the whole-answer number scan (rule a) with per-claim
mapping — segment the answer into claims, map each to a supplying source span.
That is out of scope for v1; the ``AttributionVerdict`` shape (an ``ok`` flag +
a ``reason`` + the backing ``sources``) is already the seam a claim-level verdict
would populate more richly. Keep this module free of NLP/ML imports so it stays
on the CI privacy lane.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from screencap.recall.orchestrator import (
    EvidenceBundle,
    EvidencePointer,
    QuestionKind,
)

# ---------------------------------------------------------------------------
# Refusal detection
# ---------------------------------------------------------------------------

# A refusal is the safe answer over thin/absent evidence. Detected by a small set
# of honest-decline phrasings the guardrail prompt instructs the model to emit
# ("say 'I don't have that'"). Matched case-insensitively as substrings.
_REFUSAL_MARKERS = (
    "i don't have",
    "i do not have",
    "i don’t have",  # curly apostrophe
    "don't have that",
    "no matching",
    "nothing in your recorded history",
    "not in your recorded history",
    "i couldn't find",
    "i could not find",
    "no evidence",
    "not enough to answer",
    "not enough evidence",
    "can't answer that",
    "cannot answer that",
    "unable to answer",
)


def is_refusal(answer: str) -> bool:
    """True when ``answer`` reads as an honest decline (the safe grounding
    outcome). A refusal always passes attribution — it never fabricates."""
    low = (answer or "").strip().lower()
    if not low:
        # An empty string is treated as a refusal (the model declined); the
        # dispatch replaces it with the canonical refusal text.
        return True
    return any(marker in low for marker in _REFUSAL_MARKERS)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributionVerdict:
    """The verdict for one answer.

    * ``ok`` — the answer is grounded (or is a refusal); the dispatch surfaces it.
      ``False`` → the dispatch blanks the answer to a refusal.
    * ``reason`` — a short, content-free reason a failing verdict carries (for
      diagnostics / logs; never echoes snippet text).
    * ``sources`` — the bundle's source pointers backing the answer, attached so
      the dispatch can populate the ChatAnswer's sources panel. Empty for a
      refusal / an empty bundle.
    """

    ok: bool
    reason: str = ""
    sources: list[EvidencePointer] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Number extraction
# ---------------------------------------------------------------------------

# Match integer / decimal numbers (optionally comma-grouped). We compare the bare
# digit run, so "90", "90.0" and "1,234" normalize before comparison.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    """Normalized numeric tokens in ``text`` (commas stripped, trailing ``.0``
    dropped) so "90", "90.0", "1,234" compare canonically."""
    out: set[str] = set()
    for m in _NUMBER_RE.finditer(text or ""):
        raw = m.group(0).replace(",", "")
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".") or "0"
        out.add(raw)
    return out


def _figure_numbers(figures: object | None) -> set[str]:
    """Every number a computed figure can be narrated as, from a duck-typed
    ``WindowAggregate`` (never a hard import of U2). We accept the raw ms value AND
    the common human renderings (whole minutes / hours) so an answer that says "90
    minutes" over a 5_400_000 ms figure is BACKED."""
    if figures is None:
        return set()
    nums: set[str] = set()
    covered_ms = getattr(figures, "covered_active_ms", None)
    uncovered_ms = getattr(figures, "uncovered_ms", None)
    for ms in (covered_ms, uncovered_ms):
        if not isinstance(ms, (int, float)):
            continue
        ms = float(ms)
        nums.add(_fmt_num(ms))
        nums.add(_fmt_num(ms / 1000.0))          # seconds
        nums.add(_fmt_num(ms / 60_000.0))        # minutes
        nums.add(_fmt_num(ms / 3_600_000.0))     # hours
    # Per-app event counts are legitimate figures too.
    for app in getattr(figures, "apps", []) or []:
        count = getattr(app, "event_count", None)
        if isinstance(count, int):
            nums.add(str(count))
        covered = getattr(app, "covered_active_ms", None)
        if isinstance(covered, (int, float)):
            nums.add(_fmt_num(covered / 60_000.0))
            nums.add(_fmt_num(covered / 3_600_000.0))
    return nums


def _fmt_num(value: float) -> str:
    """Format a number the way the answer would: an integer when whole, else a
    short decimal — matched against the answer's normalized tokens."""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return _numbers(f"{value:.2f}").pop() if _numbers(f"{value:.2f}") else str(value)


# ---------------------------------------------------------------------------
# Grounding overlap (rule c)
# ---------------------------------------------------------------------------

# Tokens that carry no grounding signal — an answer sharing only these with the
# evidence is NOT grounded. Kept small and generic (function words + a few
# discourse verbs the guardrail prompt itself seeds).
_STOPWORDS = frozenset(
    """a an the this that these those and or but of to in on at for with from by
    was were is are be been being it its you your i my we our they their he she
    him her about over under during after before around near then than so as into
    out up down off no not do did does have has had will would can could should
    may might must show showed shows saw see seen there here what which who when
    where why how much many long time spent minute minutes hour hours
    covered coverage span spans active idle away approximately approx about
    roughly around estimate estimated total""".split()
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9#'/-]*")


def _content_tokens(text: str) -> set[str]:
    """Substantive lowercase tokens (stopwords removed, length ≥ 3)."""
    return {
        t
        for t in _WORD_RE.findall((text or "").lower())
        if len(t) >= 3 and t not in _STOPWORDS
    }


# An answer is "grounded" when at least this fraction of its substantive tokens
# appears in the evidence/figure text. Deliberately lenient — the goal is to catch
# an answer that shares almost NOTHING with the evidence (rule c), not to demand
# extractive overlap (which would reject valid paraphrase). Tune upward only with
# the eval (KTD3 names on-device citation reliability as unverified).
_MIN_OVERLAP = 0.5


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_attribution(
    answer: str, bundle: EvidenceBundle, *, question: str = ""
) -> AttributionVerdict:
    """Validate that ``answer`` is grounded in ``bundle`` (KTD3 — heuristic v1).

    Returns an :class:`AttributionVerdict`. A refusal always passes. An empty
    bundle requires a refusal (rule b). Otherwise the answer must (a) state only
    numbers backed by a computed figure or an evidence snippet, and (c) overlap the
    evidence's substantive content.

    ``question`` is the operator's own question; its terms count toward the
    grounding vocabulary (rule c only) — an answer naming the app the operator
    ASKED about ("time in Salesforce" → "…in Salesforce…") is grounded in the
    question, not fabricated. The question NEVER backs a number (rule a): a figure
    must still come from a computed figure or a snippet, so the question can't
    launder a fabricated quantity.
    """
    sources = [item.pointer for item in bundle.evidence]
    has_evidence = bool(bundle.evidence) or bundle.figures is not None

    # Rule (b): empty bundle → the only correct answer is a refusal.
    if not has_evidence:
        if is_refusal(answer):
            return AttributionVerdict(ok=True, reason="", sources=[])
        return AttributionVerdict(
            ok=False,
            reason="non-refusal answer over an empty evidence bundle",
            sources=[],
        )

    # A refusal over a non-empty bundle is still safe — it never fabricates.
    if is_refusal(answer):
        return AttributionVerdict(ok=True, reason="", sources=sources)

    evidence_text = " ".join(item.text for item in bundle.evidence)

    # Rule (a): every number in the answer must be backed by a figure or a snippet.
    backing_numbers = _figure_numbers(bundle.figures) | _numbers(evidence_text)
    answer_numbers = _numbers(answer)
    unbacked = answer_numbers - backing_numbers
    if unbacked:
        return AttributionVerdict(
            ok=False,
            reason="answer states a number with no backing figure or snippet",
            sources=sources,
        )

    # Rule (c): the answer's substantive content must overlap the evidence/figures.
    # The bundle's coverage note is part of the grounding vocabulary the guardrail
    # prompt seeds ("figures are active time over CORROBORATED/covered spans …"), so
    # an aggregate answer that narrates figures with their coverage ("over covered
    # spans") is grounded, not fabricated.
    #
    # For an AGGREGATE answer whose numbers all check out (rule a passed above), the
    # trust comes from the number matching the computed figure VERBATIM (R5), not from
    # token overlap — the substantive content IS the figure narration. So rule (c) is
    # relaxed for a figures-backed aggregate: the number-match is the load-bearing
    # gate there. (Point answers still require overlap.)
    if bundle.question_kind is QuestionKind.AGGREGATE and bundle.figures is not None:
        return AttributionVerdict(ok=True, reason="", sources=sources)

    answer_tokens = _content_tokens(answer)
    if answer_tokens:
        coverage_note = getattr(bundle.coverage, "note", "") or ""
        grounding_text = (
            evidence_text + " " + _figures_text(bundle.figures) + " "
            + coverage_note + " " + (question or "")
        )
        evidence_tokens = _content_tokens(grounding_text)
        overlap = answer_tokens & evidence_tokens
        if len(overlap) / len(answer_tokens) < _MIN_OVERLAP:
            return AttributionVerdict(
                ok=False,
                reason="answer asserts specifics absent from all evidence",
                sources=sources,
            )

    return AttributionVerdict(ok=True, reason="", sources=sources)


def _figures_text(figures: object | None) -> str:
    """A tiny textual rendering of a figure's app names, so an aggregate answer that
    names the app it computed time for counts as grounded (rule c)."""
    if figures is None:
        return ""
    parts: list[str] = []
    for app in getattr(figures, "apps", []) or []:
        name = getattr(app, "app", None)
        if isinstance(name, str):
            parts.append(name)
    return " ".join(parts)
