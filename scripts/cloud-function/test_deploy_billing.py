"""Tests for deploy_billing.sh — the Secret-Manager deploy wrapper (plan U3).

Shells out to the script in --dry-run (which prints the resolved gcloud commands
without deploying) and exercises the guards, so no real gcloud call is ever made.
The load-bearing invariants: --dry-run prints secret NAME references
(stripe-secret-key:latest), never raw secret VALUES; a raw secret in any of the
key vars is refused up front; and the entitlement grantors deploy before the
checkout path.
"""

import os
import pathlib
import subprocess

_SCRIPT = pathlib.Path(__file__).resolve().parent / "deploy_billing.sh"

# Substrings that must NEVER appear in --dry-run output (raw secret values).
_RAW_SECRET_MARKERS = ("sk_live_", "sk_test_", "rk_live_", "rk_test_", "whsec_")


def _run(args, extra_env=None):
    """Run the script with a clean, minimal env plus overrides."""
    env = {"PATH": os.environ.get("PATH", "")}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


_VALID_ENV = {
    "STRIPE_PRICE_ID_LOCAL": "price_L",
    "STRIPE_PRICE_ID_CLOUD": "price_C",
    "STRIPE_PORTAL_CONFIGURATION_ID": "bpc_1",
}


def test_dry_run_prints_five_deploy_commands():
    r = _run(["--dry-run"], _VALID_ENV)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("gcloud functions deploy ") == 5


def test_dry_run_prints_secret_references_not_values():
    r = _run(["--dry-run"], _VALID_ENV)
    assert r.returncode == 0, r.stderr
    # Secret Manager NAME references appear...
    assert "stripe-secret-key:latest" in r.stdout
    assert "stripe-webhook-secret:latest" in r.stdout
    # ...and no raw secret value ever leaks.
    for marker in _RAW_SECRET_MARKERS:
        assert marker not in r.stdout, f"raw secret marker leaked: {marker}"


def test_grantors_deploy_before_checkout():
    r = _run(["--dry-run"], _VALID_ENV)
    assert r.returncode == 0, r.stderr
    webhook_at = r.stdout.index("deploy stripe-webhook")
    reconcile_at = r.stdout.index("deploy reconcile-entitlement")
    checkout_at = r.stdout.index("deploy create-checkout-session")
    assert webhook_at < checkout_at
    assert reconcile_at < checkout_at


def test_raw_secret_in_value_var_is_refused():
    r = _run(["--dry-run"], {**_VALID_ENV, "STRIPE_SECRET_KEY": "sk_live_leaked"})
    assert r.returncode == 1
    assert "raw secret" in r.stderr


def test_raw_secret_in_secret_name_var_is_refused():
    # The SECRET_* vars hold Secret Manager NAMES; a raw value there must also be
    # refused, since it flows into --set-secrets and the --dry-run output.
    r = _run(["--dry-run"], {**_VALID_ENV, "SECRET_STRIPE_SECRET_KEY": "sk_live_leaked"})
    assert r.returncode == 1
    assert "raw secret" in r.stderr


def test_missing_prices_fail_a_real_deploy_before_any_gcloud_call():
    # No --dry-run and no prices: the required-config check exits 1 BEFORE the
    # deploy loop, so no gcloud is ever invoked.
    r = _run([], {"STRIPE_PORTAL_CONFIGURATION_ID": "bpc_1"})
    assert r.returncode == 1
    assert "unset required config" in r.stderr


def test_missing_prices_only_warn_in_dry_run():
    r = _run(["--dry-run"], {"STRIPE_PORTAL_CONFIGURATION_ID": "bpc_1"})
    assert r.returncode == 0
    assert "WARNING" in r.stderr


def test_unknown_argument_is_rejected():
    r = _run(["--bogus"], _VALID_ENV)
    assert r.returncode == 2
