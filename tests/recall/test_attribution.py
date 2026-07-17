"""U4 — the answer-side attribution validator (``screencap.recall.attribution``).

Grounding (R4/R6) is a *behavioral* property (KTD3): single-pass makes the
evidence BOUND structural, but faithful USE of that evidence is enforced by the
guardrail prompt PLUS this net-new validator. The shipped task-schema validator
(``validate_llm_tasks``) is for prose-free task JSON and does not apply here.

This is a deliberately BOUNDED v1 — a heuristic, not research-grade NL claim
mapping (the docstring and these tests are honest about that). The three rules
under test:

  (a) every numeric / duration figure the answer states must match a computed
      figure from the bundle VERBATIM — a fabricated number is rejected;
  (b) an empty bundle must produce a REFUSAL — a non-refusal over no evidence is
      rejected;
  (c) a grounding heuristic — the answer's substantive content must overlap the
      evidence; an answer asserting specifics absent from ALL evidence is flagged.

Vision-free, no model calls — the validator is pure text/number logic.
"""

from __future__ import annotations

import pytest

from screencap.recall.attribution import (
    AttributionVerdict,
    is_refusal,
    validate_attribution,
)
from screencap.recall.dispatch import REFUSAL_TEXT_FOUND_NO_ANSWER
from screencap.recall.orchestrator import (
    CoverageDescriptor,
    CoverageState,
    EvidenceBundle,
    EvidenceItem,
    EvidencePointer,
    QuestionKind,
    Stream,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Bundle fixtures
# ---------------------------------------------------------------------------


def _item(text: str, recording: str = "rec-1", ts: int = 1000) -> EvidenceItem:
    return EvidenceItem(
        text=text,
        pointer=EvidencePointer(recording=recording, timestamp_ms=ts, stream=Stream.CONTENT),
    )


def _bundle(
    items: list[EvidenceItem],
    *,
    figures: object | None = None,
    kind: QuestionKind = QuestionKind.POINT,
    state: CoverageState = CoverageState.OK,
) -> EvidenceBundle:
    return EvidenceBundle(
        question_kind=kind,
        evidence=items,
        coverage=CoverageDescriptor(state=state, note="", per_stream={}),
        figures=figures,
    )


class _FakeAggregate:
    """A stand-in for U2's WindowAggregate carrying just the numeric fields the
    validator reads (duck-typed — the validator must not hard-import U2)."""

    def __init__(self, covered_active_ms: int, apps: list | None = None) -> None:
        self.covered_active_ms = covered_active_ms
        self.uncovered_ms = 0
        self.apps = apps or []


class _FakeApp:
    """A stand-in for U2's AppActivity (the fields ``_figures_text`` /
    ``_figure_numbers`` read)."""

    def __init__(self, app: str, covered_active_ms: int = 0, event_count: int = 0) -> None:
        self.app = app
        self.covered_active_ms = covered_active_ms
        self.event_count = event_count


# ===========================================================================
# Rule (b): empty bundle → must be a refusal
# ===========================================================================


def test_empty_bundle_non_refusal_is_rejected():
    """AE1: an answer that asserts a fact over an EMPTY bundle is rejected — the
    validator demands a refusal when there is no evidence."""
    verdict = validate_attribution(
        answer="The refund error was a gateway timeout at the vendor portal.",
        bundle=_bundle([], state=CoverageState.NO_MATCHING_MOMENTS),
    )
    assert isinstance(verdict, AttributionVerdict)
    assert verdict.ok is False
    assert verdict.reason  # a machine/humans-readable reason is attached


def test_empty_bundle_refusal_passes():
    """A refusal over an empty bundle is the CORRECT behavior — it passes."""
    verdict = validate_attribution(
        answer="I don't have that in your recorded history.",
        bundle=_bundle([], state=CoverageState.NO_MATCHING_MOMENTS),
    )
    assert verdict.ok is True


# ===========================================================================
# Rule (a): every figure in the answer must match a computed figure verbatim
# ===========================================================================


def test_fabricated_number_is_rejected():
    """An answer that states a number with NO backing computed figure is rejected
    (the model invented a duration)."""
    bundle = _bundle(
        [_item("Salesforce dashboard")],
        figures=_FakeAggregate(covered_active_ms=90 * 60 * 1000),  # 90 minutes
        kind=QuestionKind.AGGREGATE,
    )
    # The answer claims "3 hours" — not the computed 90 minutes.
    verdict = validate_attribution(
        answer="You spent about 3 hours in Salesforce this morning.",
        bundle=bundle,
    )
    assert verdict.ok is False
    assert verdict.reason


def test_echoed_computed_figure_passes():
    """An answer that echoes the computed figure (90 minutes) verbatim passes."""
    bundle = _bundle(
        [_item("Salesforce dashboard")],
        figures=_FakeAggregate(covered_active_ms=90 * 60 * 1000),
        kind=QuestionKind.AGGREGATE,
    )
    verdict = validate_attribution(
        answer="You spent about 90 minutes in Salesforce, over covered spans.",
        bundle=bundle,
    )
    assert verdict.ok is True


def test_fractional_minute_figure_rendering_is_backed():
    """FIX B: the guardrail prompt renders minutes at 1 decimal (``_figure_lines``:
    ``round(minutes, 1)`` → "1.7 minutes"), so an answer that faithfully echoes that
    "1.7" must be BACKED. Previously ``_figure_numbers`` only rendered the 2-decimal
    "1.67", so the model's honest "1.7" was flagged unbacked and the answer was
    blanked to a refusal — a faithful answer refused."""
    from screencap.recall.dispatch import _figure_lines

    figures = _FakeAggregate(covered_active_ms=100_000)  # 100000/60000 = 1.6667 min
    # The guardrail prompt the model actually sees renders "1.7 minutes".
    lines = " ".join(_figure_lines(figures))
    assert "1.7 minutes" in lines

    bundle = _bundle(
        [_item("Salesforce dashboard")],
        figures=figures,
        kind=QuestionKind.AGGREGATE,
    )
    verdict = validate_attribution(
        answer="You spent about 1.7 minutes in Salesforce, over covered spans.",
        bundle=bundle,
    )
    assert verdict.ok is True, "a faithfully-echoed 1-decimal minute figure must pass"


def test_number_present_in_evidence_text_passes():
    """A number that appears in an evidence SNIPPET (not only a computed figure)
    is a backed number — e.g. an invoice number the user saw on screen."""
    bundle = _bundle([_item("Invoice #4471 total was 12 percent over budget")])
    verdict = validate_attribution(
        answer="The invoice you saw was #4471.",
        bundle=bundle,
    )
    assert verdict.ok is True


# ===========================================================================
# FIX E: aggregate answers must not blanket-pass — an app absent from the
# computed figures cannot be named as the subject of a figure
# ===========================================================================


def test_aggregate_answer_naming_app_absent_from_figures_is_rejected():
    """FIX E: an aggregate answer that attributes a figure to an app NOT present in
    the computed figures.apps is rejected — e.g. "90 minutes in Salesforce" when the
    90-minute figure belongs to Slack and Salesforce is absent. The old code
    early-returned ok=True for any AGGREGATE bundle once the number matched, so the
    figure could be mis-attributed to a wrong (absent) app."""
    bundle = _bundle(
        [],
        figures=_FakeAggregate(
            covered_active_ms=90 * 60 * 1000,
            apps=[_FakeApp("com.tinyspeck.slackmacgap", covered_active_ms=90 * 60 * 1000)],
        ),
        kind=QuestionKind.AGGREGATE,
    )
    verdict = validate_attribution(
        answer="You spent about 90 minutes in Salesforce this morning.",
        bundle=bundle,
    )
    assert verdict.ok is False
    assert verdict.reason


def test_aggregate_answer_naming_a_present_app_passes():
    """The counterpart: an aggregate answer naming an app that IS in figures.apps
    (matched on a substring of the bundle id / name) passes — the bounded heuristic
    only rejects an app absent from the computed figures."""
    bundle = _bundle(
        [],
        figures=_FakeAggregate(
            covered_active_ms=90 * 60 * 1000,
            apps=[_FakeApp("com.tinyspeck.slackmacgap", covered_active_ms=90 * 60 * 1000)],
        ),
        kind=QuestionKind.AGGREGATE,
    )
    verdict = validate_attribution(
        answer="You spent about 90 minutes in Slack this morning, over covered spans.",
        bundle=bundle,
    )
    assert verdict.ok is True


def test_aggregate_answer_with_no_app_names_still_passes():
    """An aggregate answer that narrates only the total (no app named) still passes —
    the app-presence check only fires when the answer actually names an app."""
    bundle = _bundle(
        [],
        figures=_FakeAggregate(covered_active_ms=90 * 60 * 1000),
        kind=QuestionKind.AGGREGATE,
    )
    verdict = validate_attribution(
        answer="You were active about 90 minutes total, over covered spans.",
        bundle=bundle,
    )
    assert verdict.ok is True


# ===========================================================================
# Rule (c): substantive content must overlap the evidence
# ===========================================================================


def test_answer_asserting_absent_specifics_is_flagged():
    """An answer that asserts specific terms found in NONE of the evidence is
    flagged as ungrounded (the grounding heuristic)."""
    bundle = _bundle([_item("Refund failed: gateway timeout on the vendor portal")])
    verdict = validate_attribution(
        answer="Your Kubernetes cluster autoscaler crashed during the deployment.",
        bundle=bundle,
    )
    assert verdict.ok is False
    assert verdict.reason


def test_grounded_answer_overlapping_evidence_passes():
    """An answer whose substantive terms overlap the evidence passes."""
    bundle = _bundle([_item("Refund failed: gateway timeout on the vendor portal")])
    verdict = validate_attribution(
        answer="The vendor portal showed a refund failure — a gateway timeout.",
        bundle=bundle,
    )
    assert verdict.ok is True


def test_refusal_always_passes_even_with_evidence():
    """A refusal is always a safe verdict — it never fabricates, so it passes
    regardless of the bundle."""
    bundle = _bundle([_item("some evidence here")])
    verdict = validate_attribution(
        answer="I don't have enough to answer that.",
        bundle=bundle,
    )
    assert verdict.ok is True


def test_found_but_unanswerable_refusal_is_recognized_as_a_refusal():
    """The dispatch's found-but-unanswerable message (KTD3/R5) must read as a
    refusal marker so a re-validation of it passes — otherwise a downstream
    re-check could treat the honest decline as a fabricated answer."""
    assert is_refusal(REFUSAL_TEXT_FOUND_NO_ANSWER) is True


# ===========================================================================
# Source pointers: the verdict carries the pointers backing the answer
# ===========================================================================


def test_verdict_carries_source_pointers_from_bundle():
    """A passing verdict attaches the bundle's source pointers so the dispatch can
    surface them on the ChatAnswer (the sources panel)."""
    items = [
        _item("first snippet", recording="rec-a", ts=1000),
        _item("second snippet", recording="rec-b", ts=2000),
    ]
    bundle = _bundle(items)
    verdict = validate_attribution(
        answer="The first snippet and second snippet describe the issue.",
        bundle=bundle,
    )
    assert verdict.ok is True
    recs = {p.recording for p in verdict.sources}
    assert recs == {"rec-a", "rec-b"}


# ===========================================================================
# Honesty about the bound: the v1 rules are heuristic, not claim-mapping
# ===========================================================================


def test_validator_is_bounded_v1_documented_extension_point():
    """The module documents that v1 is heuristic (number-match + refusal-gate +
    overlap) and names the extension point — this test pins that honesty so a
    future reader isn't misled into thinking it does research-grade claim mapping."""
    import screencap.recall.attribution as attribution

    doc = (attribution.__doc__ or "").lower()
    assert "heuristic" in doc
    # An explicit, greppable extension marker for the deferred claim-mapping upgrade.
    assert "extension" in doc or "v1" in doc
