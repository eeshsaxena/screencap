"""CI structural guard against fail-OPEN regressions in cloud export.

Unit 8 of the unified-export-callable refactor. The byte-identical
contract test (``tests/test_unified_export_contract.py``) catches drift
between the three KNOWN callers of ``unified_export_events``. It cannot
catch a hypothetical FOURTH caller wired up incorrectly. This file is
the structural enforcement that fails CI when a future caller bypasses
the sanctioned ``build_cloud_window_filter`` factory.

Three static checks, each implemented as an AST walker scoped to
``src/screencap/`` only (``tests/``, ``scripts/``, and ``benchmarks/``
are out of scope):

1. **Import-path check** — ``build_privacy_filter`` may only be imported
   from ``src/screencap/privacy/filter.py`` (the declaration site) and
   ``src/screencap/exporter.py`` (the backward-compat re-export). Any
   other import within ``src/screencap/`` fails.

2. **Literal-``None`` window_filter check** — calls to
   ``unified_export_events(...)`` from ``src/screencap/chunk_processor.py``
   and ``src/screencap/cli.py`` must NOT pass ``window_filter=None`` as
   a literal ``ast.Constant(value=None)``. ``CaptureSession.export_events``
   in ``src/screencap/engine/capture.py`` IS the legitimate
   ``window_filter=None`` site (CLI export), exempted by file path.

3. **Cloud-filter exclusivity** — calls to ``build_privacy_filter(...)``
   with ``cloud_intent=False`` (literal ``False``) from any caller other
   than ``build_cloud_window_filter`` itself fail. The factory is the
   only sanctioned constructor for cloud-mode filters.

**Definition of "literally `None`":** the checks operate on the AST
keyword-argument node value. They detect the literal ``None`` token
(``ast.Constant(value=None)``) as a kwarg value. Variables that happen
to evaluate to ``None`` at runtime are NOT flagged — those are out of
scope for static analysis. With Unit 1's revised factory shape
(``build_cloud_window_filter`` returning ``None`` when
``cloud_bound=False``), Units 6 and 7 wire the factory unconditionally:
no caller-side ``if/else`` to forget, no literal ``None`` at any
cloud-bound call site.

**Known limitation:** the guard catches import-path violations and
literal ``None`` in kwarg position, but does NOT catch logic
copy-paste — a future caller could open-code the
``DefaultPolicyEvaluator`` + ``DefaultContextClassifier`` closure pattern
inline. The Slack-leak prior incident was exactly this shape. Code
review is the second control for that vector; the architecture doc
(Unit 10) names the factory as the only sanctioned constructor.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Scope: src/screencap/ only
# ---------------------------------------------------------------------------


def _src_root() -> Path:
    """Return the absolute path to ``src/screencap/`` (repo-relative)."""
    # Tests live under ``tests/``; the source tree is one directory up.
    return Path(__file__).resolve().parent.parent / "src" / "screencap"


def _iter_python_files(root: Path) -> Iterable[Path]:
    """Yield every ``*.py`` file under ``root`` recursively."""
    yield from sorted(root.rglob("*.py"))


def _parse_file(path: Path) -> ast.AST:
    """Parse a Python file into an AST. Skip files that fail to parse —
    those have other failures the regular test suite will catch."""
    return ast.parse(path.read_text(), filename=str(path))


def _rel(path: Path) -> str:
    """Render ``path`` relative to the repo root for human-friendly errors."""
    repo_root = _src_root().parent.parent
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _find_calls(tree: ast.AST, name: str) -> list[tuple[int, ast.Call]]:
    """Find every ``ast.Call`` to a function named ``name``.

    Matches both ``name(...)`` and ``module.name(...)`` invocations
    (the latter via ``ast.Attribute(attr=name)``). Returns
    ``(lineno, call_node)`` pairs for every match — the lineno makes
    failure messages locate the offending site directly.
    """
    matches: list[tuple[int, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            matches.append((node.lineno, node))
        elif isinstance(func, ast.Attribute) and func.attr == name:
            matches.append((node.lineno, node))
    return matches


def _find_imports(tree: ast.AST, target_name: str) -> list[tuple[int, str]]:
    """Find every ``from X import target_name`` statement.

    Returns ``(lineno, source_module)`` pairs. ``source_module`` is the
    dotted path that the symbol was imported from (the ``X`` in
    ``from X import target_name``). For ``import X`` style imports we
    only report the module path because the symbol is accessed via
    attribute lookup later (callers concerned with that go through the
    call-graph check instead).
    """
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == target_name:
                    matches.append((node.lineno, module))
    return matches


def _kwarg_value(call: ast.Call, name: str) -> ast.AST | None:
    """Return the AST value passed as ``name=value`` in ``call``, or
    ``None`` if no such keyword argument is present."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_literal_none(value: ast.AST | None) -> bool:
    """True iff ``value`` is the literal Python token ``None``.

    Specifically: ``ast.Constant(value=None)``. Variables that happen
    to evaluate to ``None`` at runtime are NOT flagged — out of scope
    for static analysis (per the spec).
    """
    return isinstance(value, ast.Constant) and value.value is None


