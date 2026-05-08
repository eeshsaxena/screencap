"""CI structural guard against fail-OPEN regressions in cloud export.

Unit 8 of the unified-export-callable refactor. The byte-identical
contract test (``tests/test_unified_export_contract.py``) catches drift
between the three KNOWN callers of ``unified_export_events``. It cannot
catch a hypothetical FOURTH caller wired up incorrectly. This file is
the structural enforcement that fails CI when a future caller bypasses
the sanctioned ``build_cloud_window_filter`` factory.

Four static checks, each implemented as an AST walker scoped to
``src/screencap/`` only (``tests/``, ``scripts/``, and ``benchmarks/``
are out of scope):

1. **Import-path check** — ``build_privacy_filter`` may only be imported
   from ``src/screencap/privacy/filter.py`` (the declaration site). Any
   other import within ``src/screencap/`` fails. The historical
   backward-compat re-export from ``src/screencap/exporter.py`` was
   removed in todo 019; ``screencap.privacy.filter`` is now the single
   sanctioned import location.

2. **window_filter kwarg-presence check** — calls to
   ``unified_export_events(...)`` from cloud-bound files (every file
   under ``src/screencap/`` except those in ``_UNIFIED_EXPORT_NONE_OK``)
   must EXPLICITLY pass a ``window_filter=`` keyword argument, and that
   value must NOT be a literal ``ast.Constant(value=None)``. Two
   regression vectors are caught:

   a. **Omitted kwarg** — ``unified_export_events(rows, windows)`` with
      no ``window_filter=`` slides through the runtime defaulting to
      ``window_filter=None`` and leaks titles. The check now demands
      explicit kwarg presence at every cloud-bound call site.
   b. **Literal None kwarg** — ``unified_export_events(..., window_filter
      =None)`` is the cosmetic regression the previous check caught and
      remains caught.

   ``CaptureSession.export_events`` in ``src/screencap/engine/capture.py``
   IS the legitimate ``window_filter=None`` site (CLI export, no privacy
   filter applied), exempted by file path via ``_UNIFIED_EXPORT_NONE_OK``.

3. **build_privacy_filter call-site exclusivity** — every call to
   ``build_privacy_filter(...)`` from a file other than
   ``src/screencap/privacy/filter.py`` is a violation, regardless of
   arguments. Production code must always go through
   ``build_cloud_window_filter``. The historical exporter re-export of
   ``build_privacy_filter`` was removed in todo 019; the only sanctioned
   import location is ``screencap.privacy.filter``.

4. **Legacy literal-``cloud_intent=False`` check** — preserved for
   defense in depth alongside check (3): a literal ``cloud_intent=False``
   constant from outside the factory home is also flagged. With check
   (3) in place this is moot for production code (no production caller
   may invoke ``build_privacy_filter`` at all), but the helper still
   matters for the sensitivity self-tests below and documents the
   original Unit 8 enforcement contract.

**Definition of "literally `None`":** the checks operate on the AST
keyword-argument node value. They detect the literal ``None`` token
(``ast.Constant(value=None)``) as a kwarg value. Variables that happen
to evaluate to ``None`` at runtime are NOT flagged — those are out of
scope for static analysis. With Unit 1's revised factory shape
(``build_cloud_window_filter`` returning ``None`` when
``cloud_bound=False``), Units 6 and 7 wire the factory unconditionally:
no caller-side ``if/else`` to forget, no literal ``None`` at any
cloud-bound call site.

**Known limitations.** The AST walker matches ``ast.Call`` nodes by
direct name or attribute (``foo(...)`` and ``mod.foo(...)``). It does
NOT follow:

- **Local ``as`` aliasing.** ``from screencap.engine.export import
  unified_export_events as fn`` then ``fn(rows, windows, window_filter
  =None)`` — the walker sees ``Call(func=Name("fn"), ...)``, not
  ``unified_export_events``, and the call escapes detection.
- **Variable-assignment aliasing.** ``_alias = unified_export_events``
  followed by ``_alias(...)`` — same blind spot.
- **Dynamic imports / ``getattr`` lookups.** ``importlib.import_module
  ("screencap.engine.export").unified_export_events(...)`` or
  ``getattr(mod, "unified_export_events")(...)`` — the symbol name
  appears only as a string literal, invisible to AST-name matching.
- **Logic copy-paste.** A future caller could open-code the
  ``DefaultPolicyEvaluator`` + ``DefaultContextClassifier`` closure
  pattern inline. The Slack-leak prior incident was exactly this shape.

These vectors are mitigated by code review; ``build_cloud_window_filter``
is the only sanctioned constructor. A reviewer who sees an alias or a
dynamic lookup of either ``unified_export_events`` or
``build_privacy_filter`` in a new code path should treat it as a
warning sign and reject the change unless the call goes through the
factory.
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


# build_privacy_filter declaration site. The historical backward-compat
# re-export from ``exporter.py`` was removed in todo 019; the canonical
# location is now the single sanctioned import path.
_BUILD_PRIVACY_FILTER_HOMES = {
    "privacy/filter.py",   # declaration
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
    ``screencap.privacy.filter`` (the declaration site). Any other import
    path within ``src/screencap/`` is a regression of Unit 1's contract.
    The historical backward-compat re-export from ``screencap.exporter``
    was removed in todo 019; ``screencap.privacy.filter`` is now the
    single sanctioned location.
    """

    def test_no_unsanctioned_imports_in_src(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            # The declaration site is exempt — it defines the symbol; it
            # does not "import" it in the policy-relevant sense.
            if rel in _BUILD_PRIVACY_FILTER_HOMES:
                continue

            tree = _parse_file(py_path)
            for lineno, source in _find_imports(tree, "build_privacy_filter"):
                if source != "screencap.privacy.filter":
                    violations.append(
                        f"{_rel(py_path)}:{lineno} imports build_privacy_filter "
                        f"from {source!r} — only screencap.privacy.filter "
                        "(declaration) is sanctioned. Cloud-bound callers "
                        "must use build_cloud_window_filter from "
                        "screencap.privacy.filter."
                    )
        assert not violations, (
            "Unsanctioned build_privacy_filter import(s):\n  - "
            + "\n  - ".join(violations)
        )


# ---------------------------------------------------------------------------
# Static check (b): unified_export_events must explicitly pass
# ``window_filter=`` (and not literal ``None``) at every cloud-bound call site.
# ---------------------------------------------------------------------------


def _has_kwarg(call: ast.Call, name: str) -> bool:
    """True iff ``call`` lists ``name=`` in its keyword arguments.

    ``_kwarg_value`` returns Python ``None`` for both "absent" and
    "explicitly passed as the literal None token" — indistinguishable
    by the helper alone. This predicate disambiguates: it answers the
    question "did the source code spell out ``name=``?" regardless of
    the value bound to it. Lets check (b) flag *omitted* kwargs (the
    fail-OPEN regression that defaults to the function's
    ``window_filter=None``) separately from literal ``None`` kwargs
    (the cosmetic regression that still passes the typecheck and
    silently leaks)."""
    return any(kw.arg == name for kw in call.keywords)


class TestUnifiedExportRequiresWindowFilterKwarg:
    """Every call to ``unified_export_events(...)`` from a cloud-bound
    file (every file under ``src/screencap/`` except those listed in
    ``_UNIFIED_EXPORT_NONE_OK``) must EXPLICITLY pass ``window_filter=``
    as a keyword argument. The value passed must NOT be a literal
    ``ast.Constant(value=None)``.

    Two failure modes caught here:

    1. **Omitted kwarg** — ``unified_export_events(rows, windows)``
       falls through to the function's default ``window_filter=None``
       and silently leaks titles to cloud JSONL. The previous version
       of this check did NOT catch this (``_kwarg_value`` returns
       Python ``None`` for absent kwargs, ``_is_literal_none(None)``
       returns ``False``). This is the more likely real-world
       regression.
    2. **Literal None kwarg** — ``unified_export_events(...,
       window_filter=None)`` is a deliberate (or careless) bypass of
       ``build_cloud_window_filter``. Caught both by the previous
       check and here.

    The exempt site is ``CaptureSession.export_events`` in
    ``engine/capture.py`` (CLI export, which legitimately passes
    ``window_filter=None`` because no privacy filter applies to the
    full-export pathway). It is exempted by file path via
    ``_UNIFIED_EXPORT_NONE_OK``.
    """

    def test_window_filter_kwarg_required_in_cloud_callers(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            if rel in _UNIFIED_EXPORT_NONE_OK:
                continue

            tree = _parse_file(py_path)
            for lineno, call in _find_calls(tree, "unified_export_events"):
                if not _has_kwarg(call, "window_filter"):
                    violations.append(
                        f"{_rel(py_path)}:{lineno} calls "
                        "unified_export_events(...) without an explicit "
                        "window_filter= kwarg — the function defaults to "
                        "window_filter=None, which silently leaks window "
                        "titles to cloud JSONL. Cloud-bound callers must "
                        "construct the filter via build_cloud_window_filter "
                        "(which returns None for non-cloud, the cloud-mode "
                        "filter for cloud) and pass it explicitly. Only "
                        "engine/capture.py (CLI export) may omit the kwarg."
                    )
                    continue
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
            "unified_export_events kwarg-presence violation(s):\n  - "
            + "\n  - ".join(violations)
        )


# ---------------------------------------------------------------------------
# Static check (c): build_privacy_filter call-site exclusivity
# ---------------------------------------------------------------------------


class TestBuildPrivacyFilterCallSiteExclusivity:
    """``build_privacy_filter`` is module-private to the factory home
    (``src/screencap/privacy/filter.py``). Every call from any other
    file under ``src/screencap/`` is a violation, regardless of the
    arguments passed.

    Why this is stricter than checking ``cloud_intent`` literally:
    even with the import-path check (a) limiting imports to
    ``screencap.privacy.filter``, a future production caller could
    legitimately import the symbol and then bypass the factory::

        from screencap.privacy.filter import build_privacy_filter  # passes (a)
        f = build_privacy_filter(cloud_intent=True, privacy_mode="public")

    The literal-``False`` check (d) does not fire (this is literal
    ``True``). The kwarg-presence check (b) does not fire (this is not
    a call to ``unified_export_events``). The factory bypass would be
    invisible to checks (a), (b), and (d).

    With this check in place, the only way a production file can
    construct a privacy filter is via ``build_cloud_window_filter`` (or
    by living inside ``privacy/filter.py`` itself). Tests are out of
    scope for this scanner — they may freely import and call
    ``build_privacy_filter`` for sensitivity coverage and the
    literal-``False`` regression vector."""

    def test_no_build_privacy_filter_calls_outside_factory_home(self):
        violations: list[str] = []
        src = _src_root()
        for py_path in _iter_python_files(src):
            rel = _rel_to_src(py_path)
            if rel in _BUILD_PRIVACY_FILTER_HOMES:
                # The declaration site (privacy/filter.py) defines the
                # symbol and contains the factory's internal dispatch.
                continue

            tree = _parse_file(py_path)
            for lineno, _call in _find_calls(tree, "build_privacy_filter"):
                violations.append(
                    f"{_rel(py_path)}:{lineno} calls build_privacy_filter "
                    "directly — only build_cloud_window_filter (in "
                    "screencap.privacy.filter) is the sanctioned "
                    "constructor. Cloud-bound callers must use the "
                    "factory; non-cloud paths must not construct a "
                    "filter directly."
                )
        assert not violations, (
            "build_privacy_filter call(s) outside the factory home:\n  - "
            + "\n  - ".join(violations)
        )


# ---------------------------------------------------------------------------
# Static check (d): legacy literal-``cloud_intent=False`` outside factory
# ---------------------------------------------------------------------------


class TestCloudWindowFilterFactoryExclusivity:
    """Legacy literal-``cloud_intent=False`` check. With check (c) in
    place this is moot for production code (no production caller may
    invoke ``build_privacy_filter`` at all), but the helper machinery
    (``_is_literal_false`` + ``_kwarg_value``) and this check still
    matter for the sensitivity self-tests below — they exercise the
    chain that catches the original Unit 8 ``cloud_intent=False``
    regression vector.

    A literal ``False`` constant from outside the factory home is
    flagged. Variables that happen to evaluate to ``False`` at runtime
    are out of scope for static analysis, per the same scope discipline
    as :func:`_is_literal_none`.
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

    # ------------------------------------------------------------------
    # Sensitivity tests for the new "kwarg presence" behavior (todo 001)
    # ------------------------------------------------------------------

    def test_has_kwarg_detects_explicit_keyword(self):
        """``_has_kwarg`` must report True when the kwarg is spelled out
        in the source — even if its value is the literal ``None``. The
        check disambiguates "absent in source" from "present and bound
        to None"; both are violations for cloud-bound callers but the
        kwarg-presence check fires only on absence (the literal-None
        branch fires on the value-side check).
        """
        tree = ast.parse("f(a, b, window_filter=None)")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        assert _has_kwarg(call, "window_filter")

    def test_has_kwarg_returns_false_when_omitted(self):
        """When the source code omits the kwarg entirely, the helper
        must report False — this is the omitted-kwarg fail-OPEN vector
        the new check guards against."""
        tree = ast.parse("f(a, b)")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        assert not _has_kwarg(call, "window_filter")

    def test_omitted_window_filter_kwarg_violation_caught(self, tmp_path):
        """Inject a deliberate ``unified_export_events(rows, windows)``
        with NO ``window_filter=`` kwarg in a fake source file and
        confirm the kwarg-presence check would flag it.

        This is the todo-001 fail-OPEN vector: the previous version of
        the check only flagged literal ``None`` values, so an omitted
        kwarg fell through to the function's default
        ``window_filter=None`` and silently leaked titles."""
        bad_file = tmp_path / "fake_bad_chunk_processor_omitted.py"
        bad_file.write_text(textwrap.dedent("""
            from screencap.engine.export import unified_export_events

            def export_chunk(rows, windows):
                return unified_export_events(rows, windows)
        """).strip())

        tree = _parse_file(bad_file)
        calls = _find_calls(tree, "unified_export_events")
        assert len(calls) == 1
        _, call = calls[0]
        assert not _has_kwarg(call, "window_filter"), (
            "_has_kwarg must report False when window_filter is omitted "
            "from the source — otherwise the new check vacuously passes "
            "for the omitted-kwarg vector and the fail-OPEN regression "
            "remains undetected."
        )
        # And the value-side helper must also report None (nothing to
        # check) so the omitted-vs-literal-None disambiguation works.
        assert _kwarg_value(call, "window_filter") is None

    def test_check_b_fails_for_fixture_with_omitted_kwarg(self, tmp_path, monkeypatch):
        """End-to-end: point ``_src_root`` at a fixture tree containing
        a fake module with ``unified_export_events(rows, windows)`` and
        confirm the check class fires.

        This is the strongest sensitivity test: it runs the actual
        check method against a fixture and asserts it raises. Without
        this, a future refactor could subtly break the wiring (e.g.,
        skip the ``not _has_kwarg`` branch) and the unit-level helper
        tests would still pass."""
        fake_src = tmp_path / "screencap"
        fake_src.mkdir(parents=True)
        # The check skips files in _UNIFIED_EXPORT_NONE_OK; pick a name
        # that is NOT in that set so the check actually scans the file.
        bad = fake_src / "fake_cloud_caller.py"
        bad.write_text(textwrap.dedent("""
            from screencap.engine.export import unified_export_events

            def export_chunk(rows, windows):
                return unified_export_events(rows, windows)
        """).strip())

        monkeypatch.setattr(
            "tests.test_privacy_filter_call_graph._src_root",
            lambda: fake_src,
        )

        import pytest as _pytest
        with _pytest.raises(AssertionError) as exc:
            TestUnifiedExportRequiresWindowFilterKwarg().test_window_filter_kwarg_required_in_cloud_callers()
        assert "without an explicit window_filter= kwarg" in str(exc.value)

    # ------------------------------------------------------------------
    # Sensitivity tests for the new call-site exclusivity check (todo 006)
    # ------------------------------------------------------------------

    def test_check_c_fails_for_fixture_with_direct_build_privacy_filter_call(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: point ``_src_root`` at a fixture tree containing
        a file that calls ``build_privacy_filter`` directly and confirm
        the new exclusivity check fires.

        The fixture file is named so that it is NOT in
        ``_BUILD_PRIVACY_FILTER_HOMES`` (so the check actually scans
        it), and the call uses ``cloud_intent=True`` to confirm the
        check fires regardless of cloud_intent value (the previous
        check only caught literal ``False``)."""
        fake_src = tmp_path / "screencap"
        fake_src.mkdir(parents=True)
        bad = fake_src / "fake_factory_bypass.py"
        bad.write_text(textwrap.dedent("""
            from screencap.privacy.filter import build_privacy_filter

            def make_filter():
                return build_privacy_filter(
                    cloud_intent=True, privacy_mode='public',
                )
        """).strip())

        monkeypatch.setattr(
            "tests.test_privacy_filter_call_graph._src_root",
            lambda: fake_src,
        )

        import pytest as _pytest
        with _pytest.raises(AssertionError) as exc:
            TestBuildPrivacyFilterCallSiteExclusivity().test_no_build_privacy_filter_calls_outside_factory_home()
        assert "calls build_privacy_filter directly" in str(exc.value)

    def test_check_c_allows_factory_home_to_call_build_privacy_filter(
        self, tmp_path, monkeypatch
    ):
        """The exclusivity check exempts files listed in
        ``_BUILD_PRIVACY_FILTER_HOMES``. A file at ``privacy/filter.py``
        in the fixture tree calling ``build_privacy_filter`` must NOT
        trigger the check — that file IS the factory home, and the
        factory dispatches to build_privacy_filter as part of its
        normal operation."""
        fake_src = tmp_path / "screencap"
        privacy_dir = fake_src / "privacy"
        privacy_dir.mkdir(parents=True)
        # privacy/filter.py is in _BUILD_PRIVACY_FILTER_HOMES -> exempt.
        ok = privacy_dir / "filter.py"
        ok.write_text(textwrap.dedent("""
            def build_privacy_filter(*args, **kwargs):
                return None

            def build_cloud_window_filter(cloud_bound, **kwargs):
                if not cloud_bound:
                    return None
                return build_privacy_filter(cloud_intent=True, **kwargs)
        """).strip())

        monkeypatch.setattr(
            "tests.test_privacy_filter_call_graph._src_root",
            lambda: fake_src,
        )

        # Must NOT raise — the call is inside the factory home.
        TestBuildPrivacyFilterCallSiteExclusivity().test_no_build_privacy_filter_calls_outside_factory_home()


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

    def test_allow_list_paths_exist_on_disk(self):
        """Every path in an allow-list must resolve to a real file under
        ``src/screencap/``. A phantom entry from a rename silently
        disables enforcement for the renamed file — the new path is no
        longer exempt (correct), but the phantom entry hides the rename
        from this coverage test, letting allow-list rot creep in over
        time. Failing fast on a missing file forces a deliberate update.
        """
        src = _src_root()
        for allow_list_name, paths in (
            ("_BUILD_PRIVACY_FILTER_HOMES", _BUILD_PRIVACY_FILTER_HOMES),
            ("_UNIFIED_EXPORT_NONE_OK", _UNIFIED_EXPORT_NONE_OK),
            ("_CLOUD_FALSE_FACTORY_HOMES", _CLOUD_FALSE_FACTORY_HOMES),
        ):
            for rel_path in paths:
                assert (src / rel_path).is_file(), (
                    f"{allow_list_name} contains '{rel_path}' but no such "
                    f"file exists under {src}. A rename probably orphaned "
                    f"the entry — update the allow-list."
                )
