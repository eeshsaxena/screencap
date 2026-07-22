"""Share-record store logic for share-by-link (SCR-229, U3).

Pure, GCS-free record + token logic so it can be unit-tested in isolation
(``test_shares.py``), mirroring ``paths.py``. The GCS read/write of the record
object lives in ``main.py``; this module owns token generation/validation, the
``shares/{token}/`` object layout, record (de)serialization, and the
expiry/revocation predicates.

A share is anonymous and token-addressable: the token is the only thing an
unauthenticated ``resolve-share`` holds, so the record lives at a token-keyed
path (``shares/{token}/.share.json``) rather than under ``users/{uid}/`` (which
the anonymous resolver could not find). Owner identity lives INSIDE the record
(``owner_uid``) so ``revoke-share`` can enforce owner-only revocation. The
``shares/`` namespace is disjoint from both ``demo/`` and ``users/``, so it does
not widen ``resolve_prefix``'s demo/users boundary; it is guarded here by
server-generated unguessable tokens plus format validation.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone

SHARE_NAMESPACE = "shares"
RECORD_NAME = ".share.json"
DEFAULT_EXPIRY_DAYS = 30
RECORD_VERSION = 1

_TOKEN_NBYTES = 32
# secrets.token_urlsafe(32) -> 43 url-safe base64 chars (A-Za-z0-9_-). Accept a
# small range around that, and reject any path separator or "..". This guards
# the server-generated value defensively and rejects a malformed client token.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
# Artifact relative names inside the share (mirrors _FILENAME_RE in main.py):
# subdirs allowed, no "..".
_ARTIFACT_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._/-]{0,511}$")


class ShareError(ValueError):
    """Invalid share token / record — a hard stop before any GCS access."""


def new_token() -> str:
    """A fresh unguessable share token (256-bit CSPRNG, URL-safe)."""
    return secrets.token_urlsafe(_TOKEN_NBYTES)


def is_valid_token(token) -> bool:
    return isinstance(token, str) and ".." not in token and bool(_TOKEN_RE.match(token))


def validate_token(token) -> str:
    if not is_valid_token(token):
        raise ShareError(f"Invalid share token: {token!r}")
    return token


def is_valid_artifact(name) -> bool:
    return isinstance(name, str) and ".." not in name and bool(_ARTIFACT_RE.match(name))


def share_prefix(token: str) -> str:
    """The object-key prefix for a share's artifacts: ``shares/{token}/``."""
    return f"{SHARE_NAMESPACE}/{validate_token(token)}/"


def record_key(token: str) -> str:
    """The object key of the share's metadata record."""
    return f"{share_prefix(token)}{RECORD_NAME}"


def artifact_key(token: str, artifact: str) -> str:
    """The object key of one share artifact — validated, never a raw join."""
    if not is_valid_artifact(artifact):
        raise ShareError(f"Invalid share artifact name: {artifact!r}")
    return f"{share_prefix(token)}{artifact}"


def expires_at_iso(now: datetime, days: int = DEFAULT_EXPIRY_DAYS) -> str:
    """ISO-8601 UTC expiry ``days`` from ``now``."""
    return (now + timedelta(days=days)).astimezone(timezone.utc).isoformat()


def build_record(owner_uid, artifacts, expires_at, *, view_only=True) -> dict:
    return {
        "version": RECORD_VERSION,
        "owner_uid": owner_uid,
        "artifacts": list(artifacts),
        "expires_at": expires_at,
        "revoked": False,
        "view_only": bool(view_only),
    }


def dumps(record: dict) -> str:
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def loads(raw) -> dict:
    rec = json.loads(raw)
    if not isinstance(rec, dict) or "owner_uid" not in rec or "artifacts" not in rec:
        raise ShareError("Malformed share record")
    return rec


def is_expired(record: dict, now: datetime) -> bool:
    """True when the share is past its expiry. Fail-closed on an unparseable
    ``expires_at`` (treat as expired) so a corrupt record never serves forever."""
    raw = record.get("expires_at")
    if not raw:
        return False
    try:
        exp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return now >= exp


def is_revoked(record: dict) -> bool:
    return bool(record.get("revoked"))


def is_owner(record: dict, uid) -> bool:
    return isinstance(uid, str) and record.get("owner_uid") == uid
