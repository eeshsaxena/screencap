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
interpolated value must be escaped, a structurally-safe shape (int count, enum
``.value``, ALL-CAPS module constant), or an allow-listed internal constant.
"""

from __future__ import annotations

import ast
import re
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
_RAW_INTERNAL_CONSTANTS = frozenset(
    {
        "result.state",  # daemon DaemonState label (fixed internal vocabulary)
        "count",  # int tally from a ``drops.items()`` loop
        "event_type",  # internal event-type identifier (dict key)
        "valid",  # ``_CHOICE_KEYS[key]``: the frozenset of valid choice strings
        "mode",  # internal privacy-mode label ("internal"/"cloud"/...)
        "joined",  # ``", ".join(...)`` of internal placeholder field names
        "name",  # smoke-test check function ``__name__`` (internal)
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
    int counts (``len()``/``int()`` and numeric literals), enum ``.value``
    labels, and ALL-CAPS module constants.
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
    # Enum value label, e.g. ``state.value``.
    if isinstance(node, ast.Attribute) and node.attr == "value":
        return True
    # Module constant: ``_PRIVACY_MODE_VALUES`` or ``launchagent.STATE_NOT_LOADED``.
    if isinstance(node, ast.Name) and _CONST_NAME.match(node.id):
        return True
    if isinstance(node, ast.Attribute) and _CONST_NAME.match(node.attr):
        return True
    return False


def _unparse(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return "<unparseable>"


def _iter_error_sink_interpolations():
    """Yield ``(lineno, expr_node)`` for every interpolation inside an
    error/warning ``console.print`` / ``err_console.print`` f-string sink.
    """
    tree = ast.parse(_CLI_PATH.read_text(encoding="utf-8"), filename=str(_CLI_PATH))
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
        if _is_escape_call(expr):
            continue
        if _is_structurally_safe(expr):
            continue
        if _unparse(expr) in _RAW_INTERNAL_CONSTANTS:
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
