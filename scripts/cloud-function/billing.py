"""Cloud Functions for the $5/mo Personal-cloud billing paywall.

Two HTTP entry points deployed as their OWN Cloud Functions, alongside — but
separate from — the signing function (main.py), so the signer's strict "no
tokenless request reaches ``users/``" contract is untouched (billing plan
KTD-3):

- ``create_checkout_session`` — Firebase-token-gated. Derives the uid from the
  verified bearer, creates a $5/mo hosted Stripe Checkout Session bound to that
  uid via ``client_reference_id`` AND the subscription metadata, and returns its
  URL. The client uid is NEVER taken from the request body (KTD-4).
- ``stripe_webhook`` — Stripe-signature-verified (NOT Firebase-gated). Converges
  the ``subscribed`` custom claim to the subscription's CURRENT status, so
  out-of-order / duplicate delivery cannot revoke a currently-active
  subscription (KTD-9).
- ``reconcile_entitlement`` — Firebase-token-gated, GRANT-ONLY self-heal: if the
  caller has a live Stripe subscription but no claim (dropped webhook), grant it
  so a paying customer is never permanently stuck (U14 / AE7). It deploys like
  ``create-checkout-session`` (Firebase-gated, needs ``STRIPE_SECRET_KEY``).

Firebase Admin is initialized HERE (KTD-8): a separately-deployed entry point
never imports main.py, so it cannot rely on main.py's module-scope init side
effect — that gap would otherwise crash ``set_custom_user_claims`` /
``verify_id_token`` at runtime while unit tests (which mock init) stay green.

Deploy (project: proteus-photos, region: southamerica-east1):
    # CRITICAL: these entry points live in billing.py, but the GCF Python buildpack
    # DEFAULTS the source file to main.py — so every billing deploy MUST pass
    # --set-build-env-vars GOOGLE_FUNCTION_SOURCE=billing.py, or the container fails
    # at startup with MissingTargetException (it looks for the fn inside main.py).
    # These functions also run as a dedicated SA with roles/firebaseauth.admin so
    # set_custom_user_claims works:
    #   gcloud iam service-accounts create screencap-billing --project proteus-photos
    #   gcloud projects add-iam-policy-binding proteus-photos \
    #     --member serviceAccount:screencap-billing@proteus-photos.iam.gserviceaccount.com \
    #     --role roles/firebaseauth.admin

    # create-checkout-session — Firebase-token-gated, needs the Stripe secret key
    gcloud functions deploy create-checkout-session \
        --project proteus-photos --gen2 --runtime python312 \
        --trigger-http --allow-unauthenticated \
        --region southamerica-east1 --source scripts/cloud-function/ \
        --entry-point create_checkout_session \
        --set-build-env-vars GOOGLE_FUNCTION_SOURCE=billing.py \
        --service-account screencap-billing@proteus-photos.iam.gserviceaccount.com \
        --set-env-vars SCREENCAP_PROJECT_ID=proteus-photos,STRIPE_SECRET_KEY=sk_...,STRIPE_PRICE_ID_LOCAL=price_...,STRIPE_PRICE_ID_CLOUD=price_...,TRIAL_PERIOD_DAYS=7

    # stripe-webhook — Stripe-signature-verified (tokenless by design). Also
    # needs the price->tier map: STRIPE_PRICE_ID_LOCAL, STRIPE_PRICE_ID_CLOUD,
    # and the legacy STRIPE_PRICE_ID (grandfathered to tier=cloud, KTD-2/R11).
    gcloud functions deploy stripe-webhook \
        --project proteus-photos --gen2 --runtime python312 \
        --trigger-http --allow-unauthenticated \
        --region southamerica-east1 --source scripts/cloud-function/ \
        --entry-point stripe_webhook \
        --set-build-env-vars GOOGLE_FUNCTION_SOURCE=billing.py \
        --service-account screencap-billing@proteus-photos.iam.gserviceaccount.com \
        --set-env-vars SCREENCAP_PROJECT_ID=proteus-photos,STRIPE_SECRET_KEY=sk_...,STRIPE_WEBHOOK_SECRET=whsec_...,STRIPE_PRICE_ID_LOCAL=price_...,STRIPE_PRICE_ID_CLOUD=price_...,STRIPE_PRICE_ID=price_...

    # reconcile-entitlement — Firebase-token-gated, grant-only dropped-webhook self-heal
    gcloud functions deploy reconcile-entitlement \
        --project proteus-photos --gen2 --runtime python312 \
        --trigger-http --allow-unauthenticated \
        --region southamerica-east1 --source scripts/cloud-function/ \
        --entry-point reconcile_entitlement \
        --set-build-env-vars GOOGLE_FUNCTION_SOURCE=billing.py \
        --service-account screencap-billing@proteus-photos.iam.gserviceaccount.com \
        --set-env-vars SCREENCAP_PROJECT_ID=proteus-photos,STRIPE_SECRET_KEY=sk_...

    # NEVER commit sk_live_... / whsec_...; keep test-mode and live-mode keys per
    # environment and rotate via the console + redeploy (billing plan U11).
"""

