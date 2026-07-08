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
        --set-env-vars SCREENCAP_PROJECT_ID=proteus-photos,STRIPE_SECRET_KEY=sk_...,STRIPE_PRICE_ID=price_...

    # stripe-webhook — Stripe-signature-verified (tokenless by design)
    gcloud functions deploy stripe-webhook \
        --project proteus-photos --gen2 --runtime python312 \
        --trigger-http --allow-unauthenticated \
        --region southamerica-east1 --source scripts/cloud-function/ \
        --entry-point stripe_webhook \
        --set-build-env-vars GOOGLE_FUNCTION_SOURCE=billing.py \
        --service-account screencap-billing@proteus-photos.iam.gserviceaccount.com \
        --set-env-vars SCREENCAP_PROJECT_ID=proteus-photos,STRIPE_SECRET_KEY=sk_...,STRIPE_WEBHOOK_SECRET=whsec_...

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
# U3 — create-checkout-session (Firebase-token-gated)
# --------------------------------------------------------------------------


@functions_framework.http
def create_checkout_session(request):
    """Create a $5/mo hosted Checkout Session bound to the caller's uid.

    The uid is derived server-side from the verified Firebase bearer and is
    NEVER read from the request body (KTD-4). It is stamped onto the session
    (``client_reference_id`` + ``metadata``) and the subscription metadata so
    every downstream webhook event can resolve it (KTD-9).
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

    price_id = os.environ.get("STRIPE_PRICE_ID", "")
    success_url = os.environ.get(
        "STRIPE_CHECKOUT_SUCCESS_URL", "https://screencap.sh/checkout/success"
    )
    cancel_url = os.environ.get(
        "STRIPE_CHECKOUT_CANCEL_URL", "https://screencap.sh/checkout/cancel"
    )

    stripe.api_key = _stripe_key()
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=uid,
            metadata={"uid": uid},
            # Stamp uid onto the subscription so revoke-side events
            # (subscription.deleted / invoice.payment_failed), which carry no
            # client_reference_id, can still resolve it (KTD-9).
            subscription_data={"metadata": {"uid": uid}},
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
    """Return ``(status, uid)`` for a subscription id, re-fetched from Stripe.

    Used on the clear-side so a stale/out-of-order event cannot revoke a
    now-active subscription (KTD-9). Returns ``(None, None)`` if the id is
    missing or the lookup fails.
    """
    if not subscription_id:
        return None, None
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
    except Exception as exc:  # transient lookup failure — caller no-ops safely.
        logger.warning("subscription retrieve failed for %s: %s", subscription_id, exc)
        return None, None
    return sub.get("status"), (sub.get("metadata") or {}).get("uid")


def _resolve_entitlement(event_type, obj):
    """Map a Stripe event to ``(uid, active)``, or ``(None, None)`` if no uid.

    Grant-side events (checkout completed, subscription active) set the claim
    directly. Clear-side events re-fetch the subscription's CURRENT status, so a
    stale ``deleted`` for a subscription that is now active does not revoke it.
    """
    if event_type == "checkout.session.completed":
        uid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("uid")
        return uid, True

    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        uid = (obj.get("metadata") or {}).get("uid")
        return uid, obj.get("status") in _ACTIVE_STATUSES

    if event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
        sub_id = (
            obj.get("id")
            if event_type == "customer.subscription.deleted"
            else obj.get("subscription")
        )
        event_uid = (obj.get("metadata") or {}).get("uid")
        status, fetched_uid = _current_subscription(sub_id)
        uid = event_uid or fetched_uid
        active = status in _ACTIVE_STATUSES if status is not None else False
        return uid, active

    return None, None


def _apply_entitlement(uid, active) -> None:
    """Set the ``subscribed`` custom claim (idempotent by value).

    ``subscribed`` is the only custom claim in this system; if others are added
    later this must merge rather than overwrite. Setting the same value twice is
    a no-op in effect, so duplicate webhook delivery is idempotent.
    """
    fb_auth.set_custom_user_claims(uid, {"subscribed": bool(active)})


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

    uid, active = _resolve_entitlement(event_type, obj)
    if uid is None:
        logger.info("stripe webhook %s: no resolvable uid; no-op", event_type)
        return (jsonify({"received": True}), 200)

    _apply_entitlement(uid, active)
    return (jsonify({"received": True}), 200)


# --------------------------------------------------------------------------
# U14 — reconcile-entitlement (Firebase-token-gated dropped-webhook self-heal)
# --------------------------------------------------------------------------


def _has_active_subscription(uid) -> bool:
    """True if the account has a currently-active Stripe subscription.

    Searches by the uid stamped into subscription metadata (KTD-9). Any lookup
    failure returns False — never hand out access on an error.
    """
    try:
        result = stripe.Subscription.search(query=f"metadata['uid']:'{uid}'")
    except Exception as exc:  # transient / search error — do not grant on failure.
        logger.warning("subscription search failed for uid %s: %s", uid, exc)
        return False
    for sub in result.get("data", []):
        if sub.get("status") in _ACTIVE_STATUSES:
            return True
    return False


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
    active = _has_active_subscription(uid)
    if active:
        _apply_entitlement(uid, True)
    return _cors(jsonify({"subscribed": bool(active)}))
