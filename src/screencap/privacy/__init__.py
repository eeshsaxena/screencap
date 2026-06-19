"""Privacy package — shared privacy-model vocabulary (leaf of the SCR-33 DAG).

Holds the shared privacy-model core that both ``screencap.enforcement``
(capture-time) and ``screencap.redaction`` (post-hoc) depend on: the action
vocabulary (``actions``), the policy matrix (``policy``), the context
classifier and bundle/domain maps (``classify``), audit records (``reasons``),
the domain index loader and its bundled blocklists (``domain_loader`` + the
``data/`` dir), and the pixel/region masking primitives shared by both halves
(``mask_primitives``).

This package imports neither ``screencap.enforcement`` nor
``screencap.redaction`` — it is a leaf. The detection / anonymization engine
that once lived in this ``__init__`` now lives in ``screencap.redaction``.

Import this package's submodules directly (e.g.
``from screencap.privacy.actions import PrivacyAction``); the ``__init__``
intentionally re-exports nothing so that importing a small shared symbol never
pulls in heavier modules.
"""
