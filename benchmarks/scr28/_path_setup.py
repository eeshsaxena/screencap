"""Shared sys.path bootstrap for the SCR-28 spike scripts.

The scripts under ``benchmarks/scr28/`` are run directly (``python
benchmarks/scr28/run_gliner.py``) rather than as a package, so they need the
project root (for ``tests.privacy.fixtures``), the repo ``src/`` (for
``screencap``), and their own directory (for sibling flat modules like
``schema``/``label_maps``) on ``sys.path``. Importing this module performs that
insertion as a side effect — ``import _path_setup`` near the top of each script
deduplicates the boilerplate. The scripts' own directory is already on
``sys.path[0]`` when run as scripts, so the bare ``import _path_setup`` resolves.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCR28_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCR28_DIR.parents[1]

for _p in (_PROJECT_ROOT / "src", _PROJECT_ROOT, _SCR28_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
