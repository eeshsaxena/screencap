#!/usr/bin/env python3
"""Live-mode readiness gate for the Stripe billing cutover (plan U2 / KTD-6).

Turns the Stripe go-live gotchas into a mechanical pass/fail check the operator
runs BEFORE deploying live keys (U5) and BEFORE flipping any enforcement flag.
Test-mode validation cannot catch these — the customer portal, in particular,
silently falls back to a default in test mode but fails closed in live mode.

Reads the same env var names ``billing.py`` uses so the check inspects the exact
configuration the functions will carry. Read-only: it makes no mutating Stripe
calls and writes nothing.

Checks:
  - key      — the secret key is a LIVE key (``sk_live_``/``rk_live_``); a
               test-mode key is rejected up front so this gate can never pass
               against test mode.
  - prices   — each configured price (LOCAL, CLOUD, and the legacy price when
               set) exists, is ``active``, and is ``recurring``.
  - webhook  — an ``enabled`` webhook endpoint covers every event the webhook
               consumes (or subscribes to ``*``).
  - portal   — a customer-portal Configuration exists (required in live mode;
               ``create_portal_session`` fails closed without one).
  - account  — the account has ``charges_enabled`` and a statement descriptor
               (both gate the first live charge).

Note: the webhook's signing SECRET cannot be read back via the API (Stripe shows
it only at creation), so this gate proves an endpoint with the right events
exists; the deployed secret itself is proven by U5's synthetic-event signature
check and the U7 live drill.

Usage:
    STRIPE_SECRET_KEY=sk_live_... \\
    STRIPE_PRICE_ID_LOCAL=price_... STRIPE_PRICE_ID_CLOUD=price_... \\
    STRIPE_PORTAL_CONFIGURATION_ID=bpc_... \\
        python scripts/cloud-function/verify_live_readiness.py

Exit codes: 0 = all checks pass; 1 = a check failed; 2 = no live key (refused).
"""

from __future__ import annotations

import argparse
import os
import sys

# The events billing.py's stripe_webhook actually consumes (create/updated/
# deleted subscription lifecycle + checkout completion + failed renewal). An
# endpoint must cover all of these, or entitlement events are silently dropped.
REQUIRED_WEBHOOK_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
    }
)


def _env(name: str) -> str:
    return os.environ.get(name, "")


def check_secret_key_is_live(key: str) -> tuple[bool, str]:
    """A test-mode or missing key can never pass the live readiness gate."""
    if not key:
        return False, "STRIPE_SECRET_KEY is unset"
    if key.startswith("sk_test_") or key.startswith("rk_test_"):
        return False, "a test-mode key (sk_test_/rk_test_) can never pass the live gate"
    if not (key.startswith("sk_live_") or key.startswith("rk_live_")):
        return False, "not a recognized live key (expected sk_live_/rk_live_)"
    return True, "live secret key"


def check_prices(stripe, price_ids: dict[str, str]) -> tuple[bool, str]:
    """Every configured price must exist and be recurring; the two paid tiers
    must also be active and distinct.

    The legacy grandfather price (``legacy``) is exempt from the active check:
    once the $5 tier is retired it is archived (``active=false``) in Stripe, yet
    billing.py's price->tier map still maps it (R11), so requiring it active
    would spuriously fail the gate and block the cutover. The two paid tiers
    must be distinct — billing.py's first-wins ``setdefault`` map would silently
    drop a tier if ``local`` and ``cloud`` collided (the .env.example invariant).
    """
    problems: list[str] = []
    for label, pid in price_ids.items():
        if not pid:
            problems.append(f"{label}: price id not configured")
            continue
        try:
            price = stripe.Price.retrieve(pid)
        except Exception as exc:  # missing price / network — a fail, not a crash.
            problems.append(f"{label} ({pid}): retrieve failed: {exc}")
            continue
        if label != "legacy" and not price.get("active"):
            problems.append(f"{label} ({pid}): not active")
        if price.get("type") != "recurring":
            problems.append(f"{label} ({pid}): not recurring (type={price.get('type')})")
    local, cloud = price_ids.get("local"), price_ids.get("cloud")
    if local and cloud and local == cloud:
        problems.append("local and cloud price ids are identical (must be distinct)")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(price_ids)} price(s) valid"


