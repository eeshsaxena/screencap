"""Shared helpers for the SCR-76 capture-health test modules.

Imported by ``tests/test_capture_health.py`` and
``tests/test_capture_health_frozen_dispatch.py`` (SCR-76 follow-up 006 — these
were previously duplicated verbatim in both files). Not a test module itself
(no ``test_`` prefix), so pytest does not collect it.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import textwrap
from io import StringIO

# Default per-reader liveness map for the pure-step / tick drivers.
_ALIVE = {"screen": True, "window": True, "action": True}


def _capture_stderr(callable_):
    """Run ``callable_`` with ``sys.stderr`` captured; return the emitted JSON
    event dicts (one per non-blank line)."""
    buf = StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        callable_()
    finally:
        sys.stderr = saved
    return [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]


def _referenced_names(fn) -> set[str]:
    """Names a function's CODE references (imports, attributes, identifiers),
    excluding string/docstring content — so structural guards check what the
    code does, not what its prose mentions."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names
