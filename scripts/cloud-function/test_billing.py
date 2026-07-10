"""Tests for billing.py ``create_checkout_session`` (U3) and
``reconcile_entitlement`` (U4).

Stripe and Firebase Admin are mocked throughout — no network, no real keys. The
key contracts: the uid is server-derived from the verified bearer (never the
request body), it is stamped onto both the session and the subscription metadata
(KTD-4/KTD-9), and the module initializes its own Firebase app (KTD-8).

Two-tier (KTD-1/KTD-2): checkout takes a validated ``tier`` selecting the price
only (the webhook stays the entitlement authority), starts a card-required trial
(``payment_method_collection='always'`` + ``trial_period_days``), and reconcile
resolves the tier from the active subscription's PRICE (never the tier-blind
active bool) so a Local-Pro user cannot self-heal into cloud.
"""

import os
from unittest import mock

import billing
import flask
import pytest
from auth import AuthInvalid, AuthUnavailable

LOCAL_PRICE = "price_local_123"
CLOUD_PRICE = "price_cloud_456"


@pytest.fixture(autouse=True)
def price_env():
    with mock.patch.dict(
        os.environ,
        {
            "STRIPE_PRICE_ID_LOCAL": LOCAL_PRICE,
            "STRIPE_PRICE_ID_CLOUD": CLOUD_PRICE,
            "TRIAL_PERIOD_DAYS": "7",
        },
        clear=False,
    ):
        yield


@pytest.fixture(autouse=True)
def app_context():
    app = flask.Flask(__name__)
    with app.app_context():
        yield


def _req(body=None, method="POST", auth="Bearer tok"):
    headers = {}
    if auth is not None:
        headers["Authorization"] = auth
    return mock.Mock(
        method=method, headers=headers, get_json=mock.Mock(return_value=body or {})
    )


def _invoke(req):
    resp, status, _headers = billing.create_checkout_session(req)
    payload = resp.get_json() if hasattr(resp, "get_json") else resp
    return status, payload


def _sub(price_id, *, status="active"):
    return {
        "status": status,
        "items": {"data": [{"price": {"id": price_id}}]},
    }


# --------------------------------------------------------------------------
# U3 — create_checkout_session: tier param + card-required trial
# --------------------------------------------------------------------------


def test_local_tier_uses_local_price_and_card_required_trial():
    fake_session = mock.Mock(url="https://checkout.stripe/x", id="cs_123")
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.checkout.Session.create", return_value=fake_session
    ) as create:
        # A client-supplied uid AND price in the body must both be ignored.
        status, payload = _invoke(_req({"tier": "local", "uid": "ATTACKER", "price": "px"}))
    assert status == 200
    assert payload["url"] == "https://checkout.stripe/x"
    kwargs = create.call_args.kwargs
    assert kwargs["line_items"] == [{"price": LOCAL_PRICE, "quantity": 1}]
    assert kwargs["client_reference_id"] == "userA"
    assert kwargs["metadata"]["uid"] == "userA"
    assert kwargs["subscription_data"]["metadata"]["uid"] == "userA"
    assert kwargs["subscription_data"]["trial_period_days"] == 7
    assert kwargs["payment_method_collection"] == "always"
    assert kwargs["mode"] == "subscription"


def test_cloud_tier_uses_cloud_price_and_card_required_trial():
    fake_session = mock.Mock(url="https://checkout.stripe/x", id="cs_123")
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.checkout.Session.create", return_value=fake_session
    ) as create:
        status, _ = _invoke(_req({"tier": "cloud"}))
    assert status == 200
    kwargs = create.call_args.kwargs
    assert kwargs["line_items"] == [{"price": CLOUD_PRICE, "quantity": 1}]
    assert kwargs["payment_method_collection"] == "always"
    assert kwargs["subscription_data"]["trial_period_days"] == 7


def test_unknown_tier_400_no_stripe_call():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.checkout.Session.create"
    ) as create:
        status, _ = _invoke(_req({"tier": "enterprise"}))
    assert status == 400
    create.assert_not_called()


def test_missing_tier_400_no_stripe_call():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.checkout.Session.create"
    ) as create:
        status, _ = _invoke(_req({}))
    assert status == 400
    create.assert_not_called()


