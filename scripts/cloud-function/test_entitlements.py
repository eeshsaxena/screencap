"""Entitlement store + founding-grant tests (U1).

Two layers:
- the ``entitlements`` module in isolation (``fb_auth`` mocked): the free-default
  read rule, the live-lookup round-trip, grant sets + is idempotent, and the
  transient-failure -> EntitlementUnavailable mapping;
- the grant action wired into the Cloud Function dispatcher (``main.py``): an
  authenticated grant sets founding and returns it, an unauthenticated grant is
  rejected 401 (auth is the first thing the handler does), and a transient grant
  failure is a 503.

``firebase_admin`` is a real import (its error classes are used), but every
network call (``get_user`` / ``set_custom_user_claims``) is mocked, so the suite
runs fully offline like the rest of scripts/cloud-function/.
"""

from unittest import mock

import entitlements
import firebase_admin.exceptions
import flask
import main
import pytest
from auth import AuthInvalid, AuthUnavailable
from firebase_admin import auth as fb_auth


@pytest.fixture(autouse=True)
def app_context():
    # jsonify() needs an application context (the grant handler returns JSON).
    app = flask.Flask(__name__)
    with app.app_context():
        yield


class _FakeUser:
    def __init__(self, custom_claims=None):
        self.custom_claims = custom_claims


# --------------------------------------------------------------------------
# plan_from_claims / entitlement_from_plan — the pure shaping layer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims,expected",
    [
        (None, "free"),
        ({}, "free"),
        ({"other": 1}, "free"),
        ({"plan": None}, "free"),
        ({"plan": ""}, "free"),
        ({"plan": "founding"}, "founding"),
    ],
)
def test_plan_from_claims_defaults_free(claims, expected):
    assert entitlements.plan_from_claims(claims) == expected


def test_entitlement_shape_founding_is_active_expires_null():
    ent = entitlements.entitlement_from_plan("founding")
    assert ent == {"plan": "founding", "active": True, "expires": None}


def test_entitlement_shape_free_is_inactive():
    ent = entitlements.entitlement_from_plan("free")
    assert ent == {"plan": "free", "active": False, "expires": None}


# --------------------------------------------------------------------------
# read_entitlement — LIVE get_user lookup (never off the token)
# --------------------------------------------------------------------------


def test_read_entitlement_free_for_uid_with_no_claim():
    with mock.patch.object(fb_auth, "get_user", return_value=_FakeUser(None)) as gu:
        ent = entitlements.read_entitlement("uidA")
    assert ent == {"plan": "free", "active": False, "expires": None}
    gu.assert_called_once_with("uidA")


def test_read_entitlement_round_trips_founding():
    with mock.patch.object(fb_auth, "get_user", return_value=_FakeUser({"plan": "founding"})):
        ent = entitlements.read_entitlement("uidA")
    assert ent == {"plan": "founding", "active": True, "expires": None}


def test_read_entitlement_missing_user_reads_free_not_error():
    with mock.patch.object(fb_auth, "get_user", side_effect=fb_auth.UserNotFoundError("no user")):
        ent = entitlements.read_entitlement("ghost")
    assert ent == {"plan": "free", "active": False, "expires": None}


def test_read_entitlement_transient_failure_raises_unavailable():
    err = firebase_admin.exceptions.FirebaseError("unavailable", "firebase outage")
    with mock.patch.object(fb_auth, "get_user", side_effect=err):
        with pytest.raises(entitlements.EntitlementUnavailable):
            entitlements.read_entitlement("uidA")


# --------------------------------------------------------------------------
# grant_founding — sets the claim, idempotent, preserves unrelated claims
# --------------------------------------------------------------------------


def test_grant_founding_sets_claim():
    with mock.patch.object(fb_auth, "get_user", return_value=_FakeUser(None)), \
         mock.patch.object(fb_auth, "set_custom_user_claims") as setc:
        ent = entitlements.grant_founding("uidA")
    assert ent == {"plan": "founding", "active": True, "expires": None}
    setc.assert_called_once_with("uidA", {"plan": "founding"})


def test_grant_founding_idempotent_on_repeat():
    # A repeat grant re-reads the already-founding claim and re-sets the same value.
    with mock.patch.object(fb_auth, "get_user", return_value=_FakeUser({"plan": "founding"})), \
         mock.patch.object(fb_auth, "set_custom_user_claims") as setc:
        ent = entitlements.grant_founding("uidA")
    assert ent == {"plan": "founding", "active": True, "expires": None}
    setc.assert_called_once_with("uidA", {"plan": "founding"})


def test_grant_founding_preserves_unrelated_claims():
    with mock.patch.object(fb_auth, "get_user", return_value=_FakeUser({"role": "beta"})), \
         mock.patch.object(fb_auth, "set_custom_user_claims") as setc:
        entitlements.grant_founding("uidA")
    setc.assert_called_once_with("uidA", {"role": "beta", "plan": "founding"})


def test_grant_founding_transient_failure_raises_unavailable():
    err = firebase_admin.exceptions.FirebaseError("unavailable", "outage")
    with mock.patch.object(fb_auth, "get_user", side_effect=err):
        with pytest.raises(entitlements.EntitlementUnavailable):
            entitlements.grant_founding("uidA")


# --------------------------------------------------------------------------
# Grant action wired into the dispatcher (main.py) — auth is the first gate
# --------------------------------------------------------------------------


def _req(body):
    return mock.Mock(
        method="POST",
        headers={"Authorization": "Bearer tok"},
        get_json=mock.Mock(return_value=body),
    )


def _invoke(req):
    resp, status, _headers = main.get_upload_urls(req)
    payload = resp.get_json() if hasattr(resp, "get_json") else resp
    return status, payload


def test_grant_action_authenticated_sets_founding():
    with mock.patch.object(main, "verify_bearer", return_value="userA"), \
         mock.patch.object(main, "grant_founding", return_value=entitlements.entitlement_from_plan("founding")) as grant:
        status, payload = _invoke(_req({"action": "grant-founding"}))
    assert status == 200
    assert payload["entitlement"] == {"plan": "founding", "active": True, "expires": None}
    grant.assert_called_once_with("userA")


def test_grant_action_unauthenticated_401():
    # Auth is the first thing the handler does — a tokenless grant never reaches
    # grant_founding (no entitlement write without a verified uid).
    with mock.patch.object(main, "verify_bearer", side_effect=AuthInvalid("no token")), \
         mock.patch.object(main, "grant_founding") as grant:
        status, _ = _invoke(_req({"action": "grant-founding"}))
    assert status == 401
    grant.assert_not_called()


def test_grant_action_auth_unavailable_503():
    with mock.patch.object(main, "verify_bearer", side_effect=AuthUnavailable("outage")), \
         mock.patch.object(main, "grant_founding") as grant:
        status, _ = _invoke(_req({"action": "grant-founding"}))
    assert status == 503
    grant.assert_not_called()


def test_grant_action_transient_grant_failure_503():
    with mock.patch.object(main, "verify_bearer", return_value="userA"), \
         mock.patch.object(main, "grant_founding", side_effect=entitlements.EntitlementUnavailable("outage")):
        status, payload = _invoke(_req({"action": "grant-founding"}))
    assert status == 503
    assert "error" in payload
