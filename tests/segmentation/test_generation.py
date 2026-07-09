"""Tests for the generation seam (SCR-243).

U1 — the ``GenerationProvider`` protocol + the ``Evidence`` fail-closed contract
(and the KTD11 builder-only AST guard). U2 — the grounding-prompt build and the
``sanitize_answer`` untrusted-output seam.

These fixtures are local to this file — the shared ``_fixtures.py`` is off-limits.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from screencap.segmentation.generation import (
    Evidence,
    GenerationProvider,
)
from screencap.segmentation.generation_finish import (
    build_answer_prompt,
    sanitize_answer,
)

# ---------------------------------------------------------------------------
# U1 — Evidence contract (KTD2 / R11 / R12)
# ---------------------------------------------------------------------------


def test_evidence_defaults_to_unstripped():
    # stripped defaults False so an un-set marker fails closed.
    assert Evidence(text="hi").stripped is False


def test_evidence_is_frozen():
    ev = Evidence(text="hi", stripped=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.text = "changed"  # type: ignore[misc]


def test_evidence_rejects_non_str_text():
    # R12: evidence is text only — no frame/image bytes can ride inside it.
    with pytest.raises(TypeError):
        Evidence(text=b"bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# U1 — GenerationProvider protocol (R5)
# ---------------------------------------------------------------------------


class _Answers:
    def answer(self, prompt, evidence):
        return "ok"


class _SegmentsOnly:
    def segment(self, activity_summary):
        return None


def test_generation_provider_is_runtime_checkable():
    assert isinstance(_Answers(), GenerationProvider)
    assert not isinstance(_SegmentsOnly(), GenerationProvider)


# ---------------------------------------------------------------------------
# U1 — builder-only guard for Evidence(stripped=True) (KTD11)
# ---------------------------------------------------------------------------

_SEG_ROOT = Path(__file__).resolve().parents[2] / "src" / "screencap" / "segmentation"


def _constructs_stripped_evidence(tree: ast.AST) -> bool:
    """True if this AST calls ``Evidence(..., stripped=<non-falsy-constant>)``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name != "Evidence":
            continue
        for kw in node.keywords:
            # Flag stripped=<anything except a falsy constant>. A non-constant
            # value (a variable that could be truthy) is flagged conservatively.
            if kw.arg == "stripped" and not (
                isinstance(kw.value, ast.Constant) and not kw.value.value
            ):
                return True
    return False


@pytest.mark.privacy
def test_no_segmentation_module_mints_stripped_evidence():
    """KTD11: only the consumer (Chat retrieval) may set Evidence(stripped=True);
    no module under segmentation/ mints it, mirroring the activity-summary
    stripped-marker guard."""
    offenders = []
    for path in _SEG_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if _constructs_stripped_evidence(tree):
            offenders.append(path.relative_to(_SEG_ROOT))
    assert not offenders, (
        "Evidence(stripped=True) must be minted only by the consumer, never "
        f"inside segmentation/; these modules construct it: {offenders}"
    )


# ---------------------------------------------------------------------------
# U2 — grounding-prompt build
# ---------------------------------------------------------------------------


def test_build_answer_prompt_includes_question_evidence_and_grounding():
    out = build_answer_prompt("what did I work on?", Evidence(text="edited foo.py", stripped=True))
    assert "what did I work on?" in out
    assert "edited foo.py" in out
    # Grounding framing is present (the "only the evidence / say so if insufficient" rule).
    assert "ONLY the evidence" in out
    assert "does not contain enough" in out


def test_build_answer_prompt_handles_empty_evidence():
    out = build_answer_prompt("q", Evidence(text="", stripped=True))
    assert "QUESTION:" in out and "q" in out  # well-formed, no crash


# ---------------------------------------------------------------------------
# U2 — sanitize_answer (KTD10)
# ---------------------------------------------------------------------------


def test_sanitize_answer_strips_markup_and_control_chars():
    dirty = "Answer <script>alert(1)</script> with a \x07 bell and <b>tag</b>."
    clean = sanitize_answer(dirty)
    assert "<" not in clean and ">" not in clean
    assert "\x07" not in clean
    assert "alert(1)" in clean  # inner text survives; only the tag span is stripped


def test_sanitize_answer_passes_benign_text_unchanged():
    benign = "You edited foo.py and ran the tests; they passed."
    assert sanitize_answer(benign) == benign


def test_sanitize_answer_bounds_length():
    assert len(sanitize_answer("x" * 20000)) == 8000


def test_sanitize_answer_non_str_returns_empty():
    assert sanitize_answer(None) == ""  # type: ignore[arg-type]
