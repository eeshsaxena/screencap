"""Backfill subsystem (SCR-178) — one-shot OCR indexing of existing recordings.

Deliberately light: this ``__init__`` re-exports nothing so importing a small
symbol (e.g. a reason constant) never pulls in the heavier scrub/OCR import
surface. Mirrors the ``screencap.privacy`` leaf convention. Import submodules
(``skip_intervals``, ``ledger``, ``engine``) directly.
"""
