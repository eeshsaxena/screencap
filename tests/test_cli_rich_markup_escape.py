"""Structural guard for the SCR-117 CLI Rich-markup ``escape()`` invariant.

Rich's ``Console.print`` parses its *entire* rendered string for ``[...]``
markup, including any interpolated dynamic text. A sink shaped like
``console.print(f"[red]Error:[/red] {e}")`` feeds the exception text straight
into that parser: balanced brackets get stripped (garble), and an unbalanced
``[/]`` raises ``rich.errors.MarkupError`` — which the surrounding domain
``except`` does not catch, so it crashes the command with a traceback. The
SCR-117 sweep fixed ~30 sinks one-by-one by wrapping the dynamic segment in
``rich.markup.escape``; see
``docs/solutions/runtime-errors/rich-markup-escape-cli-error-sinks-2026-06-22.md``.

Per-sink tests do not scale and let the *next* unescaped sink slip in silently
(the original sweep itself missed sinks, caught only in review of #261). This is
the structural replacement: one AST walk over ``src/screencap/cli/__init__.py``
pins the invariant for every error/warning sink at once, matching the repo's
"fewer better tests" philosophy and mirroring the call-graph guards in
``tests/test_privacy_filter_call_graph.py`` / ``tests/test_package_boundary_call_graph.py``.

Scope (the deliberate decision): **error/warning sinks only** — f-strings whose
developer-written (static) markup carries a ``[red]`` or ``[yellow]`` tag. Pure
info/success sinks interpolating dynamic text are a separate, still-open gap
(SCR-169); covering "all dynamic interpolations" here would flag ~140 in-scope-
for-SCR-169 sinks and never go green. The rule for an error/warning sink: every
interpolated value must be escaped, a structurally-safe shape (int count, or a
module-prefixed ALL-CAPS constant), or an allow-listed internal constant.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

_CLI_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "screencap"
    / "cli"
    / "__init__.py"
)

# Objects whose ``.print`` renders through Rich's markup parser.
_SINK_OBJECTS = frozenset({"console", "err_console"})

# An error/warning sink: its *static* (developer-written) markup carries a red or
# yellow tag. ``[red]``/``[yellow]``, optionally ``bold``/``bright_`` prefixed.
# This is the structural proxy for "error/warning" that keeps the guard off the
# info/success sinks tracked separately in SCR-169.
_ERROR_MARKUP = re.compile(r"\[(?:bold\s+)?(?:bright_)?(?:red|yellow)\b", re.IGNORECASE)

# Module-constant identifier: ALL-CAPS, optionally underscore-prefixed for a
# module-private constant (e.g. ``_PRIVACY_MODE_VALUES``).
_CONST_NAME = re.compile(r"^_*[A-Z][A-Z0-9_]*$")

# Bare-name interpolations that are intentionally left unescaped: each is a value
# the CLI produces from a fixed *internal* vocabulary (NOT user CLI input, an
# exception, or a repr of external data), so it cannot carry markup metacharacters
# an attacker controls. Escaping them would be a no-op that obscures intent. These
# are the cases the structural detectors below cannot prove safe from the call
# site alone (the name is bound elsewhere).
#
# Before adding an entry, confirm the expression is NOT user/exception/external
# text. When in doubt, wrap the sink in ``escape()`` instead of allow-listing it.
#
# Known limitation (accepted): matching is by the *bare unparsed expression
# string* (``_unparse(expr) in _RAW_INTERNAL_CONSTANTS``), so the exemption is
# keyed on the name's spelling, not its provenance. A future external variable
# that happened to share one of these spellings at a red/yellow sink would be
# silently exempted. That risk was deliberately minimised: the formerly generic
# ``name``/``mode``/``event_type`` entries were removed (``mode``/``event_type``
# are now ``escape()``-wrapped at their sinks; the smoke-test local was renamed to
# ``check_name``). The four entries that remain are genuinely internal *and*
# already specific enough (``result.state``/``valid``/``joined`` are unlikely
# external-data spellings; ``count`` is an int tally). Re-keying on (lineno, expr)
# was considered and rejected: it would couple the allow-list to line numbers that
# shift on every unrelated edit, making the guard brittle for no real safety gain.
_RAW_INTERNAL_CONSTANTS = frozenset(
    {
        "result.state",  # daemon DaemonState label (fixed internal vocabulary)
        "count",  # int tally from a ``drops.items()`` loop
        "valid",  # ``_CHOICE_KEYS[key]``: the frozenset of valid choice strings
        "joined",  # ``", ".join(...)`` of internal placeholder field names
    }
)


def _static_text(joined: ast.JoinedStr) -> str:
    """The concatenated literal (non-interpolated) parts of an f-string."""
    return "".join(
        part.value
        for part in joined.values
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )


def _is_escape_call(node: ast.expr) -> bool:
    """True if ``node`` is ``escape(...)`` — the markup-neutralizing wrapper."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "escape"
    )


