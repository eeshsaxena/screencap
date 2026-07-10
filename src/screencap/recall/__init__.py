"""Conversational-recall core (SCR — feat/conversational-recall-chat).

Turns a question (+ optional prior-turn *pointers*) into a stripped,
pointer-carrying evidence bundle (U3) and, later, into a grounded answer (U4).
The evidence bundle is the seam U5 (the ``/v0/chat.answer`` daemon verb) and U6
(the MCP tool) consume.

Deliberately light: this ``__init__`` re-exports nothing so importing a small
symbol never pulls in the heavier retrieval / privacy import surface, and — the
load-bearing architecture constraint — this package NEVER imports
``screencap.daemon.app`` (U5 makes the daemon import *this*, so the reverse edge
would be a circular import). Retrieval reaches the daemon's underlying primitives
(``content_index``, the transcript/timeline readers) directly, behind an
injectable :class:`~screencap.recall.orchestrator.Retriever` seam. Import
submodules (``orchestrator``) directly.
"""
