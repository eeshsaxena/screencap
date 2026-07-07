"""Tests for scripts/set_entitlement.py — the Stripe-independent comp tool (U12).

The Firebase Admin SDK is mocked; no network or real project is touched. The
key contracts: grant sets ``subscribed=True``, revoke sets ``False`` while
preserving other custom claims, and email/uid both resolve.
"""

import importlib.util
import pathlib
from unittest import mock

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "set_entitlement.py"


def _load():
    spec = importlib.util.spec_from_file_location("set_entitlement_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_grant_sets_subscribed_true():
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user", return_value=user) as get_user, \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        uid = mod.set_entitlement("userA", is_email=False, subscribed=True, project_id="p")
    assert uid == "userA"
    get_user.assert_called_once_with("userA")
    setc.assert_called_once_with("userA", {"subscribed": True})


def test_revoke_sets_false_and_preserves_other_claims():
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={"subscribed": True, "other": 1})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user_by_email", return_value=user), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        mod.set_entitlement("e@x.com", is_email=True, subscribed=False, project_id="p")
    setc.assert_called_once_with("userA", {"subscribed": False, "other": 1})


def test_main_grant_by_email(capsys):
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user_by_email", return_value=user), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        rc = mod.main(["--email", "e@x.com"])
    assert rc == 0
    setc.assert_called_once_with("userA", {"subscribed": True})
    assert "Granted" in capsys.readouterr().out
