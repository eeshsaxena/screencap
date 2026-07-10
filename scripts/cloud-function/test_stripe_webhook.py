"""Tests for billing.py ``stripe_webhook`` (U2 / U4).

Signature verification and the Admin SDK are mocked. The key contracts: an
unsigned/invalid request mutates NO claim (400), the claim converges to the
subscription's CURRENT status so a stale/out-of-order clear cannot revoke an
active subscription (KTD-9), revoke events resolve the uid from subscription
metadata (no client_reference_id), and duplicate delivery is idempotent.

Under the two-tier split (KTD-1/KTD-2) the claim is ``{tier, subscribed}`` with
the security invariant ``subscribed = (tier == "cloud")``. The webhook resolves
the tier from the subscription's PRICE on every grant path; a completed checkout
or an unmapped/empty price can never mint ``tier=cloud`` / ``subscribed=true``.
"""

import os
from unittest import mock

import billing
import flask
import pytest
import stripe

# Price ids used across the two-tier tests. Kept distinct + non-empty so the
# resolver never falls back to treating '' as a tier key (U1).
LOCAL_PRICE = "price_local_123"
CLOUD_PRICE = "price_cloud_456"
LEGACY_PRICE = "price_legacy_5dollar"


@pytest.fixture(autouse=True)
def price_env():
    """Point the price->tier map at known ids for the duration of each test."""
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_PRICE_ID_LOCAL": LOCAL_PRICE,
            "STRIPE_PRICE_ID_CLOUD": CLOUD_PRICE,
            "STRIPE_PRICE_ID": LEGACY_PRICE,
        },
        clear=False,
    ):
        yield


@pytest.fixture(autouse=True)
def app_context():
    app = flask.Flask(__name__)
    with app.app_context():
        yield


def _req(payload=b"{}", sig="t=1,v1=abc"):
    headers = {}
    if sig is not None:
        headers["Stripe-Signature"] = sig
    return mock.Mock(headers=headers, get_data=mock.Mock(return_value=payload))


def _invoke(req):
    resp, status = billing.stripe_webhook(req)
    payload = resp.get_json() if hasattr(resp, "get_json") else resp
    return status, payload


def _event(etype, obj):
    return {"type": etype, "data": {"object": obj}}


def _sub(price_id, *, status="active", uid="userA", trial_end=None):
    """A minimal Stripe subscription object carrying one price line-item."""
    obj = {
        "metadata": {"uid": uid} if uid is not None else {},
        "status": status,
        "items": {"data": [{"price": {"id": price_id}}]},
    }
    if trial_end is not None:
        obj["trial_end"] = trial_end
    return obj


# --------------------------------------------------------------------------
# subscription.created / updated — tier resolved from the price
# --------------------------------------------------------------------------


def test_active_cloud_price_sets_tier_cloud_and_subscribed():
    ev = _event("customer.subscription.updated", _sub(CLOUD_PRICE, status="active"))
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"tier": "cloud", "subscribed": True})


def test_active_local_price_never_grants_subscribed():
    ev = _event("customer.subscription.updated", _sub(LOCAL_PRICE, status="active"))
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"tier": "local", "subscribed": False})


def test_trialing_cloud_writes_trial_end():
    ev = _event(
        "customer.subscription.created",
        _sub(CLOUD_PRICE, status="trialing", trial_end=1893456000),
    )
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with(
        "userA", {"tier": "cloud", "subscribed": True, "trial_end": 1893456000}
    )


def test_trialing_local_writes_trial_end_but_not_subscribed():
    ev = _event(
        "customer.subscription.created",
        _sub(LOCAL_PRICE, status="trialing", trial_end=1893456000),
    )
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with(
        "userA", {"tier": "local", "subscribed": False, "trial_end": 1893456000}
    )


def test_legacy_price_grandfathers_to_cloud():
    # Existing $5 subs keep resolving cloud via the legacy STRIPE_PRICE_ID map,
    # instead of hitting the unmapped-price no-grant path (KTD-2, R11).
    ev = _event("customer.subscription.updated", _sub(LEGACY_PRICE, status="active"))
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"tier": "cloud", "subscribed": True})


def test_unmapped_price_no_grant():
    ev = _event("customer.subscription.updated", _sub("price_unknown", status="active"))
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, payload = _invoke(_req())
    assert status == 200
    assert payload.get("received") is True
    setc.assert_not_called()


