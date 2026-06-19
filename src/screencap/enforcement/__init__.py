"""Capture-time privacy enforcement (SCR-33 zone).

The live per-window-event filter (``recorder_enforcement``), the window-event
title filter constructors (``window_filter``), and the menubar-disable
sidecars (``persistence``, ``disable_log``, ``scrub_worker``) that run while a
recording is in progress.

This package depends one-way on the shared ``screencap.privacy`` core
(policy / actions / classifier / mask primitives) and must NOT import the
post-hoc ``screencap.redaction`` detection/NLP pipeline — keeping the heavy
``transformers``/``torch`` surface off the capture path. Imports are kept
light (no eager heavy imports at package import time).
"""