from __future__ import annotations

import logging
import os

import firebase_admin
import functions_framework
import stripe
from auth import AuthInvalid, AuthUnavailable, verify_bearer
from firebase_admin import auth as fb_auth
from flask import jsonify

logger = logging.getLogger(__name__)


def _resolve_project_id() -> str:
    """The Firebase project this function trusts (mirrors main.py's pin)."""
    return os.environ.get("SCREENCAP_PROJECT_ID", "proteus-photos")


PROJECT_ID = _resolve_project_id()

# Subscription statuses that count as an active entitlement. ``past_due`` is
# deliberately excluded: a failed renewal payment blocks new uploads (R10).
_ACTIVE_STATUSES = {"active", "trialing"}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _ensure_firebase_app() -> None:
    """Initialize the default Firebase app if absent (KTD-8).

    A separately-deployed billing entry point never imports main.py, so it must
    initialize the Admin app itself — otherwise ``set_custom_user_claims`` /
    ``verify_id_token`` raise "default Firebase app does not exist" on the first
    live invocation, while conftest's mocked init hides the gap in unit tests.
    Guarded on ``_apps`` so a warm process (or the signer sharing the module) is
    not re-initialized.
    """
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": PROJECT_ID})
        logger.info("firebase_admin initialized for project %s (billing)", PROJECT_ID)


_ensure_firebase_app()


def _cors(response, status=200):
    """Attach CORS headers to a response (mirrors main.py)."""
    if isinstance(response, tuple):
        body, code = response[0], response[1]
        return (body, code, CORS_HEADERS)
    return (response, status, CORS_HEADERS)


def _stripe_key() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "")


def _webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "")


# --------------------------------------------------------------------------
# Two-tier entitlement (KTD-1/KTD-2)
# --------------------------------------------------------------------------
#
# The custom claim is ``{tier, subscribed}`` where the security invariant is
# ``subscribed = (tier == "cloud")`` — the signer (unchanged) still gates cloud
# on ``subscribed``. ``tier`` is an open string; the gates test set membership,
# so a future ``free_capped`` slots in as a new value. ``TIERS_BY_RANK`` orders
# tiers low→high so "highest tier wins" (reconcile, multi-sub) is a max().

TIERS_BY_RANK = ("local", "cloud")


def _price_tier_map() -> dict:
    """Build the ``price_id -> tier`` map from env, skipping empty ids.

    An empty/missing price env is NEVER inserted, so a subscription carrying an
    empty ('') price id can never match an unset env and be granted a tier (U1).
    The legacy single ``STRIPE_PRICE_ID`` is mapped to ``cloud`` so existing $5
    subscribers keep resolving instead of hitting the no-grant path (KTD-2, R11).
    """
    mapping: dict = {}
    for env_name, tier in (
        ("STRIPE_PRICE_ID_LOCAL", "local"),
        ("STRIPE_PRICE_ID_CLOUD", "cloud"),
        ("STRIPE_PRICE_ID", "cloud"),  # legacy grandfather
    ):
        price_id = os.environ.get(env_name, "")
        if price_id:  # never treat '' as a tier key
            mapping.setdefault(price_id, tier)
    return mapping


def _price_for_tier(tier: str) -> str:
    """Return the configured price id for a checkout tier, or '' if unset."""
    env_name = {"local": "STRIPE_PRICE_ID_LOCAL", "cloud": "STRIPE_PRICE_ID_CLOUD"}.get(tier)
    return os.environ.get(env_name, "") if env_name else ""