def test_missing_bearer_401_no_stripe_call():
    with mock.patch.object(
        billing, "verify_bearer", side_effect=AuthInvalid("no token")
    ), mock.patch("stripe.checkout.Session.create") as create:
        status, _ = _invoke(_req({"tier": "cloud"}, auth=None))
    assert status == 401
    create.assert_not_called()


def test_auth_unavailable_503_no_stripe_call():
    with mock.patch.object(
        billing, "verify_bearer", side_effect=AuthUnavailable("outage")
    ), mock.patch("stripe.checkout.Session.create") as create:
        status, _ = _invoke(_req({"tier": "cloud"}))
    assert status == 503
    create.assert_not_called()


def test_stripe_failure_returns_502():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.checkout.Session.create", side_effect=Exception("stripe down")
    ):
        status, _ = _invoke(_req({"tier": "cloud"}))
    assert status == 502


def test_options_preflight_204():
    status = billing.create_checkout_session(_req(method="OPTIONS"))[1]
    assert status == 204


def test_ensure_firebase_app_initializes_when_absent():
    # KTD-8: billing initializes the Admin app itself (does not rely on main.py).
    with mock.patch.object(billing.firebase_admin, "_apps", {}), mock.patch.object(
        billing.firebase_admin, "initialize_app"
    ) as init:
        billing._ensure_firebase_app()
    init.assert_called_once()


def test_ensure_firebase_app_noop_when_present():
    with mock.patch.object(
        billing.firebase_admin, "_apps", {"[DEFAULT]": object()}
    ), mock.patch.object(billing.firebase_admin, "initialize_app") as init:
        billing._ensure_firebase_app()
    init.assert_not_called()


# --------------------------------------------------------------------------
# U4 — reconcile_entitlement (tier-aware, grant-only self-heal)
# --------------------------------------------------------------------------


def _invoke_reconcile(req):
    resp, status, _headers = billing.reconcile_entitlement(req)
    payload = resp.get_json() if hasattr(resp, "get_json") else resp
    return status, payload


def test_reconcile_grants_cloud_from_cloud_price():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", return_value={"data": [_sub(CLOUD_PRICE)]}
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, payload = _invoke_reconcile(_req())
    assert status == 200
    assert payload["tier"] == "cloud"
    assert payload["subscribed"] is True
    setc.assert_called_once_with("userA", {"tier": "cloud", "subscribed": True})


def test_reconcile_grants_local_from_local_price_never_cloud():
    # A Local-Pro user's only active sub must NOT self-heal into cloud.
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", return_value={"data": [_sub(LOCAL_PRICE)]}
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, payload = _invoke_reconcile(_req())
    assert status == 200
    assert payload["tier"] == "local"
    assert payload["subscribed"] is False
    setc.assert_called_once_with("userA", {"tier": "local", "subscribed": False})


def test_reconcile_highest_tier_wins_when_multiple_active():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search",
        return_value={"data": [_sub(LOCAL_PRICE), _sub(CLOUD_PRICE)]},
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, payload = _invoke_reconcile(_req())
    assert status == 200
    assert payload["tier"] == "cloud"
    setc.assert_called_once_with("userA", {"tier": "cloud", "subscribed": True})


def test_reconcile_no_grant_when_no_active_subscription():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", return_value={"data": []}
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, payload = _invoke_reconcile(_req())
    assert status == 200
    assert payload["subscribed"] is False
    assert payload["tier"] is None
    setc.assert_not_called()


def test_reconcile_no_grant_when_only_inactive_subscription():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search",
        return_value={"data": [_sub(CLOUD_PRICE, status="canceled")]},
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, payload = _invoke_reconcile(_req())
    assert status == 200
    assert payload["subscribed"] is False
    setc.assert_not_called()


def test_reconcile_requires_auth():
    with mock.patch.object(
        billing, "verify_bearer", side_effect=AuthInvalid("no token")
    ), mock.patch("stripe.Subscription.search") as search:
        status, _ = _invoke_reconcile(_req(auth=None))
    assert status == 401
    search.assert_not_called()


def test_reconcile_search_failure_does_not_grant():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", side_effect=Exception("stripe down")
    ), mock.patch.object(billing.fb_auth, "set_custom_user_claims") as setc:
        status, payload = _invoke_reconcile(_req())
    assert status == 200
    assert payload["subscribed"] is False
    setc.assert_not_called()
