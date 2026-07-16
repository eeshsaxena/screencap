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
    MaskedFrame,
    verify_masked_frames,
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
# U3 — MaskedFrame typed multimodal channel + masked-only invariant (R12)
# ---------------------------------------------------------------------------


def test_evidence_defaults_to_no_masked_frames():
    # A text-only Evidence carries no frames.
    assert Evidence(text="hi", stripped=True).masked_frames == ()


def test_masked_frame_defaults_unmasked():
    # masked defaults False so an un-set provenance marker fails closed.
    assert MaskedFrame(jpeg_bytes=b"j", timestamp_ms=1).masked is False


def test_evidence_rejects_unmasked_frame():
    # masked-only invariant: an unmarked/raw frame structurally cannot ride.
    raw = MaskedFrame(jpeg_bytes=b"j", timestamp_ms=1, masked=False)
    with pytest.raises(TypeError):
        Evidence(text="hi", stripped=True, masked_frames=(raw,))


def test_evidence_carries_masked_frames_and_text():
    # A masked=True frame + text carries both, side by side, text still str.
    frame = MaskedFrame(jpeg_bytes=b"jpegbytes", timestamp_ms=300_000, masked=True)
    ev = Evidence(text="edited foo.py", stripped=True, masked_frames=(frame,))
    assert ev.text == "edited foo.py"
    assert ev.masked_frames == (frame,)
    assert ev.masked_frames[0].jpeg_bytes == b"jpegbytes"


def test_verify_masked_frames_rejects_unmasked():
    # The SEGMENT path (which does not use Evidence) runs the same guard: a
    # MaskedFrame(masked=False) passed as a segment input raises.
    raw = MaskedFrame(jpeg_bytes=b"j", timestamp_ms=1, masked=False)
    with pytest.raises(TypeError):
        verify_masked_frames((raw,))


def test_verify_masked_frames_rejects_non_maskedframe():
    # Only MaskedFrame values may travel the channel — a bare bytes tuple is
    # rejected, so raw bytes cannot be smuggled past the type.
    with pytest.raises(TypeError):
        verify_masked_frames((b"raw-bytes",))  # type: ignore[arg-type]


def test_verify_masked_frames_passes_masked_and_returns_unchanged():
    frames = (
        MaskedFrame(jpeg_bytes=b"a", timestamp_ms=1, masked=True),
        MaskedFrame(jpeg_bytes=b"b", timestamp_ms=2, masked=True),
    )
    assert verify_masked_frames(frames) is frames


def test_verify_masked_frames_empty_is_ok():
    assert verify_masked_frames(()) == ()


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


def _is_truthy(value: ast.AST) -> bool:
    """False only for a *falsy constant* (False / 0 / None / "").

    A truthy constant OR any non-constant expression (a variable that could be
    truthy) is flagged conservatively — the guard fails closed.
    """
    return not (isinstance(value, ast.Constant) and not value.value)


