"""Tests for scripts/set_entitlement.py — the Stripe-independent comp tool
(U12 / U4).

The Firebase Admin SDK is mocked; no network or real project is touched. The
key contracts: grant sets the two-tier claim ``{tier, subscribed}`` with the
fail-closed invariant ``subscribed = (tier == "cloud")`` (KTD-1), revoke clears
both while preserving other custom claims, email/uid both resolve, and the
grandfather backfill migrates legacy ``subscribed=true`` accounts to
``tier=cloud`` idempotently (KTD-6).
"""

import importlib.util
import pathlib
from unittest import mock

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "set_entitlement.py"


def _load():
    spec = importlib.util.spec_from_file_location("set_entitlement_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_grant_cloud_sets_tier_and_subscribed_true():
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user", return_value=user) as get_user, \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        uid = mod.set_entitlement("userA", is_email=False, tier="cloud", project_id="p")
    assert uid == "userA"
    get_user.assert_called_once_with("userA")
    setc.assert_called_once_with("userA", {"tier": "cloud", "subscribed": True})


def test_grant_local_sets_subscribed_false():
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user", return_value=user), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        mod.set_entitlement("userA", is_email=False, tier="local", project_id="p")
    setc.assert_called_once_with("userA", {"tier": "local", "subscribed": False})


def test_revoke_clears_both_and_preserves_other_claims():
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={"subscribed": True, "tier": "cloud", "other": 1})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user_by_email", return_value=user), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        mod.set_entitlement("e@x.com", is_email=True, tier=None, project_id="p")
    setc.assert_called_once_with("userA", {"subscribed": False, "tier": "none", "other": 1})


def test_main_grant_by_email_defaults_to_cloud(capsys):
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user_by_email", return_value=user), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        rc = mod.main(["--email", "e@x.com"])
    assert rc == 0
    setc.assert_called_once_with("userA", {"tier": "cloud", "subscribed": True})
    assert "Granted" in capsys.readouterr().out


def test_main_grant_local_tier(capsys):
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user", return_value=user), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        rc = mod.main(["--uid", "userA", "--tier", "local"])
    assert rc == 0
    setc.assert_called_once_with("userA", {"tier": "local", "subscribed": False})


def test_main_revoke_clears(capsys):
    mod = _load()
    user = mock.Mock(uid="userA", custom_claims={"tier": "cloud", "subscribed": True})
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "get_user", return_value=user), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        rc = mod.main(["--uid", "userA", "--revoke"])
    assert rc == 0
    setc.assert_called_once_with("userA", {"tier": "none", "subscribed": False})
    assert "Revoked" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Grandfather backfill (KTD-6): migrate legacy subscribed=true -> tier=cloud
# --------------------------------------------------------------------------


def _fake_user(uid, claims):
    return mock.Mock(uid=uid, custom_claims=claims)


def _page(*users):
    """A fake ListUsersPage whose ``iterate_all()`` pages through ``users``.

    The backfill reads ``list_users().iterate_all()`` (the whole project), not
    the first-page ``.users`` slice, so tests mock ``iterate_all``.
    """
    return mock.Mock(iterate_all=lambda: iter(users))


def test_backfill_migrates_legacy_subscribed_accounts():
    mod = _load()
    # One legacy cloud sub (no tier), one already-migrated, one non-sub.
    page = _page(
        _fake_user("legacy1", {"subscribed": True}),
        _fake_user("already", {"subscribed": True, "tier": "cloud"}),
        _fake_user("free", {}),
    )
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "list_users", return_value=page), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        migrated = mod.backfill_grandfathered(project_id="p")
    # Only the legacy (subscribed=true, tier missing) account is written.
    assert migrated == ["legacy1"]
    setc.assert_called_once_with("legacy1", {"subscribed": True, "tier": "cloud"})


def test_backfill_is_idempotent_on_rerun():
    mod = _load()
    # After a first pass every subscribed account already carries tier=cloud.
    page = _page(
        _fake_user("legacy1", {"subscribed": True, "tier": "cloud"}),
        _fake_user("free", {}),
    )
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "list_users", return_value=page), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        migrated = mod.backfill_grandfathered(project_id="p")
    assert migrated == []
    setc.assert_not_called()


def test_backfill_pages_past_the_first_list_users_page():
    # Regression: the backfill must page through ALL users via iterate_all(),
    # not read only the first list_users() page (.users). A page whose first-page
    # .users is empty but whose iterate_all() still yields a candidate proves it.
    mod = _load()
    page = mock.Mock(
        users=[],
        iterate_all=lambda: iter([_fake_user("legacy_late", {"subscribed": True})]),
    )
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "list_users", return_value=page), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        migrated = mod.backfill_grandfathered(project_id="p")
    assert migrated == ["legacy_late"]
    setc.assert_called_once_with("legacy_late", {"subscribed": True, "tier": "cloud"})


def test_backfill_dry_run_counts_without_writing():
    mod = _load()
    page = _page(
        _fake_user("legacy1", {"subscribed": True}),
        _fake_user("legacy2", {"subscribed": True}),
        _fake_user("already", {"subscribed": True, "tier": "cloud"}),
        _fake_user("free", {}),
    )
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "list_users", return_value=page), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        matched = mod.backfill_grandfathered(project_id="p", dry_run=True)
    # Both candidates are counted; nothing is written.
    assert matched == ["legacy1", "legacy2"]
    setc.assert_not_called()


def test_main_backfill_flag(capsys):
    mod = _load()
    page = _page(_fake_user("legacy1", {"subscribed": True}))
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "list_users", return_value=page), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        rc = mod.main(["--backfill-grandfathered"])
    assert rc == 0
    setc.assert_called_once_with("legacy1", {"subscribed": True, "tier": "cloud"})
    assert "1" in capsys.readouterr().out


def test_main_backfill_dry_run_writes_nothing(capsys):
    mod = _load()
    page = _page(
        _fake_user("legacy1", {"subscribed": True}),
        _fake_user("legacy2", {"subscribed": True}),
    )
    with mock.patch.object(mod.firebase_admin, "_apps", {"[DEFAULT]": object()}), \
         mock.patch.object(mod.fb_auth, "list_users", return_value=page), \
         mock.patch.object(mod.fb_auth, "set_custom_user_claims") as setc:
        rc = mod.main(["--backfill-grandfathered", "--dry-run"])
    assert rc == 0
    setc.assert_not_called()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "2" in out


def test_dry_run_requires_backfill():
    # --dry-run only makes sense with the mass backfill; guard it so a stray
    # --dry-run on a single-account grant can't read as a silent no-write grant.
    mod = _load()
    with pytest.raises(SystemExit):
        mod.main(["--email", "e@x.com", "--dry-run"])
