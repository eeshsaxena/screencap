"""Import-lightness guards for the privacy/redaction split (SCR-33 U1).

The whole point of moving the 488-line detection engine out of
``screencap.privacy.__init__`` is that importing a small shared symbol must
no longer drag in the heavy engine. These tests pin that property:

- Importing ``screencap.privacy`` (or a leaf submodule like
  ``screencap.privacy.actions``) must NOT import
  ``screencap.redaction.engine`` — the shim's ``__getattr__`` is lazy.
- The legacy ``from screencap.privacy import <engine symbol>`` path must still
  resolve via the shim and return the *identical* object the engine defines
  (object identity, not a copy — critical for ``except
  AllDetectorsFailedError``).

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


def test_shim_resolves_and_is_engine_object() -> None:
    """Legacy shim access returns the identical engine object."""
    result = _run(
        "from screencap.privacy import are_nlp_models_cached\n"
        "import screencap.redaction.engine as engine\n"
        "assert are_nlp_models_cached is engine.are_nlp_models_cached\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_all_detectors_failed_error_class_identity() -> None:
    """The shim must return the IDENTICAL exception class (``is``), not a copy.

    If the shim returned a copy, ``except AllDetectorsFailedError`` in code
    that imported it via ``screencap.privacy`` would silently stop matching a
    detector failure raised by the engine.
    """
    result = _run(
        "import screencap.privacy\n"
        "import screencap.redaction.engine as engine\n"
        "assert screencap.privacy.AllDetectorsFailedError "
        "is engine.AllDetectorsFailedError\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
