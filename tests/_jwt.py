"""Shared test helper: mint a minimal unsigned JWT for uid-extraction tests.

The SCR-116 account-ownership tests (auth, supervisor re-mint guard, terminal
stage) all need a token whose payload decodes to a known ``user_id`` so
``auth.id_token_uid`` can extract it. This is the single definition imported by
all three suites.
"""

from __future__ import annotations

import base64
import json


def _jwt(claims: dict) -> str:
    """A minimal unsigned JWT whose payload decodes to *claims* (uid extraction)."""

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"