def _tier_from_price(price_id) -> str | None:
    """Map a subscription's price id to a known paid tier, or None (fail-closed)."""
    if not price_id:
        return None
    return _price_tier_map().get(price_id)


def _subscription_price_id(sub) -> str | None:
    """Extract the first line-item's price id from a Stripe subscription object."""
    items = (sub.get("items") or {}).get("data") or []
    if not items:
        return None
    return ((items[0] or {}).get("price") or {}).get("id")


def _tier_from_subscription(sub) -> str | None:
    """Resolve the tier from a subscription's price (None if unmapped/empty)."""
    return _tier_from_price(_subscription_price_id(sub))


def _trial_period_days() -> int | None:
    """Trial length for a card-required checkout, or None if unset (KTD-3)."""
    raw = os.environ.get("TRIAL_PERIOD_DAYS", "")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


# --------------------------------------------------------------------------
# U3 — create-checkout-session (Firebase-token-gated)
# --------------------------------------------------------------------------


@functions_framework.http
def create_checkout_session(request):
    """Create a hosted Checkout Session for a tier, bound to the caller's uid.

    The uid is derived server-side from the verified Firebase bearer and is
    NEVER read from the request body (KTD-4). It is stamped onto the session
    (``client_reference_id`` + ``metadata``) and the subscription metadata so
    every downstream webhook event can resolve it (KTD-9).

    The request body's validated ``tier`` (``local`` | ``cloud``) selects the
    price ONLY — an unknown tier is rejected 400 with no Stripe call, and any
    client-supplied uid/price is ignored. The webhook (U2) stays the sole
    entitlement authority, re-deriving the tier from the paid price, so a
    spoofed tier cannot over-grant. The trial is card-required
    (``payment_method_collection='always'`` + ``trial_period_days``, KTD-3).
    """
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    _ensure_firebase_app()

    try:
        uid = verify_bearer(request, PROJECT_ID)
    except AuthUnavailable:
        return _cors(
            (jsonify({"error": "Auth verification temporarily unavailable"}), 503)
        )
    except AuthInvalid:
        return _cors((jsonify({"error": "Authentication required"}), 401))

    body = request.get_json(silent=True) or {}
    tier = body.get("tier")
    price_id = _price_for_tier(tier) if tier in TIERS_BY_RANK else ""
    if not price_id:
        # Unknown tier, or its price env is unset — reject before any Stripe call
        # (never fall back to a default price and mis-charge).
        return _cors((jsonify({"error": "Invalid or unavailable tier"}), 400))

    success_url = os.environ.get(
        "STRIPE_CHECKOUT_SUCCESS_URL", "https://screencap.sh/checkout/success"
    )
    cancel_url = os.environ.get(
        "STRIPE_CHECKOUT_CANCEL_URL", "https://screencap.sh/checkout/cancel"
    )

    # Stamp uid onto the subscription so revoke-side events (deleted /
    # payment_failed), which carry no client_reference_id, can still resolve it.
    subscription_data = {"metadata": {"uid": uid}}
    trial_days = _trial_period_days()
    if trial_days is not None:
        subscription_data["trial_period_days"] = trial_days

    stripe.api_key = _stripe_key()
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=uid,
            metadata={"uid": uid},
            subscription_data=subscription_data,
            # Card-required trial: collect a payment method upfront so the trial
            # auto-converts (R5). trial_period_days alone can yield a card-
            # OPTIONAL trial, so this must accompany it.
            payment_method_collection="always",
            allow_promotion_codes=True,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except Exception as exc:  # Stripe/network error — do not leak internals.
        logger.warning("checkout session create failed: %s", exc)
        return _cors((jsonify({"error": "Checkout temporarily unavailable"}), 502))

    return _cors(jsonify({"url": session.url, "id": session.id}))


# --------------------------------------------------------------------------
# U4 — stripe-webhook (Stripe-signature-verified, tokenless)
# --------------------------------------------------------------------------