def test_empty_price_id_never_treated_as_tier_key():
    # An '' price id must not match an unset '' env and grant a tier.
    with mock.patch.dict(os.environ, {"STRIPE_PRICE_ID_LOCAL": ""}, clear=False):
        ev = _event("customer.subscription.updated", _sub("", status="active"))
        with mock.patch(
            "stripe.Webhook.construct_event", return_value=ev
        ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
            status, _ = _invoke(_req())
    assert status == 200
    setc.assert_not_called()


# --------------------------------------------------------------------------
# checkout.session.completed — must resolve tier from the sub's price, not grant
# cloud by default (the critical fail-closed contract).
# --------------------------------------------------------------------------


def test_checkout_completed_local_sub_resolves_local_never_cloud():
    ev = _event(
        "checkout.session.completed",
        {"client_reference_id": "userA", "metadata": {}, "subscription": "sub_1"},
    )
    fetched = _sub(LOCAL_PRICE, status="active", uid="userA")
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"tier": "local", "subscribed": False})


def test_checkout_completed_cloud_sub_resolves_cloud():
    ev = _event(
        "checkout.session.completed",
        {"client_reference_id": "userA", "metadata": {}, "subscription": "sub_1"},
    )
    fetched = _sub(CLOUD_PRICE, status="active", uid="userA")
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"tier": "cloud", "subscribed": True})


def test_checkout_completed_no_resolvable_price_fail_closed():
    # A completed checkout whose subscription cannot be resolved to a known paid
    # price must NEVER mint tier=cloud by default (fail-closed).
    ev = _event(
        "checkout.session.completed",
        {"client_reference_id": "userA", "metadata": {}, "subscription": "sub_1"},
    )
    fetched = _sub("price_unknown", status="active", uid="userA")
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_not_called()


def test_checkout_completed_no_subscription_id_fail_closed():
    ev = _event(
        "checkout.session.completed",
        {"client_reference_id": "userA", "metadata": {}},
    )
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_not_called()


# --------------------------------------------------------------------------
# Revoke-side (deleted / payment_failed) — re-fetch current status
# --------------------------------------------------------------------------


def test_deleted_event_clears_claim_via_subscription_metadata():
    # No client_reference_id on a delete event; uid resolves from subscription
    # metadata, and the re-fetched status confirms it is no longer active.
    ev = _event("customer.subscription.deleted", {"id": "sub_1", "metadata": {"uid": "userA"}})
    fetched = _sub(CLOUD_PRICE, status="canceled", uid="userA")
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"tier": "none", "subscribed": False})


def test_stale_delete_for_active_subscription_does_not_clear():
    # Out-of-order: a delete event arrives, but the subscription is currently
    # active -> converge to the active tier, do NOT revoke a paying user.
    ev = _event("customer.subscription.deleted", {"id": "sub_1", "metadata": {"uid": "userA"}})
    fetched = _sub(CLOUD_PRICE, status="active", uid="userA")
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"tier": "cloud", "subscribed": True})


def test_payment_failed_clears_via_subscription_lookup():
    ev = _event("invoice.payment_failed", {"subscription": "sub_1", "metadata": {}})
    fetched = _sub(CLOUD_PRICE, status="past_due", uid="userA")
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    # past_due is not an active status -> access blocked (R10).
    setc.assert_called_once_with("userA", {"tier": "none", "subscribed": False})


# --------------------------------------------------------------------------
# Signature / payload / no-uid / idempotency (contracts preserved)
# --------------------------------------------------------------------------


def test_invalid_signature_400_no_claim_mutation():
    with mock.patch(
        "stripe.Webhook.construct_event",
        side_effect=stripe.error.SignatureVerificationError("bad", "sig"),
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req(sig="bad"))
    assert status == 400
    setc.assert_not_called()


def test_malformed_payload_400_no_claim_mutation():
    with mock.patch(
        "stripe.Webhook.construct_event", side_effect=ValueError("bad payload")
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 400
    setc.assert_not_called()


def test_no_resolvable_uid_is_noop():
    ev = _event("customer.subscription.updated", _sub(CLOUD_PRICE, status="active", uid=None))
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, payload = _invoke(_req())
    assert status == 200
    assert payload.get("received") is True
    setc.assert_not_called()


def test_duplicate_delivery_is_idempotent():
    ev = _event("customer.subscription.updated", _sub(CLOUD_PRICE, status="active"))
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        _invoke(_req())
        _invoke(_req())
    # Same value set both times — idempotent in effect (no divergent side effect).
    assert setc.call_count == 2
    assert all(
        c.args == ("userA", {"tier": "cloud", "subscribed": True})
        for c in setc.call_args_list
    )
