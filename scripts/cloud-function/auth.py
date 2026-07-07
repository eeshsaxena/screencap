"""Token verification for the signing Cloud Function — bearer -> uid.

``verify_bearer`` is the trust boundary's front door: it extracts and verifies a
Firebase ID token and returns the immutable, opaque, path-safe ``uid``. It
raises two DISTINCT typed errors so callers can map them to different HTTP
statuses:

* :class:`AuthInvalid`     -> **401**: the token is missing, malformed, expired,
  wrong-signature, or minted for a different Firebase project. The caller is
  genuinely not authenticated.
* :class:`AuthUnavailable` -> **503-class**: verification could not be completed
  (Google public-cert fetch failed, Firebase outage). This is NOT "you are
  unauthenticated" — the client must treat it as fail-closed (retryable / no
  destructive action), never as a hard deny.

The project id is pinned at init (see main.py) and re-asserted here (``aud`` /
``iss``) so the function can never verify-but-misattribute a token from a foreign
Firebase project. ``check_revoked=False`` keeps this on the hot path (pure JWT +
cached-cert checks, no Admin-API round-trip); revocation / account-disable
checks are a deferred abuse-control concern.

This module imports ``firebase_admin`` but does NOT call ``initialize_app`` —
that happens once at module scope in main.py. Importing this module therefore
requires no ADC, so ``test_auth.py`` runs offline with ``verify_id_token``
mocked.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from firebase_admin import auth as fb_auth


class _HasHeaders(Protocol):
    """The minimal request contract verify_bearer needs: a ``headers`` mapping
    with ``.get``. Documents the duck-typed boundary (a Flask request satisfies
    it) without importing flask into this module."""

    headers: Mapping[str, str]


# Small skew tolerance so a token minted moments ago does not 401 on a cold
# start with a slightly-behind clock ("token used too early"). Kept well under
# the SDK-permitted max of 60s.
_CLOCK_SKEW_SECONDS = 10


class AuthError(Exception):
    """Base class for bearer-token verification failures."""


class AuthInvalid(AuthError):
    """The bearer token is missing/malformed/expired/wrong-project. -> 401."""


class AuthUnavailable(AuthError):
    """Verification could not be completed (cert fetch / Firebase outage).

    -> 503. Clients treat this as fail-closed, not as a hard deny: an outage
    must never be mistaken for "this user is unauthenticated".
    """


def _extract_bearer(request: _HasHeaders) -> str:
    header = request.headers.get("Authorization", "") if request is not None else ""
    if not header:
        raise AuthInvalid("Missing Authorization header")
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthInvalid("Malformed Authorization header (expected 'Bearer <token>')")
    return parts[1].strip()


def verify_bearer_full(
    request: _HasHeaders, project_id: str | None = None
) -> "tuple[str, dict]":
    """Verify the request's bearer token; return ``(uid, decoded_token)``.

    The decoded token carries any Firebase **custom claims** (e.g.
    ``subscribed``) as top-level keys, so an entitlement gate can read them
    without a second Admin-API round-trip. This is the full verification core;
    :func:`verify_bearer` is a thin uid-only wrapper over it, so existing
    callers and the tokenless-boundary contract test stay unchanged.

    Args:
        request: a Flask request (anything with ``.headers.get``).
        project_id: the pinned Firebase project id. When set, the decoded
            token's ``aud``/``iss`` are asserted to match it — belt-and-
            suspenders over the SDK's own check, blocking verify-but-
            misattribute against a foreign project.

    Returns:
        ``(uid, decoded_token)`` — the verified, immutable, path-safe Firebase
        ``uid`` and the full decoded token (including custom claims).

    Raises:
        AuthInvalid: missing/malformed/expired/bad-signature/foreign-project
            token (caller -> 401).
        AuthUnavailable: verification itself could not run, e.g. cert-fetch
            failure or Firebase outage (caller -> 503; client fails closed).
    """
    token = _extract_bearer(request)
    try:
        decoded = fb_auth.verify_id_token(
            token,
            check_revoked=False,
            clock_skew_seconds=_CLOCK_SKEW_SECONDS,
        )
    except fb_auth.CertificateFetchError as exc:
        # Verification could not complete — distinct from "token is invalid".
        raise AuthUnavailable(f"Token verification unavailable: {exc}") from exc
    except (fb_auth.InvalidIdTokenError, fb_auth.UserDisabledError, ValueError) as exc:
        # ExpiredIdTokenError / RevokedIdTokenError subclass InvalidIdTokenError;
        # UserDisabledError does NOT (it subclasses InvalidArgumentError), so it
        # is listed explicitly — it can't fire while check_revoked=False, but
        # adding it now closes the gap before the deferred abuse-control change
        # enables revocation (otherwise it would surface as an unhandled 500).
        # ValueError covers a non-JWT / non-string token argument.
        raise AuthInvalid(f"Invalid token: {exc}") from exc

    uid = decoded.get("uid") or decoded.get("user_id") or decoded.get("sub")
    if not uid:
        raise AuthInvalid("Verified token carried no uid")

    if project_id:
        aud = decoded.get("aud")
        iss = decoded.get("iss")
        if aud != project_id or iss != f"https://securetoken.google.com/{project_id}":
            raise AuthInvalid("Token minted for a different Firebase project")

    return uid, decoded


def verify_bearer(request: _HasHeaders, project_id: str | None = None) -> str:
    """Verify the request's bearer token and return the Firebase uid.

    Thin wrapper over :func:`verify_bearer_full` that discards the decoded
    token — the historical contract (``-> str``) the demo/list/sign-download
    handlers and the tokenless-boundary test depend on. Raises the same
    ``AuthInvalid`` / ``AuthUnavailable`` errors.
    """
    uid, _ = verify_bearer_full(request, project_id)
    return uid
