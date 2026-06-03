"""GCS object-key construction — the single security boundary.

``resolve_prefix`` is the ONLY code in the function that turns a request into a
GCS object-key prefix. The demo/users isolation invariant is enforced here and
nowhere else:

* an **unauthenticated** request can only ever resolve under ``demo/``;
* an **authenticated** request can only ever resolve under ``users/{uid}/``.

Any attempt to build a key outside its namespace raises
``PrefixResolutionError`` *before* any ``list_blobs``/``generate_signed_url``
call can run. After assembling the full key the resolver re-asserts the
``startswith`` namespace prefix as belt-and-suspenders over the construction.

This module is deliberately firebase-free and dependency-light so the boundary
logic can be unit-tested in complete isolation (``test_paths.py``) without ADC
or any GCP credential.
"""

from __future__ import annotations

import re

# The owner-segment alphabet is Firebase-uid-shaped. This is a provider-COUPLED
# choice: a future provider whose subject id contains other characters (e.g.
# Auth0's ``google-oauth2|123``) would change the storage-path alphabet and so
# require a data migration, not a drop-in seam swap. "Provider-agnostic" is
# scoped to the verification interface (bearer -> opaque owner id), NOT this
# path encoding.
_OWNER_RE = re.compile(r"^[A-Za-z0-9]{1,128}$")

# Server-side allow-list of data kinds. A client-supplied ``source`` is checked
# against this set before it is ever interpolated into a path — it is never
# trusted raw.
_ALLOWED_SOURCES = frozenset({"recordings", "sessions"})

# The single public namespace. Hard-coded; never derived from client input.
DEMO_NAMESPACE = "demo"

# Recording-name guard (mirrors ``_RECORDING_RE`` in main.py). A name may not
# contain a slash or climb via "..".
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,255}$")


class PrefixResolutionError(ValueError):
    """A request would resolve to a key outside its allowed namespace.

    This is a hard security stop, not an ordinary bad-input 400: it means the
    central demo/users invariant would have been violated. Callers map it to a
    400 for client-facing input (bad name/source) but the important property is
    that no list/sign call runs on a key that escaped its namespace.
    """


def owner_segment(uid: str) -> str:
    """Validate a verified Firebase uid for use as a path segment.

    Defense-in-depth: the uid arrives already verified by ``verify_bearer``, but
    we re-assert it is path-safe (no ``/``, ``.``, ``..``, empty) before it is
    ever interpolated into an object key. See ``_OWNER_RE`` for why this
    alphabet is provider-coupled.
    """
    if not isinstance(uid, str) or not _OWNER_RE.match(uid):
        raise PrefixResolutionError(f"Invalid owner segment: {uid!r}")
    return uid


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or ".." in name or not _NAME_RE.match(name):
        raise PrefixResolutionError(f"Invalid recording name: {name!r}")
    return name


def resolve_prefix(
    authenticated: bool,
    uid: str | None,
    source: str | None,
    name: str | None,
) -> str:
    """Build the GCS object-key prefix for a request — the ONLY key builder.

    Args:
        authenticated: whether the request carried a verified bearer token.
        uid: the verified Firebase uid (required iff ``authenticated``).
        source: data kind; checked against ``_ALLOWED_SOURCES`` for
            authenticated requests. Ignored for the unauthenticated/demo branch
            (the demo gallery is flat — ``demo/{name}/`` — and accepts no
            client-supplied source, by design, to defeat ``source=demo``
            overloading).
        name: recording name; ``None`` means "the namespace root" (a list).

    Returns:
        The prefix, always ending in ``/``.

    Raises:
        PrefixResolutionError: if the request would resolve outside its allowed
            namespace, or carries an invalid source/uid/name.
    """
    if authenticated:
        seg = owner_segment(uid)  # raises on bad/missing uid
        if source not in _ALLOWED_SOURCES:
            raise PrefixResolutionError(f"Invalid source: {source!r}")
        base = f"users/{seg}/{source}/"
        expected_root = f"users/{seg}/"
    else:
        # Unauthenticated requests can ONLY reach the demo namespace, and the
        # demo gallery is flat: source plays no part in its path. A smuggled
        # source/uid therefore cannot widen reach beyond demo/.
        base = f"{DEMO_NAMESPACE}/"
        expected_root = f"{DEMO_NAMESPACE}/"

    key = f"{base}{_validate_name(name)}/" if name is not None else base

    # Final re-assertion: the assembled key MUST be under the expected root.
    # Belt-and-suspenders over the construction above — catches any future
    # refactor that lets a bad segment slip through.
    if not key.startswith(expected_root):
        raise PrefixResolutionError(
            f"Resolved key {key!r} escaped namespace {expected_root!r}"
        )
    return key