def _current_subscription(subscription_id):
    """Return the re-fetched Stripe subscription object, or ``None``.

    Used to resolve the CURRENT status + price from an event's subscription id
    (grant-side checkout completed, and clear-side deleted/payment_failed), so a
    stale/out-of-order event cannot revoke a now-active subscription (KTD-9).
    Returns ``None`` if the id is missing or the lookup fails.
    """
    if not subscription_id:
        return None
    try:
        return stripe.Subscription.retrieve(subscription_id)
    except Exception as exc:  # transient lookup failure — caller no-ops safely.
        logger.warning("subscription retrieve failed for %s: %s", subscription_id, exc)
        return None


def _grant_from_subscription(sub):
    """Resolve ``(tier, trial_end)`` from a live subscription object (KTD-1/2).

    Returns ``(None, None)`` unless the subscription is currently in an active
    status AND its price maps to a known paid tier (fail-closed). When
    ``trialing`` the subscription's ``trial_end`` is surfaced for the "days
    left" UI (U6/U11).
    """
    if not sub or sub.get("status") not in _ACTIVE_STATUSES:
        return None, None
    tier = _tier_from_subscription(sub)
    if tier is None:
        return None, None
    trial_end = sub.get("trial_end") if sub.get("status") == "trialing" else None
    return tier, trial_end


def _resolve_entitlement(event_type, obj):
    """Map a Stripe event to ``(uid, tier, trial_end)``.

    ``tier`` is ``None`` when the event resolves no known paid tier — the
    fail-closed path (clear the claim / no grant). The tier is ALWAYS resolved
    from the subscription's PRICE, never assumed: a ``checkout.session.completed``
    carries no price, so it re-fetches the subscription and resolves from there —
    a completed checkout can never mint ``tier=cloud`` by default. Returns
    ``(None, None, None)`` when no uid is resolvable (logged no-op).
    """
    if event_type == "checkout.session.completed":
        uid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("uid")
        if uid is None:
            return None, None, None
        sub = _current_subscription(obj.get("subscription"))
        tier, trial_end = _grant_from_subscription(sub)
        return uid, tier, trial_end

    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        uid = (obj.get("metadata") or {}).get("uid")
        if uid is None:
            return None, None, None
        tier, trial_end = _grant_from_subscription(obj)
        return uid, tier, trial_end

    if event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
        sub_id = (
            obj.get("id")
            if event_type == "customer.subscription.deleted"
            else obj.get("subscription")
        )
        event_uid = (obj.get("metadata") or {}).get("uid")
        sub = _current_subscription(sub_id)
        fetched_uid = (sub.get("metadata") or {}).get("uid") if sub else None
        uid = event_uid or fetched_uid
        if uid is None:
            return None, None, None
        # Re-fetched status/price: a stale delete for a now-active sub keeps it.
        tier, trial_end = _grant_from_subscription(sub)
        return uid, tier, trial_end

    return None, None, None


def _apply_entitlement(uid, tier, trial_end=None) -> None:
    """Set the two-tier custom claim (KTD-1).

    Writes ``{tier, subscribed = (tier == "cloud")}`` — ``subscribed`` is always
    a pure function of ``tier``, never set independently, so the two fields can
    never drift into ``subscribed=true`` with ``tier != cloud``. A ``None`` tier
    is the fail-closed no-grant/clear state (``tier="none"``, ``subscribed=false``).
    ``trial_end`` is written only when present (during a trial). The webhook is
    the sole authority for these claims and owns the whole ``tier``-derived set,
    so it writes them directly; setting the same value twice is a no-op in
    effect, so duplicate delivery is idempotent.
    """
    resolved = tier or "none"
    claims = {"tier": resolved, "subscribed": resolved == "cloud"}
    if trial_end is not None:
        claims["trial_end"] = trial_end
    fb_auth.set_custom_user_claims(uid, claims)


