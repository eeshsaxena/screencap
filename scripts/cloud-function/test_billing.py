"""Tests for billing.py ``create_checkout_session`` (U3).

Stripe and Firebase Admin are mocked throughout — no network, no real keys. The
key contracts: the uid is server-derived from the verified bearer (never the
request body), it is stamped onto both the session and the subscription metadata
(KTD-4/KTD-9), and the module initializes its own Firebase app (KTD-8).
"""

from unittest import mock

import billing
import flask
import pytest
from auth import AuthInvalid, AuthUnavailable


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


def test_creates_session_bound_to_token_uid():
    fake_session = mock.Mock(url="https://checkout.stripe/x", id="cs_123")
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.checkout.Session.create", return_value=fake_session
    ) as create:
        # A client-supplied uid in the body must be ignored.
        status, payload = _invoke(_req({"uid": "ATTACKER"}))
    assert status == 200
    assert payload["url"] == "https://checkout.stripe/x"
    kwargs = create.call_args.kwargs
    assert kwargs["client_reference_id"] == "userA"
    assert kwargs["metadata"]["uid"] == "userA"
    assert kwargs["subscription_data"]["metadata"]["uid"] == "userA"
    assert kwargs["allow_promotion_codes"] is True
    assert kwargs["mode"] == "subscription"


def test_missing_bearer_401_no_stripe_call():
    with mock.patch.object(
        billing, "verify_bearer", side_effect=AuthInvalid("no token")
    ), mock.patch("stripe.checkout.Session.create") as create:
        status, _ = _invoke(_req(auth=None))
    assert status == 401
    create.assert_not_called()


def test_auth_unavailable_503_no_stripe_call():
    with mock.patch.object(
        billing, "verify_bearer", side_effect=AuthUnavailable("outage")
    ), mock.patch("stripe.checkout.Session.create") as create:
        status, _ = _invoke(_req())
    assert status == 503
    create.assert_not_called()


def test_stripe_failure_returns_502():
    with mock.patch.object(billing, "verify_bearer", return_value="userA"), mock.patch(
        "stripe.checkout.Session.create", side_effect=Exception("stripe down")
    ):
        status, _ = _invoke(_req())
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