def check_webhook_endpoint(stripe, required_events: set[str]) -> tuple[bool, str]:
    """An enabled endpoint must cover every consumed event (or subscribe to *)."""
    try:
        result = stripe.WebhookEndpoint.list(limit=100)
    except Exception as exc:
        return False, f"webhook endpoint list failed: {exc}"
    for endpoint in result.get("data", []):
        if endpoint.get("status") != "enabled":
            continue
        events = set(endpoint.get("enabled_events") or [])
        if "*" in events or required_events <= events:
            return True, f"enabled endpoint covers required events ({endpoint.get('id')})"
    return False, (
        "no enabled webhook endpoint covers all required events: "
        + ", ".join(sorted(required_events))
    )


def check_portal_configuration(stripe, config_id: str) -> tuple[bool, str]:
    """Live mode requires the SPECIFIC saved portal configuration the deployed
    function will use.

    billing.py's ``create_portal_session`` fails closed in live mode when
    ``STRIPE_PORTAL_CONFIGURATION_ID`` is unset, and deploy_billing.sh only
    *warns* on an unset id — so "some configuration exists" is not enough. The
    gate requires the exact id and verifies it exists and is active, mirroring
    the runtime requirement, or the deployed portal would fail closed while the
    gate read green.
    """
    if not config_id:
        return False, (
            "STRIPE_PORTAL_CONFIGURATION_ID is unset — the live portal fails "
            "closed without it (billing.py); set the id the deploy will use"
        )
    try:
        cfg = stripe.billing_portal.Configuration.retrieve(config_id)
    except Exception as exc:
        return False, f"portal configuration {config_id} check failed: {exc}"
    if not cfg.get("active", False):
        return False, f"portal configuration {config_id} is not active"
    return True, f"portal configuration {config_id} present and active"


def check_account_ready(stripe) -> tuple[bool, str]:
    """The account must be able to charge and carry a statement descriptor."""
    try:
        account = stripe.Account.retrieve()
    except Exception as exc:
        return False, f"account retrieve failed: {exc}"
    problems: list[str] = []
    if not account.get("charges_enabled"):
        problems.append("charges_enabled is false (account not fully activated)")
    descriptor = account.get("statement_descriptor")
    if not descriptor:
        # Newer API nests it under settings.payments.statement_descriptor.
        settings = account.get("settings") or {}
        payments = settings.get("payments") or {}
        descriptor = payments.get("statement_descriptor")
    if not descriptor:
        problems.append("no statement descriptor set")
    if problems:
        return False, "; ".join(problems)
    return True, "charges enabled + statement descriptor set"


def run_checks(
    stripe, *, price_ids: dict[str, str], portal_config_id: str
) -> list[tuple[str, bool, str]]:
    """Run every Stripe-side check; return ``[(name, ok, detail), ...]``."""
    return [
        ("prices", *check_prices(stripe, price_ids)),
        ("webhook", *check_webhook_endpoint(stripe, set(REQUIRED_WEBHOOK_EVENTS))),
        ("portal", *check_portal_configuration(stripe, portal_config_id)),
        ("account", *check_account_ready(stripe)),
    ]


def _resolve_price_ids() -> dict[str, str]:
    """LOCAL + CLOUD are required; the legacy price is checked only when set."""
    price_ids = {
        "local": _env("STRIPE_PRICE_ID_LOCAL"),
        "cloud": _env("STRIPE_PRICE_ID_CLOUD"),
    }
    legacy = _env("STRIPE_PRICE_ID")
    if legacy:
        price_ids["legacy"] = legacy
    return price_ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--key", help="Stripe secret key (default: STRIPE_SECRET_KEY env)."
    )
    args = parser.parse_args(argv)

    key = args.key or _env("STRIPE_SECRET_KEY")
    ok, detail = check_secret_key_is_live(key)
    print(f"[{'PASS' if ok else 'FAIL'}] key: {detail}")
    if not ok:
        print(
            "Refusing to run live-readiness checks without a live key.",
            file=sys.stderr,
        )
        return 2

    import stripe

    stripe.api_key = key

    results = run_checks(
        stripe,
        price_ids=_resolve_price_ids(),
        portal_config_id=_env("STRIPE_PORTAL_CONFIGURATION_ID"),
    )
    all_ok = True
    for name, check_ok, check_detail in results:
        print(f"[{'PASS' if check_ok else 'FAIL'}] {name}: {check_detail}")
        all_ok = all_ok and check_ok

    if not all_ok:
        print(
            "Live readiness: FAILED — do not deploy live keys or flip enforcement "
            "until every check is green.",
            file=sys.stderr,
        )
        return 1
    print("Live readiness: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