@functions_framework.http
def stripe_webhook(request):
    """Verify the Stripe signature, then converge the ``subscribed`` claim.

    Signature verification runs FIRST: an unsigned or invalid request returns
    400 and mutates no claim (contract: no unsigned request reaches
    ``set_custom_user_claims``). An event with no resolvable uid is a logged
    no-op, never a crash.
    """
    _ensure_firebase_app()

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, _webhook_secret())
    except ValueError as exc:  # malformed payload
        logger.warning("stripe webhook payload invalid: %s", exc)
        return (jsonify({"error": "invalid payload"}), 400)
    except stripe.error.SignatureVerificationError as exc:
        logger.warning("stripe webhook signature verification failed: %s", exc)
        return (jsonify({"error": "invalid signature"}), 400)

    stripe.api_key = _stripe_key()
    event_type = event["type"]
    obj = event["data"]["object"]

    uid, tier, trial_end = _resolve_entitlement(event_type, obj)
    if uid is None:
        logger.info("stripe webhook %s: no resolvable uid; no-op", event_type)
        return (jsonify({"received": True}), 200)

    # A grant-side event that resolves no known paid tier is usually a no-op,
    # not a write of tier=none — this keeps an unmapped/empty-price created/
    # updated event (or a checkout whose sub can't be re-fetched) from clobbering
    # a good claim, while a stale delete for a now-active sub still re-grants via
    # _grant_from_subscription. The ONE exception is a `customer.subscription.
    # updated` whose subscription is in an INACTIVE status (canceled / unpaid /
    # paused / incomplete_expired): that is a genuine revoke delivered as
    # `updated` rather than a separate `deleted`, so it must fall through and
    # clear. `created` and `checkout.session.completed` stay no-op (a new/
    # incomplete sub is not a revoke signal and must not clear an existing claim).
    if tier is None and event_type in (
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        is_revoking_update = (
            event_type == "customer.subscription.updated"
            and obj.get("status") is not None
            and obj.get("status") not in _ACTIVE_STATUSES
        )
        if not is_revoking_update:
            logger.info(
                "stripe webhook %s: no resolvable paid tier for uid; no grant",
                event_type,
            )
            return (jsonify({"received": True}), 200)
        logger.info(
            "stripe webhook customer.subscription.updated: inactive status %s; "
            "clearing claim",
            obj.get("status"),
        )

    _apply_entitlement(uid, tier, trial_end)
    return (jsonify({"received": True}), 200)


# --------------------------------------------------------------------------
# U14 — reconcile-entitlement (Firebase-token-gated dropped-webhook self-heal)
# --------------------------------------------------------------------------


def _active_subscription_tier(uid) -> str | None:
    """Highest paid tier among the account's currently-active subscriptions.

    Searches by the uid stamped into subscription metadata (KTD-9) and resolves
    each active sub's tier from its PRICE — NOT a tier-blind "any active" bool,
    which would let a Local-Pro user self-heal into cloud (KTD-1). If multiple
    active subs exist, the highest tier wins (``local`` < ``cloud``). Any lookup
    failure returns ``None`` — never hand out access on an error.
    """
    try:
        result = stripe.Subscription.search(query=f"metadata['uid']:'{uid}'")
    except Exception as exc:  # transient / search error — do not grant on failure.
        logger.warning("subscription search failed for uid %s: %s", uid, exc)
        return None
    best: str | None = None
    for sub in result.get("data", []):
        if sub.get("status") not in _ACTIVE_STATUSES:
            continue
        tier = _tier_from_subscription(sub)
        if tier is None:
            continue
        if best is None or TIERS_BY_RANK.index(tier) > TIERS_BY_RANK.index(best):
            best = tier
    return best


@functions_framework.http
def reconcile_entitlement(request):
    """Self-heal a dropped webhook: grant ``subscribed`` if the caller has a live
    Stripe subscription but no claim yet (U14 / AE7).

    Firebase-token-gated (uid server-derived). GRANT-ONLY: it repairs a missing
    grant when Stripe confirms an active subscription; it never revokes here
    (revocation is the webhook's job), so it can never lock out a paying user. A
    client calls this when a post-checkout ``whoami`` still shows
    ``subscribed: false``, so a paying customer is never permanently stuck.
    """
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    _ensure_firebase_app()

    try:
        uid = verify_bearer(request, PROJECT_ID)
    except AuthUnavailable:
        return _cors(
            (jsonify({"error": "Auth verification temporarily unavailable"}), 503)
        )
    except AuthInvalid:
        return _cors((jsonify({"error": "Authentication required"}), 401))

    stripe.api_key = _stripe_key()
    tier = _active_subscription_tier(uid)
    if tier is not None:
        _apply_entitlement(uid, tier)
    return _cors(jsonify({"tier": tier, "subscribed": tier == "cloud"}))