def _evidence_names(tree: ast.AST) -> set[str]:
    """Local names bound to the ``Evidence`` class, incl. ``import ... as`` aliases."""
    names = {"Evidence"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Evidence":
                    names.add(alias.asname or alias.name)
    return names


def _constructs_stripped_evidence(tree: ast.AST) -> bool:
    """True if this AST constructs a trusted ``Evidence(stripped=truthy)`` via ANY
    form: keyword arg, second positional arg, an ``import ... as`` alias, or a
    module-qualified ``pkg.Evidence(...)`` call. Catching only the keyword form
    (the earlier version) let ``Evidence("t", True)`` and aliased imports slip
    past the tripwire."""
    names = _evidence_names(tree)
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
        if name not in names:
            continue
        # keyword: Evidence(..., stripped=<truthy>)
        if any(kw.arg == "stripped" and _is_truthy(kw.value) for kw in node.keywords):
            return True
        # positional: Evidence(text, <truthy>)
        if len(node.args) >= 2 and _is_truthy(node.args[1]):
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


@pytest.mark.privacy
def test_stripped_evidence_guard_catches_all_construction_forms():
    """The guard must catch every trusted-evidence construction form — a bypass
    via any of these would ship unstripped, recording-derived text undetected."""
    caught = [
        "Evidence(stripped=True)",
        "Evidence('t', True)",  # second positional arg is `stripped`
        "Evidence('t', flag)",  # non-constant positional — flagged conservatively
        "pkg.Evidence(stripped=True)",  # module-qualified
        "from x.generation import Evidence as E\nE(stripped=True)",  # aliased import
        "from x.generation import Evidence as E\nE('t', True)",  # aliased + positional
    ]
    for src in caught:
        assert _constructs_stripped_evidence(ast.parse(src)), f"missed: {src!r}"

    ignored = [
        "Evidence('t')",  # no stripped
        "Evidence('t', stripped=False)",
        "Evidence('t', False)",  # falsy positional
        "other(stripped=True)",  # a non-Evidence call with a stripped= kwarg
    ]
    for src in ignored:
        assert not _constructs_stripped_evidence(ast.parse(src)), f"false-positive: {src!r}"


# ---------------------------------------------------------------------------
# U3 — builder-only guard for MaskedFrame(masked=True) (mirrors KTD11)
# ---------------------------------------------------------------------------

# Scan the whole package (not just segmentation/): a MaskedFrame(masked=True) is
# the one value allowed to carry frame bytes to a provider, so the "only the U1
# producer may mint it" invariant must hold repo-wide (U5's attach points live
# outside segmentation/ and must call produce_egress_frames, never mint their own).
_SRC_PKG = Path(__file__).resolve().parents[2] / "src" / "screencap"
_FRAME_EGRESS = _SEG_ROOT / "frame_egress.py"


def _masked_frame_names(tree: ast.AST) -> set[str]:
    """Local names bound to ``MaskedFrame``, incl. ``import ... as`` aliases."""
    names = {"MaskedFrame"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "MaskedFrame":
                    names.add(alias.asname or alias.name)
    return names


def _constructs_masked_frame(tree: ast.AST) -> bool:
    """True if this AST mints a provenance-trusted ``MaskedFrame(masked=truthy)``
    via ANY form: keyword arg, third positional arg (``jpeg_bytes, timestamp_ms,
    masked``), an ``import ... as`` alias, or a module-qualified
    ``pkg.MaskedFrame(...)`` call — mirroring the ``Evidence(stripped=True)``
    tripwire so no construction form slips past."""
    names = _masked_frame_names(tree)
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
        if name not in names:
            continue
        # keyword: MaskedFrame(..., masked=<truthy>)
        if any(kw.arg == "masked" and _is_truthy(kw.value) for kw in node.keywords):
            return True
        # positional: MaskedFrame(jpeg_bytes, timestamp_ms, <truthy>)
        if len(node.args) >= 3 and _is_truthy(node.args[2]):
            return True
    return False


@pytest.mark.privacy
def test_only_frame_egress_mints_masked_frame():
    """Only the U1 producer (``frame_egress.py``) may mint a
    ``MaskedFrame(masked=True)`` — the provenance stamp the provider seam's
    fail-closed guard trusts. Any other module minting it would let an
    unverified frame ride to a provider undetected."""
    offenders = []
    for path in _SRC_PKG.rglob("*.py"):
        if path.resolve() == _FRAME_EGRESS.resolve():
            continue  # the blessed producer
        tree = ast.parse(path.read_text(), filename=str(path))
        if _constructs_masked_frame(tree):
            offenders.append(path.relative_to(_SRC_PKG))
    assert not offenders, (
        "MaskedFrame(masked=True) may be minted only by "
        "segmentation/frame_egress.py (the U1 producer); these modules mint it: "
        f"{offenders}"
    )


@pytest.mark.privacy
def test_frame_egress_does_mint_masked_frame():
    """Sanity: the blessed producer really does mint a MaskedFrame(masked=True) —
    so the guard is testing a live invariant, not a vacuous one."""
    tree = ast.parse(_FRAME_EGRESS.read_text(), filename=str(_FRAME_EGRESS))
    assert _constructs_masked_frame(tree)


@pytest.mark.privacy
def test_masked_frame_guard_catches_all_construction_forms():
    """The guard must catch every masked-frame construction form — a bypass via
    any of these would ship an unverified frame to a provider undetected."""
    caught = [
        "MaskedFrame(masked=True)",
        "MaskedFrame(b, ts, True)",  # third positional arg is `masked`
        "MaskedFrame(b, ts, flag)",  # non-constant positional — flagged conservatively
        "pkg.MaskedFrame(masked=True)",  # module-qualified
        "from x.generation import MaskedFrame as M\nM(masked=True)",  # aliased import
        "from x.generation import MaskedFrame as M\nM(b, ts, True)",  # aliased + positional
    ]
    for src in caught:
        assert _constructs_masked_frame(ast.parse(src)), f"missed: {src!r}"

    ignored = [
        "MaskedFrame(b, ts)",  # no masked -> defaults False
        "MaskedFrame(b, ts, masked=False)",
        "MaskedFrame(b, ts, False)",  # falsy positional
        "other(masked=True)",  # a non-MaskedFrame call with a masked= kwarg
    ]
    for src in ignored:
        assert not _constructs_masked_frame(ast.parse(src)), f"false-positive: {src!r}"


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
    assert "<" not in clean and ">" not in clean  # angle brackets escaped, not raw
    assert "\x07" not in clean
    assert "alert(1)" in clean  # inner text preserved; markup neutralized via escaping
    assert "tag" in clean


def test_sanitize_answer_neutralizes_malformed_and_bare_brackets():
    # A lone '<' (prose or a truncated tag) and unbalanced brackets must be
    # neutralized, not left dangling — and legitimate prose text is preserved
    # (escaping is lossless, unlike deleting <...> spans).
    out = sanitize_answer("compare x < y and 3 > 2, plus <notclosed here")
    assert "<" not in out and ">" not in out
    assert "x" in out and "y" in out and "3" in out and "2" in out  # prose kept
    assert "notclosed here" in out  # truncated-tag text preserved, not deleted
    assert "&lt;" in out and "&gt;" in out  # escaped, not stripped


def test_sanitize_answer_passes_benign_text_unchanged():
    benign = "You edited foo.py and ran the tests; they passed."
    assert sanitize_answer(benign) == benign


def test_sanitize_answer_bounds_length():
    assert len(sanitize_answer("x" * 20000)) == 8000


def test_sanitize_answer_non_str_returns_empty():
    assert sanitize_answer(None) == ""  # type: ignore[arg-type]