def _is_literal_false(value: ast.AST | None) -> bool:
    """True iff ``value`` is the literal Python token ``False``.

    Specifically: ``ast.Constant(value=False)``. Same scope discipline
    as :func:`_is_literal_none`.
    """
    return isinstance(value, ast.Constant) and value.value is False


# ---------------------------------------------------------------------------
# Allow-lists (file paths). Resolved against ``src/screencap/`` root.
# ---------------------------------------------------------------------------


# build_privacy_filter declaration + sanctioned re-export site.
_BUILD_PRIVACY_FILTER_HOMES = {
    "privacy/filter.py",   # declaration
    "exporter.py",         # backward-compat re-export
}

# unified_export_events callers that legitimately pass window_filter=None
# as a literal None constant (CLI export — no privacy filter applied).
_UNIFIED_EXPORT_NONE_OK = {
    "engine/capture.py",   # CaptureSession.export_events (CLI export path)
}

# build_privacy_filter(cloud_intent=...) with literal False is allowed only in:
_CLOUD_FALSE_FACTORY_HOMES = {
    "privacy/filter.py",   # build_cloud_window_filter dispatch lives here
}


def _rel_to_src(path: Path) -> str:
    """Path relative to ``src/screencap/`` (POSIX-style for portability)."""
    return path.relative_to(_src_root()).as_posix()


# ---------------------------------------------------------------------------
# Static check (a): import-path of build_privacy_filter
# ---------------------------------------------------------------------------


