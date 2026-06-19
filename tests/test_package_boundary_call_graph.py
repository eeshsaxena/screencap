"""Structural guard for the SCR-33 three-package dependency DAG.

The ``privacy`` / ``enforcement`` / ``redaction`` split is only meaningful if its
dependency direction is enforced. This guard statically (AST) asserts the DAG so
the split cannot silently re-fuse over time:

- ``screencap.privacy`` is the **shared-core leaf** — it imports neither
  ``screencap.enforcement`` nor ``screencap.redaction``.
- ``screencap.enforcement`` (capture-time) and ``screencap.redaction`` (post-hoc)
  each depend only on the shared core, **never on each other**. In particular the
  capture path must never reach the heavy detection/NLP engine (which lives under
  ``redaction``), and ``redaction`` must never reach into capture-time enforcement.

Both ``redaction`` and ``enforcement`` depending on ``privacy`` is allowed (that
is the whole point of the leaf). The guard walks every ``import`` and ``from ...
import`` in each package — including imports nested inside function bodies — so a
deferred import cannot evade it, and resolves relative imports to absolute module
paths so ``from ..redaction import x`` is caught the same as the absolute form.

Companion guard: ``tests/redaction/test_import_lightness.py`` pins the *runtime*
budget (importing the shared core pulls no engine/ML module), which catches a
heavy symbol re-fattening the core *in place* — a failure mode this edge guard,
which only inspects cross-package edges, cannot see. The two are complementary.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
_PKG_ROOT = _SRC / "screencap"


def _module_name(py: Path) -> str:
    """Absolute dotted module name for a file under ``src/`` (drops trailing __init__)."""
    rel = py.relative_to(_SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_modules(py: Path) -> set[str]:
    """Every absolute module path imported by ``py`` (module-level or nested).

    Relative imports are resolved against the file's own package so that
    ``from ..redaction import x`` becomes ``screencap.redaction``.
    """
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    own_pkg_parts = _module_name(py).split(".")
    # The package containing this module is its dotted name minus the last
    # segment for a regular module; an __init__'s module name already is its
    # package.
    own_pkg = own_pkg_parts if py.name == "__init__.py" else own_pkg_parts[:-1]

    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    mods.add(node.module)
            else:
                # Relative import: climb ``level`` packages from own_pkg.
                base = own_pkg[: len(own_pkg) - (node.level - 1)] if node.level > 1 else own_pkg
                target = list(base)
                if node.module:
                    target += node.module.split(".")
                if target:
                    mods.add(".".join(target))
    return mods


def _violations(package: str, forbidden: tuple[str, ...]) -> list[str]:
    pkg_dir = _PKG_ROOT / package
    out: list[str] = []
    for py in sorted(pkg_dir.rglob("*.py")):
        for mod in sorted(_imported_modules(py)):
            for bad in forbidden:
                if mod == bad or mod.startswith(bad + "."):
                    out.append(f"{py.relative_to(_SRC)} imports {mod}")
    return out


def test_privacy_is_a_leaf() -> None:
    """The shared core imports neither downstream package."""
    bad = _violations("privacy", ("screencap.enforcement", "screencap.redaction"))
    assert not bad, "screencap.privacy must be a leaf (imports neither half):\n" + "\n".join(bad)


def test_enforcement_does_not_import_redaction() -> None:
    """Capture-time enforcement must never reach the post-hoc redaction package."""
    bad = _violations("enforcement", ("screencap.redaction",))
    assert not bad, (
        "screencap.enforcement must not import screencap.redaction "
        "(would pull the NLP/ML engine into the capture path):\n" + "\n".join(bad)
    )


def test_redaction_does_not_import_enforcement() -> None:
    """Post-hoc redaction must never reach into capture-time enforcement."""
    bad = _violations("redaction", ("screencap.enforcement",))
    assert not bad, "screencap.redaction must not import screencap.enforcement:\n" + "\n".join(bad)
