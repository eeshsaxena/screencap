"""Tests for verify_live_readiness.py — the live-mode readiness gate (plan U2).

Stripe is injected as a mock into the check functions, so no SDK or network is
needed. The key contracts: a test-mode key is rejected before any Stripe call;
prices must be active + recurring; the webhook endpoint must cover every consumed
event; live mode requires a portal configuration; and the account must be able to
charge with a statement descriptor.
"""

import os
from unittest import mock

import pytest
import verify_live_readiness as vlr

_REQUIRED = sorted(vlr.REQUIRED_WEBHOOK_EVENTS)


def _stripe(*, price_overrides=None, endpoints=None, portal_list=None, account=None):
    """A mock ``stripe`` module whose checks all pass unless overridden."""
    m = mock.Mock()

    def price_retrieve(pid):
        return (price_overrides or {}).get(pid, {"active": True, "type": "recurring"})

    m.Price.retrieve.side_effect = price_retrieve
    m.WebhookEndpoint.list.return_value = {
        "data": endpoints
        if endpoints is not None
        else [{"id": "we_1", "status": "enabled", "enabled_events": _REQUIRED}]
    }
    m.billing_portal.Configuration.list.return_value = {
        "data": portal_list if portal_list is not None else [{"id": "bpc_1"}]
    }
    m.Account.retrieve.return_value = account or {
        "charges_enabled": True,
        "statement_descriptor": "SCREENCAP",
    }
    return m


# --- key gate --------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("", False),
        ("sk_test_abc", False),
        ("rk_test_abc", False),
        ("pk_live_abc", False),
        ("sk_live_abc", True),
        ("rk_live_abc", True),
    ],
)
def test_secret_key_is_live(key, expected):
    ok, _ = vlr.check_secret_key_is_live(key)
    assert ok is expected


def test_main_refuses_test_key_before_any_stripe_call():
    # A test key returns exit 2 without importing/using stripe.
    assert vlr.main(["--key", "sk_test_abc"]) == 2


def test_main_refuses_missing_key():
    with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}, clear=False):
        assert vlr.main(["--key", ""]) == 2


# --- prices ----------------------------------------------------------------


def test_prices_active_recurring_pass():
    ok, _ = vlr.check_prices(_stripe(), {"local": "price_l", "cloud": "price_c"})
    assert ok is True


def test_price_not_recurring_fails():
    stripe = _stripe(price_overrides={"price_l": {"active": True, "type": "one_time"}})
    ok, detail = vlr.check_prices(stripe, {"local": "price_l", "cloud": "price_c"})
    assert ok is False
    assert "not recurring" in detail


def test_price_inactive_fails():
    stripe = _stripe(price_overrides={"price_c": {"active": False, "type": "recurring"}})
    ok, detail = vlr.check_prices(stripe, {"local": "price_l", "cloud": "price_c"})
    assert ok is False
    assert "not active" in detail


def test_missing_price_id_fails():
    ok, detail = vlr.check_prices(_stripe(), {"local": "price_l", "cloud": ""})
    assert ok is False
    assert "not configured" in detail


def test_price_retrieve_error_fails_not_crash():
    stripe = _stripe()
    stripe.Price.retrieve.side_effect = Exception("no such price")
    ok, detail = vlr.check_prices(stripe, {"local": "price_l"})
    assert ok is False
    assert "retrieve failed" in detail


# --- webhook ---------------------------------------------------------------


def test_webhook_covers_required_events_passes():
    ok, _ = vlr.check_webhook_endpoint(_stripe(), set(vlr.REQUIRED_WEBHOOK_EVENTS))
    assert ok is True


def test_webhook_wildcard_passes():
    stripe = _stripe(endpoints=[{"id": "we_1", "status": "enabled", "enabled_events": ["*"]}])
    ok, _ = vlr.check_webhook_endpoint(stripe, set(vlr.REQUIRED_WEBHOOK_EVENTS))
    assert ok is True


def test_webhook_missing_one_event_fails():
    partial = [e for e in _REQUIRED if e != "invoice.payment_failed"]
    stripe = _stripe(endpoints=[{"id": "we_1", "status": "enabled", "enabled_events": partial}])
    ok, detail = vlr.check_webhook_endpoint(stripe, set(vlr.REQUIRED_WEBHOOK_EVENTS))
    assert ok is False
    assert "invoice.payment_failed" in detail


def test_webhook_disabled_endpoint_does_not_count():
    stripe = _stripe(endpoints=[{"id": "we_1", "status": "disabled", "enabled_events": ["*"]}])
    ok, _ = vlr.check_webhook_endpoint(stripe, set(vlr.REQUIRED_WEBHOOK_EVENTS))
    assert ok is False


# --- portal ----------------------------------------------------------------


def test_portal_configuration_present_passes():
    ok, _ = vlr.check_portal_configuration(_stripe(), "")
    assert ok is True


def test_portal_configuration_absent_fails():
    ok, detail = vlr.check_portal_configuration(_stripe(portal_list=[]), "")
    assert ok is False
    assert "live mode requires one" in detail


def test_portal_specific_config_inactive_fails():
    stripe = _stripe()
    stripe.billing_portal.Configuration.retrieve.return_value = {"active": False}
    ok, detail = vlr.check_portal_configuration(stripe, "bpc_x")
    assert ok is False
    assert "not active" in detail


# --- account ---------------------------------------------------------------


def test_account_ready_passes():
    ok, _ = vlr.check_account_ready(_stripe())
    assert ok is True


def test_account_not_activated_fails():
    stripe = _stripe(account={"charges_enabled": False, "statement_descriptor": "X"})
    ok, detail = vlr.check_account_ready(stripe)
    assert ok is False
    assert "charges_enabled" in detail


def test_account_missing_descriptor_fails():
    stripe = _stripe(account={"charges_enabled": True})
    ok, detail = vlr.check_account_ready(stripe)
    assert ok is False
    assert "statement descriptor" in detail


def test_account_descriptor_via_settings_passes():
    stripe = _stripe(
        account={
            "charges_enabled": True,
            "settings": {"payments": {"statement_descriptor": "SCREENCAP"}},
        }
    )
    ok, _ = vlr.check_account_ready(stripe)
    assert ok is True


# --- orchestration ---------------------------------------------------------


def test_run_checks_reports_every_check_name():
    results = vlr.run_checks(
        _stripe(), price_ids={"local": "price_l", "cloud": "price_c"}, portal_config_id=""
    )
    names = {name for name, _ok, _detail in results}
    assert names == {"prices", "webhook", "portal", "account"}
    assert all(ok for _name, ok, _detail in results)