class TestBuildPrivacyFilterImportPath:
    """``build_privacy_filter`` may only be imported from
    ``screencap.privacy.filter`` (declaration) and ``screencap.exporter``
    (backward-compat re-export). Any other import path within
    ``src/screencap/`` is a regression of Unit 1's contract.
    """

    def test_no_unsanctioned_imports_in_src(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            # The declaration / re-export sites are exempt — they
            # define or re-publish the symbol; they do not "import" it
            # in the policy-relevant sense (the re-export is the
            # backward-compat path other code is allowed to depend on).
            if rel in _BUILD_PRIVACY_FILTER_HOMES:
                continue

            tree = _parse_file(py_path)
            for lineno, source in _find_imports(tree, "build_privacy_filter"):
                if source not in {
                    "screencap.privacy.filter",
                    "screencap.exporter",
                }:
                    violations.append(
                        f"{_rel(py_path)}:{lineno} imports build_privacy_filter "
                        f"from {source!r} — only screencap.privacy.filter "
                        "(declaration) and screencap.exporter "
                        "(backward-compat re-export) are sanctioned. "
                        "Cloud-bound callers must use "
                        "build_cloud_window_filter from "
                        "screencap.privacy.filter."
                    )
        assert not violations, (
            "Unsanctioned build_privacy_filter import(s):\n  - "
            + "\n  - ".join(violations)
        )


# ---------------------------------------------------------------------------
# Static check (b): no literal window_filter=None at cloud-bound call sites
# ---------------------------------------------------------------------------


class TestUnifiedExportNoLiteralNoneFilter:
    """Calls to ``unified_export_events(...)`` from cloud-bound files
    (``chunk_processor.py``, ``cli.py``) must NOT pass
    ``window_filter=None`` as a literal ``ast.Constant(value=None)``.

    The exempt site is ``CaptureSession.export_events`` in
    ``engine/capture.py`` (CLI export, which legitimately passes
    ``None``).
    """

    def test_no_literal_none_window_filter_in_cloud_callers(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            if rel in _UNIFIED_EXPORT_NONE_OK:
                continue

            tree = _parse_file(py_path)
            for lineno, call in _find_calls(tree, "unified_export_events"):
                value = _kwarg_value(call, "window_filter")
                if _is_literal_none(value):
                    violations.append(
                        f"{_rel(py_path)}:{lineno} calls "
                        "unified_export_events(window_filter=None) — "
                        "cloud-bound callers must construct the filter "
                        "via build_cloud_window_filter (which returns "
                        "None for non-cloud, the cloud-mode filter for "
                        "cloud) so the call site is unconditional. "
                        "Only engine/capture.py (CLI export) is allowed "
                        "to pass a literal None."
                    )
        assert not violations, (
            "Literal window_filter=None at cloud-bound call site(s):\n  - "
            + "\n  - ".join(violations)
        )


# ---------------------------------------------------------------------------
# Static check (c): build_cloud_window_filter exclusivity
# ---------------------------------------------------------------------------


class TestCloudWindowFilterFactoryExclusivity:
    """``build_cloud_window_filter`` is the only sanctioned constructor
    for cloud-mode window filters. ``build_privacy_filter(cloud_intent
    =False, ...)`` (or any literal ``cloud_intent=False`` from any
    caller other than the factory itself) is a fail-OPEN signal.

    Note: ``build_privacy_filter`` is still callable for the CLI's
    legacy ``screencap export`` pathway with the default
    ``cloud_intent`` (omitted entirely or implicit). The check fires
    only on a LITERAL ``False`` constant, which signals a deliberate
    bypass of the factory.
    """

    def test_no_literal_cloud_intent_false_outside_factory(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            if rel in _CLOUD_FALSE_FACTORY_HOMES:
                # The factory itself dispatches on cloud_bound. It is
                # allowed to call build_privacy_filter with cloud_intent
                # arguments (literal True is the cloud branch; the
                # non-cloud branch returns None before any call).
                continue

            tree = _parse_file(py_path)
            for lineno, call in _find_calls(tree, "build_privacy_filter"):
                value = _kwarg_value(call, "cloud_intent")
                if _is_literal_false(value):
                    violations.append(
                        f"{_rel(py_path)}:{lineno} calls "
                        "build_privacy_filter(cloud_intent=False) — "
                        "only build_cloud_window_filter (in "
                        "screencap.privacy.filter) is the sanctioned "
                        "constructor. Cloud-bound callers must use the "
                        "factory; non-cloud paths should not pass "
                        "cloud_intent at all (defaults to False)."
                    )
        assert not violations, (
            "Literal cloud_intent=False outside the factory:\n  - "
            + "\n  - ".join(violations)
        )


# ---------------------------------------------------------------------------
# Self-test — the checker must catch deliberate violations
# ---------------------------------------------------------------------------


class TestCheckerSensitivity:
    """The static checker must actually detect the patterns it claims to
    detect. These tests inject deliberate violations into in-memory ASTs
    and assert the checker functions flag them. Pins the checker's
    sensitivity so a future refactor that loosens the AST helpers (e.g.,
    matching only ``ast.Name`` and missing ``ast.Attribute`` calls)
    cannot silently disable the guard.
    """

    def test_find_calls_matches_bare_name_call(self):
        tree = ast.parse("unified_export_events(rows, [], window_filter=None)")
        matches = _find_calls(tree, "unified_export_events")
        assert len(matches) == 1
        assert matches[0][0] == 1  # line 1

    def test_find_calls_matches_attribute_call(self):
        tree = ast.parse(
            "screencap.engine.export.unified_export_events(rows, [], "
            "window_filter=None)"
        )
        matches = _find_calls(tree, "unified_export_events")
        assert len(matches) == 1

    def test_is_literal_none_detects_constant_none(self):
        tree = ast.parse("f(x=None)")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        assert _is_literal_none(_kwarg_value(call, "x"))

    def test_is_literal_none_rejects_variable_named_none(self):
        """Variables that happen to evaluate to None at runtime are NOT
        flagged. The checker matches only the literal ``None`` token.
        """
        tree = ast.parse("f(x=my_var)")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        assert not _is_literal_none(_kwarg_value(call, "x"))

    def test_is_literal_false_detects_constant_false(self):
        tree = ast.parse("f(cloud_intent=False)")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        assert _is_literal_false(_kwarg_value(call, "cloud_intent"))

    def test_is_literal_false_rejects_zero(self):
        """``0`` is not the same constant as ``False`` for our purposes —
        the checker is about the literal ``False`` token specifically."""
        tree = ast.parse("f(x=0)")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        # ast.Constant(value=0) is not value=False
        assert not _is_literal_false(_kwarg_value(call, "x"))

    def test_find_imports_detects_violating_import_pattern(self, tmp_path):
        """A deliberately-injected violation file would be flagged by
        the import-path check.

        We construct a fake source file that imports
        ``build_privacy_filter`` from a forbidden location and confirm
        ``_find_imports`` reports the bad source module.
        """
        bad_file = tmp_path / "fake_bad_caller.py"
        bad_file.write_text(textwrap.dedent("""
            from screencap.some.other.location import build_privacy_filter

            def use_it():
                return build_privacy_filter(cloud_intent=True)
        """).strip())

        tree = _parse_file(bad_file)
        imports = _find_imports(tree, "build_privacy_filter")
        assert len(imports) == 1
        lineno, source = imports[0]
        assert source == "screencap.some.other.location", (
            "_find_imports must report the source module so failure "
            "messages locate the violation."
        )

    def test_literal_none_kwarg_violation_pattern_caught(self, tmp_path):
        """Inject a deliberate ``unified_export_events(window_filter=None)``
        in a fake source file and confirm the call+kwarg detection chain
        flags it.
        """
        bad_file = tmp_path / "fake_bad_chunk_processor.py"
        bad_file.write_text(textwrap.dedent("""
            from screencap.engine.export import unified_export_events

            def export_chunk(rows, windows):
                return unified_export_events(
                    rows, windows, window_filter=None,
                )
        """).strip())

        tree = _parse_file(bad_file)
        calls = _find_calls(tree, "unified_export_events")
        assert len(calls) == 1
        _, call = calls[0]
        value = _kwarg_value(call, "window_filter")
        assert _is_literal_none(value), (
            "The combination (find_calls + kwarg_value + is_literal_none) "
            "must flag a literal None in window_filter kwarg position."
        )

    def test_literal_cloud_intent_false_violation_caught(self, tmp_path):
        """Inject a fake ``build_privacy_filter(cloud_intent=False)`` and
        confirm the chain flags it."""
        bad_file = tmp_path / "fake_bad_caller.py"
        bad_file.write_text(textwrap.dedent("""
            from screencap.privacy.filter import build_privacy_filter

            def make_filter():
                return build_privacy_filter(
                    privacy_mode='public', cloud_intent=False,
                )
        """).strip())

        tree = _parse_file(bad_file)
        calls = _find_calls(tree, "build_privacy_filter")
        assert len(calls) == 1
        _, call = calls[0]
        value = _kwarg_value(call, "cloud_intent")
        assert _is_literal_false(value), (
            "The chain must flag a literal False in cloud_intent kwarg "
            "position so a future caller bypassing the factory fails CI."
        )


# ---------------------------------------------------------------------------
# Smoke: the checker covers a non-trivial number of files
# ---------------------------------------------------------------------------


class TestCheckerCoverage:
    """Sanity: the AST walker actually traverses ``src/screencap/``.

    Without this, an import-path bug in ``_src_root`` could silently
    return zero files and every check would vacuously pass.
    """

    def test_scope_finds_screencap_source_files(self):
        files = list(_iter_python_files(_src_root()))
        # The tree contains dozens of modules; pinning a lower bound of
        # 20 protects against an accidental empty walk without being
        # brittle against future refactors.
        assert len(files) >= 20, (
            f"Expected to find >=20 files under src/screencap/, got "
            f"{len(files)} — _src_root() is probably wrong."
        )
        # Spot-check that the call sites we care about actually exist.
        rels = {_rel_to_src(p) for p in files}
        assert "chunk_processor.py" in rels
        assert "cli.py" in rels
        assert "engine/capture.py" in rels
        assert "engine/export.py" in rels
        assert "privacy/filter.py" in rels
        assert "exporter.py" in rels
