"""Tests for billing.py ``stripe_webhook`` (U4).

Signature verification and the Admin SDK are mocked. The key contracts: an
unsigned/invalid request mutates NO claim (400), the claim converges to the
subscription's CURRENT status so a stale/out-of-order clear cannot revoke an
active subscription (KTD-9), revoke events resolve the uid from subscription
metadata (no client_reference_id), and duplicate delivery is idempotent.
"""

from unittest import mock

import billing
import flask
import pytest
import stripe


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


def test_active_subscription_sets_claim():
    ev = _event(
        "customer.subscription.updated",
        {"metadata": {"uid": "userA"}, "status": "active"},
    )
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"subscribed": True})


def test_checkout_completed_sets_claim_from_client_reference_id():
    ev = _event(
        "checkout.session.completed",
        {"client_reference_id": "userA", "metadata": {}},
    )
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"subscribed": True})


def test_deleted_event_clears_claim_via_subscription_metadata():
    # No client_reference_id on a delete event; uid resolves from subscription
    # metadata, and the re-fetched status confirms it is no longer active.
    ev = _event("customer.subscription.deleted", {"id": "sub_1", "metadata": {"uid": "userA"}})
    fetched = {"status": "canceled", "metadata": {"uid": "userA"}}
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"subscribed": False})


def test_stale_delete_for_active_subscription_does_not_clear():
    # Out-of-order: a delete event arrives, but the subscription is currently
    # active -> converge to active, do NOT revoke a paying user.
    ev = _event("customer.subscription.deleted", {"id": "sub_1", "metadata": {"uid": "userA"}})
    fetched = {"status": "active", "metadata": {"uid": "userA"}}
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    setc.assert_called_once_with("userA", {"subscribed": True})


def test_payment_failed_clears_via_subscription_lookup():
    ev = _event("invoice.payment_failed", {"subscription": "sub_1", "metadata": {}})
    fetched = {"status": "past_due", "metadata": {"uid": "userA"}}
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch(
        "stripe.Subscription.retrieve", return_value=fetched
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, _ = _invoke(_req())
    assert status == 200
    # past_due is not an active status -> access blocked (R10).
    setc.assert_called_once_with("userA", {"subscribed": False})


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
    ev = _event("customer.subscription.updated", {"metadata": {}, "status": "active"})
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        status, payload = _invoke(_req())
    assert status == 200
    assert payload.get("received") is True
    setc.assert_not_called()


def test_duplicate_delivery_is_idempotent():
    ev = _event(
        "customer.subscription.updated",
        {"metadata": {"uid": "userA"}, "status": "active"},
    )
    with mock.patch("stripe.Webhook.construct_event", return_value=ev), mock.patch.object(
        billing.fb_auth, "set_custom_user_claims"
    ) as setc:
        _invoke(_req())
        _invoke(_req())
    # Same value set both times — idempotent in effect (no divergent side effect).
    assert setc.call_count == 2
    assert all(c.args == ("userA", {"subscribed": True}) for c in setc.call_args_list)
