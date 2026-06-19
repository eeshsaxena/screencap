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


def test_enforcement_import_pulls_no_redaction_or_ml():
    """The full capture-time enforcement package must stay free of the
    post-hoc redaction pipeline and its heavy ML/NLP deps (R3 / SCR-28).

    Importing every ``screencap.enforcement`` capture module in a fresh
    subprocess must not transitively pull in ``screencap.redaction`` or the
    ``transformers``/``torch``/``gliner``/``presidio_analyzer`` stack. The AST
    boundary guard catches static cross-package edges; this asserts the
    invariant at runtime, so a deferred or dynamic import cannot smuggle the
    engine onto the recording path undetected.
    """
    code = (
        "import sys\n"
        "import screencap.enforcement.recorder_enforcement\n"
        "import screencap.enforcement.window_filter\n"
        "import screencap.enforcement.scrub_worker\n"
        "import screencap.enforcement.persistence\n"
        "import screencap.enforcement.disable_log\n"
        "forbidden = [\n"
        "    m for m in sys.modules\n"
        "    if m == 'screencap.redaction' or m.startswith('screencap.redaction.')\n"
        "    or m in ('torch', 'transformers', 'gliner', 'presidio_analyzer')\n"
        "    or m.startswith(('torch.', 'transformers.', 'gliner.', 'presidio_analyzer.'))\n"
        "]\n"
        "assert not forbidden, forbidden\n"
        "print('enforcement path clean')\n"
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
    assert "enforcement path clean" in result.stdout
