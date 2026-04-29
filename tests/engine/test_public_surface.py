"""Regression tests for the curated ``screencap.engine`` import surface.

Plain ``import screencap.engine`` must not transitively load heavy
third-party deps (pynput, av, mss, matplotlib, numpy, PIL) or the engine
submodules whose presence is the leading indicator of regression
(``visualize.demo`` for PIL, ``comparison`` for numpy/matplotlib).

Subprocess isolation is required because pytest itself imports many of
these modules — the parent process's ``sys.modules`` would always be dirty.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Watchlist (prefix-match against sys.modules keys). Update with care:
# every entry is a documented architectural boundary, not noise.
HEAVY_IMPORT_PREFIXES: tuple[str, ...] = (
    "pynput",
    "av",
    "mss",
    "matplotlib",
    "numpy",
    "PIL",
    "screencap.engine.visualize.demo",
    "screencap.engine.comparison",
)


def test_engine_import_does_not_pull_heavy_deps() -> None:
    """``import screencap.engine`` must not eagerly load heavy submodules.

    Assumes ``pip install -e ".[dev]"`` has been run from the repo root so the
    child interpreter's site-packages exposes ``screencap``. CI runs in that
    posture; local devs running ``pytest`` do too.
    """
    code = (
        "import screencap.engine, sys\n"
        f"prefixes = {HEAVY_IMPORT_PREFIXES!r}\n"
        "leaked = sorted(m for m in sys.modules if m.startswith(prefixes))\n"
        "if leaked: print(leaked, file=sys.stderr)\n"  # diagnostic on failure only
        "sys.exit(1 if leaked else 0)\n"
    )
    # Suppress coverage's subprocess bootstrap so its sitecustomize-driven
    # preloads don't colour the result.
    env = {**os.environ, "COVERAGE_PROCESS_START": ""}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"Heavy deps leaked into `import screencap.engine`: {result.stderr.strip()!r}"
    )
