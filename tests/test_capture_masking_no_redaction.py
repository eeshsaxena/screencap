"""Guard: the capture-time masking primitive path must not pull in redaction.

``RecorderPrivacyFilter.mask_frame`` runs live during capture and imports
its pixel/region primitives from ``screencap.privacy.mask_primitives``
(SCR-33 U2). Those primitives live in the shared ``screencap.privacy``
leaf precisely so the capture path never depends on
``screencap.redaction`` — importing redaction would drag ML/NLP deps into
the recording engine and violate the package DAG.

This asserts, in a fresh subprocess (so import state is pristine), that
importing the primitive module does not transitively import
``screencap.redaction.masking``.
"""

from __future__ import annotations

import subprocess
import sys


def test_mask_primitives_import_does_not_pull_redaction_masking():
    code = (
        "import screencap.privacy.mask_primitives\n"
        "import sys\n"
        "assert 'screencap.redaction.masking' not in sys.modules, "
        "    sorted(m for m in sys.modules if 'redaction' in m)\n"
        "print('capture path clean')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "capture path clean" in result.stdout
