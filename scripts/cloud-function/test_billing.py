"""Tests for billing.py ``create_checkout_session`` (U3),
``reconcile_entitlement`` (U4), and ``create_portal_session`` (account-sheet U1).

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

import logging
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


# --------------------------------------------------------------------------
# Account-sheet U1 — create_portal_session (customer-portal URL)
# --------------------------------------------------------------------------
#
# New portal tests carry @pytest.mark.privacy so the chain runs in the CI
# privacy lane (account-sheet plan KTD-8); the older checkout/reconcile tests
# above are deliberately left unmarked.

PORTAL_URL = "https://billing.stripe/portal-session-xyz"


def _psub(price_id, *, status="active", customer="cus_1", created=0):
    """A minimal subscription as returned by Subscription.search for portal tests."""
    return {
        "status": status,
        "customer": customer,
        "created": created,
        "items": {"data": [{"price": {"id": price_id}}]},
    }


def _invoke_portal(req):
    resp, status, _headers = billing.create_portal_session(req)
    payload = resp.get_json() if hasattr(resp, "get_json") else resp
    return status, payload


@pytest.mark.privacy
def test_portal_happy_path_returns_url():
    fake_session = mock.Mock(url=PORTAL_URL)
    with mock.patch.dict(
        os.environ, {"STRIPE_PORTAL_RETURN_URL": "https://screencap.sh/account"}
    ), mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search",
        return_value={"data": [_psub(CLOUD_PRICE, customer="cus_42")]},
    ), mock.patch("stripe.billing_portal.Session.create", return_value=fake_session) as create:
        status, payload = _invoke_portal(_req())
    assert status == 200
    assert payload["url"] == PORTAL_URL
    kwargs = create.call_args.kwargs
    assert kwargs["customer"] == "cus_42"
    assert kwargs["return_url"] == "https://screencap.sh/account"
    # No configuration is passed unless STRIPE_PORTAL_CONFIGURATION_ID is set.
    assert "configuration" not in kwargs


@pytest.mark.privacy
def test_portal_trialing_subscription_resolves():
    # A card-required trial has a real Stripe subscription — Manage Subscription
    # must work pre-conversion (R7 / AE6 backend half).
    fake_session = mock.Mock(url=PORTAL_URL)
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search",
        return_value={"data": [_psub(CLOUD_PRICE, status="trialing", customer="cus_trial")]},
    ), mock.patch("stripe.billing_portal.Session.create", return_value=fake_session) as create:
        status, payload = _invoke_portal(_req())
    assert status == 200
    assert payload["url"] == PORTAL_URL
    assert create.call_args.kwargs["customer"] == "cus_trial"


@pytest.mark.privacy
def test_portal_no_subscription_4xx_no_session_create():
    # "Nothing to manage" is a distinct structured error, never a Stripe
    # pass-through (R12 / AE8) — and Session.create is never reached.
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", return_value={"data": []}
    ), mock.patch("stripe.billing_portal.Session.create") as create:
        status, payload = _invoke_portal(_req())
    assert 400 <= status < 500
    assert payload["error"] == "no_subscription"
    assert payload["code"] == "no_subscription"
    create.assert_not_called()


@pytest.mark.privacy
def test_portal_only_inactive_subscription_is_no_subscription():
    # A canceled sub must not open a portal — fail closed to the 4xx.
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search",
        return_value={"data": [_psub(CLOUD_PRICE, status="canceled")]},
    ), mock.patch("stripe.billing_portal.Session.create") as create:
        status, payload = _invoke_portal(_req())
    assert 400 <= status < 500
    assert payload["code"] == "no_subscription"
    create.assert_not_called()


@pytest.mark.privacy
def test_portal_injection_uid_rejected_before_any_stripe_call():
    # Stripe search syntax accepts quotes/operators; an interpolated uid like
    # x' OR ... could open ANOTHER customer's portal. Reject pre-query (KTD-2).
    for bad_uid in ("x'", "x' OR metadata['uid']:'y", "a b", "", "uïd"):
        with mock.patch.object(billing, "verify_bearer", return_value=bad_uid), mock.patch(
            "stripe.Subscription.search"
        ) as search, mock.patch("stripe.billing_portal.Session.create") as create:
            status, _ = _invoke_portal(_req())
        assert 400 <= status < 500, bad_uid
        search.assert_not_called()
        create.assert_not_called()


@pytest.mark.privacy
def test_portal_picks_active_subscriptions_customer_among_many():
    # Each checkout mints a NEW customer: a canceled sub on cus_old must lose to
    # the active sub on cus_new regardless of ordering.
    fake_session = mock.Mock(url=PORTAL_URL)
    subs = [
        _psub(CLOUD_PRICE, status="canceled", customer="cus_old", created=200),
        _psub(CLOUD_PRICE, status="active", customer="cus_new", created=100),
    ]
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", return_value={"data": subs}
    ), mock.patch("stripe.billing_portal.Session.create", return_value=fake_session) as create:
        status, _ = _invoke_portal(_req())
    assert status == 200
    assert create.call_args.kwargs["customer"] == "cus_new"


@pytest.mark.privacy
def test_portal_highest_tier_wins_then_newest_created():
    # Deterministic selection (KTD-2): active/trialing filter -> highest tier ->
    # newest created. Cloud beats a newer Local sub; a tier tie goes to newest.
    fake_session = mock.Mock(url=PORTAL_URL)
    subs = [
        _psub(LOCAL_PRICE, customer="cus_local", created=900),
        _psub(CLOUD_PRICE, customer="cus_cloud_old", created=100),
        _psub(CLOUD_PRICE, customer="cus_cloud_new", created=500),
    ]
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", return_value={"data": subs}
    ), mock.patch("stripe.billing_portal.Session.create", return_value=fake_session) as create:
        status, _ = _invoke_portal(_req())
    assert status == 200
    assert create.call_args.kwargs["customer"] == "cus_cloud_new"


@pytest.mark.privacy
def test_portal_missing_bearer_401_no_stripe_call():
    with mock.patch.object(
        billing, "verify_bearer", side_effect=AuthInvalid("no token")
    ), mock.patch("stripe.Subscription.search") as search:
        status, _ = _invoke_portal(_req(auth=None))
    assert status == 401
    search.assert_not_called()


@pytest.mark.privacy
def test_portal_auth_unavailable_503_no_stripe_call():
    with mock.patch.object(
        billing, "verify_bearer", side_effect=AuthUnavailable("outage")
    ), mock.patch("stripe.Subscription.search") as search:
        status, _ = _invoke_portal(_req())
    assert status == 503
    search.assert_not_called()


@pytest.mark.privacy
def test_portal_stripe_failure_502_generic_body():
    # e.g. live mode with no saved portal configuration — the body must stay
    # generic; the exception text never reaches the client.
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", return_value={"data": [_psub(CLOUD_PRICE)]}
    ), mock.patch(
        "stripe.billing_portal.Session.create",
        side_effect=Exception("No configuration provided; secret_internal_detail"),
    ):
        status, payload = _invoke_portal(_req())
    assert status == 502
    assert "secret_internal_detail" not in str(payload)
    assert "No configuration provided" not in str(payload)


@pytest.mark.privacy
def test_portal_options_preflight_204_cors():
    body, status, headers = billing.create_portal_session(_req(method="OPTIONS"))
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "*"


@pytest.mark.privacy
def test_portal_configuration_env_passed_through():
    # STRIPE_PORTAL_CONFIGURATION_ID pins the U7 allowlist in code, not
    # dashboard state.
    fake_session = mock.Mock(url=PORTAL_URL)
    with mock.patch.dict(
        os.environ, {"STRIPE_PORTAL_CONFIGURATION_ID": "bpc_test_123"}
    ), mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.Subscription.search", return_value={"data": [_psub(CLOUD_PRICE)]}
    ), mock.patch("stripe.billing_portal.Session.create", return_value=fake_session) as create:
        status, _ = _invoke_portal(_req())
    assert status == 200
    assert create.call_args.kwargs["configuration"] == "bpc_test_123"


@pytest.mark.privacy
def test_portal_happy_path_never_logs_session_url(caplog):
    # The portal URL is a capability link into another party's billing account —
    # it must never be written to logs (U1 hard rule).
    fake_session = mock.Mock(url=PORTAL_URL)
    with caplog.at_level(logging.DEBUG), mock.patch.object(
        billing, "verify_bearer", return_value="userA"
    ), mock.patch(
        "stripe.Subscription.search", return_value={"data": [_psub(CLOUD_PRICE)]}
    ), mock.patch("stripe.billing_portal.Session.create", return_value=fake_session):
        status, _ = _invoke_portal(_req())
    assert status == 200
    for record in caplog.records:
        assert PORTAL_URL not in record.getMessage()
