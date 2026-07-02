"""Cloud-side entitlement store — the source of truth for a uid's cloud plan (U1).

v1 backs entitlement with a Firebase custom claim (``plan``) rather than a
Firestore record: it ships on infra that already exists (``firebase_admin`` is
initialized once in main.py) and needs no new runtime dependency. The claim is
read LIVE via ``get_user(uid).custom_claims`` at the authoritative upload gate
(U2) — never off the already-verified ID token, whose claims ``verify_bearer``
discards — so a just-granted (or, once billing lands, just-revoked) plan is
always fresh at the gate (KTD2).

The read/write seam is deliberately narrow (``read_entitlement`` /
``grant_founding`` / ``plan_from_claims``) so it can swap to a Firestore record
when billing adds revocable/lapsing state, at which point the claim demotes to a
UX cache.

Grant authorization: ``grant_founding`` is SELF-SERVICE and SELF-AUTHORIZED in
v1 — founding is free and confers no billable capability. When billing activates
this MUST move behind the payment-processor webhook / server-side eligibility
check and must NOT remain client-callable (see the plan's Definition of Done).

IAM: both ``set_custom_user_claims`` and the per-upload ``get_user`` lookup
require the runtime service account to hold the Firebase Admin
(``serviceAccountTokenCreator`` / token-minting) permission — a deploy-time
prerequisite (see main.py header).
"""

from __future__ import annotations

import firebase_admin.exceptions
from firebase_admin import auth as fb_auth

# Plan tiers v1 understands. "free" is the ABSENCE of a claim (the default);
# "founding" is the v1 grant. "paid"/"lapsed" are reserved for the billing
# milestone and are intentionally not grantable here.
PLAN_FREE = "free"
PLAN_FOUNDING = "founding"

# Plans that authorize cloud upload. A frozenset so a mistyped membership test
# fails loud rather than silently authorizing an unknown tier.
ENTITLED_PLANS = frozenset({PLAN_FOUNDING})


class EntitlementUnavailable(Exception):
    """The entitlement lookup/write could not complete (Firebase outage / API error).

    Distinct from "not entitled": the U2 gate maps this to 503 so a transient
    failure is never mistaken for a hard deny (fail-closed, retryable), mirroring
    the AuthUnavailable -> 503 discrimination in auth.py.
    """


def plan_from_claims(custom_claims: "dict | None") -> str:
    """Extract the plan tier from a uid's custom claims, defaulting to ``free``.

    Pure and side-effect-free so the free-default rule lives in exactly one place,
    shared by the live lookup here and any caller that already holds the claims.
    """
    if not custom_claims:
        return PLAN_FREE
    plan = custom_claims.get("plan")
    return plan if plan else PLAN_FREE


def entitlement_from_plan(plan: str) -> dict:
    """Shape a plan tier into the entitlement contract the gate/read surface share.

    ``expires`` is always ``None`` in v1 (forward-compat; populated when billing
    adds lapse). ``active`` is ``True`` iff the plan authorizes upload.
    """
    return {"plan": plan, "active": plan in ENTITLED_PLANS, "expires": None}


def read_entitlement(uid: str) -> dict:
    """Live-read the uid's entitlement via ``get_user(uid).custom_claims``.

    Deliberately NOT read off the verified ID token (whose claims are discarded
    by ``verify_bearer``): a custom claim only appears in a freshly minted token,
    so reading the token would authorize on a stale claim. The Admin ``get_user``
    call always reflects the current claim (KTD2).

    A uid with no Firebase user record (should not happen for a verified token)
    reads as ``free`` — fail-closed, not error. A transient lookup failure raises
    :class:`EntitlementUnavailable` so the gate can 503 rather than deny.
    """
    try:
        user = fb_auth.get_user(uid)
    except fb_auth.UserNotFoundError:
        return entitlement_from_plan(PLAN_FREE)
    except firebase_admin.exceptions.FirebaseError as exc:
        raise EntitlementUnavailable(str(exc)) from exc
    return entitlement_from_plan(plan_from_claims(user.custom_claims))


def grant_founding(uid: str) -> dict:
    """Grant the founding plan to ``uid`` by setting the ``plan`` custom claim.

    Idempotent: it reads the uid's existing custom claims and re-sets ``plan``,
    so a repeat grant is a no-op re-set and any unrelated claims are preserved
    (``set_custom_user_claims`` is a full replace). Returns the resulting
    entitlement.

    Self-service / self-authorized in v1 (see the module docstring). Raises
    :class:`EntitlementUnavailable` on a transient Firebase failure.
    """
    try:
        user = fb_auth.get_user(uid)
        claims = dict(user.custom_claims or {})
        claims["plan"] = PLAN_FOUNDING
        fb_auth.set_custom_user_claims(uid, claims)
    except firebase_admin.exceptions.FirebaseError as exc:
        raise EntitlementUnavailable(str(exc)) from exc
    return entitlement_from_plan(PLAN_FOUNDING)
