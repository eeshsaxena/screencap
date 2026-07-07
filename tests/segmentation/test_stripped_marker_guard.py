"""Guard: the ``stripped=True`` privacy marker is builder-only-writable (KTD1).

The fail-closed privacy gate (a provider refuses any activity summary not marked
``stripped=True``) is only trustworthy if that marker is set by exactly one place —
``build_activity_summary`` — after it runs the ALLOW-only strip. SCR-239 widens the
set of code that *reads* the marker (the downloaded provider, the BYO provider, the
chain), so this AST guard pins that **no** module in ``segmentation/`` other than
``activity_summary.py`` ever *writes* a truthy ``stripped`` key/attribute. A future
refactor that hand-marks a summary ``stripped=True`` (bypassing the strip) fails
here, before it can defeat the gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SEG_ROOT = Path(__file__).resolve().parents[2] / "src" / "screencap" / "segmentation"
# The one file allowed to set the marker (the builder that runs the strip).
_ALLOWED = "activity_summary.py"


def _writes_stripped_marker(tree: ast.AST) -> bool:
    """True if this AST assigns a truthy ``stripped`` dict key or attribute."""
    for node in ast.walk(tree):
        # ``x["stripped"] = <truthy>`` or ``x.stripped = <truthy>``
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "stripped"
                ):
                    return True
        # ``{"stripped": True, ...}`` dict literal.
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "stripped":
                    return True
    return False


@pytest.mark.privacy
def test_only_the_builder_writes_the_stripped_marker():
    offenders = []
    for path in _SEG_ROOT.rglob("*.py"):
        if path.name == _ALLOWED:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if _writes_stripped_marker(tree):
            offenders.append(path.relative_to(_SEG_ROOT))
    assert not offenders, (
        "the 'stripped' privacy marker must be set only by build_activity_summary "
        f"(activity_summary.py); these modules also write it: {offenders}"
    )


@pytest.mark.privacy
def test_builder_does_write_the_marker():
    """Sanity: the allowed file genuinely sets the marker (guard isn't vacuous)."""
    tree = ast.parse((_SEG_ROOT / _ALLOWED).read_text())
    assert _writes_stripped_marker(tree)
