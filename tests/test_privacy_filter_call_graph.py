"""Minimal structural CI guards remaining after SCR-34.

The unified-export-callable refactor (SCR-34) collapsed the three
historical export paths (CLI, chunk processor, recovery) into a single
``screencap.export.export_chunk_events`` function. With one call site
to ``unified_export_events``, the previous fleet of AST checks became
either trivially true or moot. Three structural guards remain:

1. ``unified_export_events`` is called from exactly one file:
   ``src/screencap/export.py``. Anyone adding a second caller must
   instead route through ``export_chunk_events`` so the privacy
   posture stays at one parameter.

2. Cloud-capable callers of ``export_chunk_events`` must pass
   ``window_filter=build_cloud_window_filter(...)`` explicitly.
   ``export_chunk_events`` defaults ``window_filter`` to ``None`` for
   the local-only CLI path, so a future cloud-bound caller could omit
   the kwarg and silently leak unfiltered window titles. The allowlist
   covers the only sanctioned local-only path
   (``engine/capture.py``, the CLI ``screencap export`` command).

3. ``build_privacy_filter`` is called only from its declaration site
   (``src/screencap/enforcement/window_filter.py``). Production code routes
   through ``build_cloud_window_filter`` — the factory that returns
   ``None`` for non-cloud paths and the cloud-mode filter otherwise.
   This catches the original Slack-title leak shape (a literal
   ``cloud_intent=False`` from production code).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable


def _src_root() -> Path:
    return Path(__file__).resolve().parent.parent / "src" / "screencap"


def _iter_python_files(root: Path) -> Iterable[Path]:
    yield from sorted(root.rglob("*.py"))


def _rel(path: Path) -> str:
    repo_root = _src_root().parent.parent
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _rel_to_src(path: Path) -> str:
    return path.relative_to(_src_root()).as_posix()


def _find_calls(tree: ast.AST, name: str) -> list[int]:
    linenos: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            linenos.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            linenos.append(node.lineno)
    return linenos


_UNIFIED_EXPORT_HOME = "export.py"
_BUILD_PRIVACY_FILTER_HOME = "enforcement/window_filter.py"

# Local-only callers of ``export_chunk_events`` may omit
# ``window_filter`` (the default ``None`` is correct for
# non-cloud paths). Adding a file here is a security-relevant
# decision: the file must never feed cloud upload.
_EXPORT_CHUNK_EVENTS_LOCAL_ONLY: frozenset[str] = frozenset({
    # CLI ``screencap export`` writes JSONL to disk; output never
    # leaves the user's machine.
    "engine/capture.py",
})


class TestUnifiedExportSingleCallSite:
    """``unified_export_events`` must be called from exactly one file —
    the new export seam ``src/screencap/export.py``. A second caller is
    a regression of SCR-34: privacy posture is meant to live at one
    parameter, not three call patterns.
    """

    def test_only_export_module_calls_unified_export_events(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            if rel == _UNIFIED_EXPORT_HOME:
                continue
            tree = ast.parse(py_path.read_text(), filename=str(py_path))
            for lineno in _find_calls(tree, "unified_export_events"):
                violations.append(
                    f"{_rel(py_path)}:{lineno} calls unified_export_events "
                    f"directly. SCR-34 collapsed the three historical "
                    f"export paths into screencap.export.export_chunk_events; "
                    f"new callers must route through that seam."
                )
        assert not violations, "Extra unified_export_events caller(s):\n  - " + (
            "\n  - ".join(violations)
        )


def _find_call_nodes(tree: ast.AST, name: str) -> list[ast.Call]:
    """Return ``ast.Call`` nodes whose callee matches ``name`` by either
    bare name (``foo(...)``) or attribute (``mod.foo(...)``).
    """
    nodes: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            nodes.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            nodes.append(node)
    return nodes


def _kwarg(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _is_call_to(node: ast.expr, name: str) -> bool:
    """True if ``node`` is ``name(...)`` or ``mod.name(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == name:
        return True
    if isinstance(func, ast.Attribute) and func.attr == name:
        return True
    return False


class TestExportChunkEventsCloudCallSites:
    """Cloud-capable callers of ``export_chunk_events`` must pass
    ``window_filter=build_cloud_window_filter(...)`` explicitly.

    ``export_chunk_events`` keeps a ``window_filter=None`` default so
    the CLI ``screencap export`` (local-only) stays ergonomic. Without
    this guard, a future cloud-bound caller could omit ``window_filter=``
    or pass a literal ``None`` and silently leak unfiltered window
    titles. The allowlist (``_EXPORT_CHUNK_EVENTS_LOCAL_ONLY``) names
    the only local-only path; every other call site must route the
    filter through the cloud factory.
    """

    def test_cloud_call_sites_pass_build_cloud_window_filter(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            if rel in _EXPORT_CHUNK_EVENTS_LOCAL_ONLY:
                continue
            tree = ast.parse(py_path.read_text(), filename=str(py_path))
            for call in _find_call_nodes(tree, "export_chunk_events"):
                kw = _kwarg(call, "window_filter")
                if kw is None:
                    violations.append(
                        f"{_rel(py_path)}:{call.lineno} calls "
                        f"export_chunk_events without window_filter=. "
                        f"Cloud-capable callers must pass "
                        f"window_filter=build_cloud_window_filter(...). "
                        f"If this path is local-only, add it to "
                        f"_EXPORT_CHUNK_EVENTS_LOCAL_ONLY."
                    )
                    continue
                if not _is_call_to(kw.value, "build_cloud_window_filter"):
                    violations.append(
                        f"{_rel(py_path)}:{call.lineno} passes window_filter="
                        f"<not build_cloud_window_filter(...)>. The cloud "
                        f"factory is the sanctioned source — a literal None "
                        f"or hand-rolled filter would defeat the privacy "
                        f"posture (Slack-title leak shape)."
                    )
        assert not violations, (
            "export_chunk_events cloud-call-site violation(s):\n  - "
            + "\n  - ".join(violations)
        )


class TestBuildPrivacyFilterCallSiteExclusivity:
    """``build_privacy_filter`` is module-private to the factory home
    (``src/screencap/enforcement/window_filter.py``). Production code must
    always go through ``build_cloud_window_filter``.
    """

    def test_no_build_privacy_filter_calls_outside_factory_home(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            if rel == _BUILD_PRIVACY_FILTER_HOME:
                continue
            tree = ast.parse(py_path.read_text(), filename=str(py_path))
            for lineno in _find_calls(tree, "build_privacy_filter"):
                violations.append(
                    f"{_rel(py_path)}:{lineno} calls build_privacy_filter "
                    "directly — only build_cloud_window_filter (in "
                    "screencap.enforcement.window_filter) is the sanctioned "
                    "constructor."
                )
        assert not violations, (
            "build_privacy_filter call(s) outside the factory home:\n  - "
            + "\n  - ".join(violations)
        )