def _is_structurally_safe(node: ast.expr) -> bool:
    """True if ``node`` provably cannot carry attacker-controlled markup.

    Covers the documented exemption categories the AST can verify at the sink:
    int counts (``len()``/``int()`` and numeric literals) and ALL-CAPS module
    constants.

    The blanket ``.value`` exemption was removed: it accepted *any* attribute
    named ``value`` regardless of receiver (so ``external_obj.value`` slipped
    through on shape alone), and no current red/yellow sink relies on it. The
    ALL-CAPS-attribute exemption is narrowed to a module-prefixed receiver — a
    bare ``Name`` (e.g. ``launchagent.STATE_NOT_LOADED``) — rather than any
    ``.ATTR`` chain, so ``external_obj.method().SOME_ATTR`` is no longer waved
    through. Anything outside these shapes must be ``escape()``-wrapped or
    allow-listed by spelling.
    """
    # ``len(...)`` / ``int(...)`` → an int count.
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"len", "int"}
    ):
        return True
    # Numeric literal.
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return True
    # Bare module constant: ``_PRIVACY_MODE_VALUES``.
    if isinstance(node, ast.Name) and _CONST_NAME.match(node.id):
        return True
    # Module-prefixed constant: ``launchagent.STATE_NOT_LOADED`` — an ALL-CAPS
    # attribute whose receiver is a plain module/name reference. Constraining the
    # receiver to a ``Name`` keeps arbitrary ``external_obj.SOME_ATTR`` chains out.
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and _CONST_NAME.match(node.attr)
    ):
        return True
    return False


def _unparse(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return "<unparseable>"


def _iter_error_sink_interpolations_in(tree: ast.AST) -> Iterator[tuple[int, ast.expr]]:
    """Yield ``(lineno, expr_node)`` for every interpolation inside an
    error/warning ``console.print`` / ``err_console.print`` f-string sink found
    in ``tree``.

    The walk is parameterised on an already-parsed tree so the negative self-test
    (:func:`test_guard_flags_a_known_bad_sink`) can drive the *identical*
    detection path over a synthetic snippet without re-reading the real CLI file.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "print"
            and isinstance(func.value, ast.Name)
            and func.value.id in _SINK_OBJECTS
        ):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.JoinedStr):
                continue
            if not _ERROR_MARKUP.search(_static_text(arg)):
                continue
            for part in arg.values:
                if isinstance(part, ast.FormattedValue):
                    yield node.lineno, part.value


def _iter_error_sink_interpolations() -> Iterator[tuple[int, ast.expr]]:
    """Yield error/warning-sink interpolations from the live CLI module."""
    tree = ast.parse(_CLI_PATH.read_text(encoding="utf-8"), filename=str(_CLI_PATH))
    yield from _iter_error_sink_interpolations_in(tree)


def _is_unescaped_violation(expr: ast.expr) -> bool:
    """True if ``expr`` at a red/yellow sink is NOT proven safe by any of the
    three exemptions (``escape()`` wrap, structural shape, allow-listed name).

    Shared by the live-file guard and the negative self-test so both exercise the
    exact same predicates — the self-test cannot drift from what CI enforces.
    """
    if _is_escape_call(expr):
        return False
    if _is_structurally_safe(expr):
        return False
    if _unparse(expr) in _RAW_INTERNAL_CONSTANTS:
        return False
    return True


def test_cli_error_sinks_escape_dynamic_markup() -> None:
    """Every dynamic interpolation in a red/yellow CLI sink is escaped, a
    structurally-safe shape, or an allow-listed internal constant.

    A failure means a new ``console.print(f"[red]...[/red] {x}")`` interpolates
    external text (user CLI input / an exception / repr of external data) without
    ``escape()`` — the exact SCR-117 garble/``MarkupError`` regression. Fix it by
    wrapping the segment: ``{escape(str(x))}``. Only add to
    ``_RAW_INTERNAL_CONSTANTS`` if the value is genuinely an internal constant.
    """
    violations: list[str] = []
    for lineno, expr in _iter_error_sink_interpolations():
        if not _is_unescaped_violation(expr):
            continue
        violations.append(
            f"src/screencap/cli/__init__.py:{lineno} interpolates "
            f"{{{_unparse(expr)}}} into a red/yellow Rich sink without escape(). "
            f"Wrap it: {{escape(str(...))}} — see SCR-117 / "
            f"docs/solutions/runtime-errors/rich-markup-escape-cli-error-sinks-2026-06-22.md"
        )
    assert not violations, (
        "Unescaped dynamic text in CLI error/warning Rich-markup sink(s):\n  - "
        + "\n  - ".join(violations)
    )


def test_guard_flags_a_known_bad_sink() -> None:
    """The detector actually fires on a deliberately-unescaped red sink.

    Without this, a silent regression in the walk or the exemption predicates
    (e.g. ``_ERROR_MARKUP`` stops matching, ``_iter_*`` yields nothing) would let
    the live-file guard pass *vacuously* forever — green while enforcing nothing.
    This drives the SAME walk + predicates over a synthetic snippet and asserts at
    least one violation, pinning the guard against rotting into a no-op.
    """
    snippet = 'console.print(f"[red]Error:[/red] {e}")'
    tree = ast.parse(snippet)
    violations = [
        expr
        for _lineno, expr in _iter_error_sink_interpolations_in(tree)
        if _is_unescaped_violation(expr)
    ]
    assert violations, "guard failed to flag a known-bad unescaped [red] sink"
