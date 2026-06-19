"""Import-lightness guards for the privacy/redaction split (SCR-33).

The whole point of moving the 488-line detection engine out of
``screencap.privacy.__init__`` is that importing a small shared symbol must
no longer drag in the heavy engine. These tests pin that property:

- Importing ``screencap.privacy`` (or a leaf submodule like
  ``screencap.privacy.actions``) must NOT import
  ``screencap.redaction.engine``.

The lightness assertions use a fresh subprocess so ``sys.modules`` is clean:
another test in the same session may have already imported the engine, which
would mask a regression here.
"""

from __future__ import annotations

import subprocess
import sys


def _run(snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a Python snippet in a fresh interpreter and return the result."""
    return subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
    )


def test_importing_privacy_does_not_import_engine() -> None:
    """``import screencap.privacy`` must not pull in the heavy engine."""
    result = _run(
        "import sys\n"
        "import screencap.privacy\n"
        "assert 'screencap.redaction.engine' not in sys.modules, "
        "sorted(m for m in sys.modules if 'redaction' in m)\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_importing_privacy_actions_does_not_import_engine() -> None:
    """A leaf submodule import must not pull in the engine either."""
    result = _run(
        "import sys\n"
        "import screencap.privacy.actions\n"
        "assert 'screencap.redaction.engine' not in sys.modules, "
        "sorted(m for m in sys.modules if 'redaction' in m)\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_importing_privacy_pulls_no_ml_dependencies() -> None:
    """Frozen import budget: the shared core stays free of the NLP/ML stack.

    The cross-package edge guard (``tests/test_package_boundary_call_graph.py``)
    catches a *new edge* (privacy importing redaction), but not a heavy symbol
    re-fattening the core *in place* (e.g. someone adding a ``torch`` import
    straight into ``screencap.privacy``). This budget assertion is the durable
    defense against that regression — the original wart was exactly the core
    being heavy to import.
    """
    result = _run(
        "import sys\n"
        "import screencap.privacy\n"
        "import screencap.privacy.classify\n"
        "import screencap.privacy.mask_primitives\n"
        "heavy = [m for m in sys.modules if m.split('.')[0] in "
        "{'torch', 'transformers', 'presidio_analyzer', 'gliner', 'detect_secrets'}]\n"
        "assert not heavy, heavy\n"
        "assert 'screencap.redaction.engine' not in sys.modules\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_redaction_engine_class_identity() -> None:
    """``AllDetectorsFailedError`` is one object across its real homes.

    Consumers import it from ``screencap.redaction`` (package ``__getattr__``
    forwards to ``engine``); the ``except AllDetectorsFailedError`` in
    ``scrubber.py`` only matches if both resolve to the identical class.
    """
    result = _run(
        "import screencap.redaction as redaction\n"
        "import screencap.redaction.engine as engine\n"
        "assert redaction.AllDetectorsFailedError "
        "is engine.AllDetectorsFailedError\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
