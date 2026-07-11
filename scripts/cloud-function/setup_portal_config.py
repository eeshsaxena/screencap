#!/usr/bin/env python3
"""Run-once: create the Stripe customer-portal ``Configuration`` that
``create_portal_session`` (billing.py) needs before its first LIVE-mode call
(account-sheet plan U7).

Stripe's hosted customer portal refuses ``billing_portal.Session.create()`` in
live mode until a Configuration exists (test mode silently falls back to an
account default, which masks this failure until launch — see billing.py's
pre-launch checklist). This script creates one scoped to EXACTLY the two paid
products (Local Pro, Cloud) so a customer can only plan-switch between them —
omitting the ``products`` allowlist would expose every product in the Stripe
account for plan-switching.

Configuration created:
    - ``subscription_update``: enabled, ``default_allowed_updates: ["price"]``,
      ``proration_behavior: "create_prorations"``, ``products`` allowlisted to
      the Local Pro + Cloud product/price pairs (resolved from the price ids
      via ``stripe.Price.retrieve`` — Stripe's allowlist is keyed by product,
      not price).
    - ``subscription_cancel``: enabled, ``mode: "at_period_end"`` (no
      immediate-cancel option — matches the plan's cancel-at-period-end
      contract).
    - ``payment_method_update``: enabled.
    - ``invoice_history``: enabled.
    - A minimal ``business_profile`` (Stripe requires the portal to have some
      identifying copy).

Idempotency: lists existing configurations first. If any already exist, this
script WARNS with the existing configuration id(s) and does NOT create a
duplicate — pass ``--force`` to create a new one anyway (e.g. to intentionally
replace scoping after a product change).

Usage:
    # Test mode dry run — prints the payload, makes no Stripe calls, no key needed:
    python scripts/cloud-function/setup_portal_config.py \\
        --price-id-local price_test_local --price-id-cloud price_test_cloud --dry-run

    # Live run (price ids from env, matching billing.py's own env names):
    STRIPE_SECRET_KEY=sk_live_... \\
    STRIPE_PRICE_ID_LOCAL=price_... STRIPE_PRICE_ID_CLOUD=price_... \\
        python scripts/cloud-function/setup_portal_config.py

After it prints a configuration id (``bpc_...``), set it as
``STRIPE_PORTAL_CONFIGURATION_ID`` on the deployed ``stripe-portal-session``
Cloud Function (see billing.py's deploy docstring) and redeploy — the endpoint
passes it explicitly to ``Session.create`` (code-enforced scoping, never
dashboard state, KTD-2/U7).
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _stripe_key() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "")


def _price_ids(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve the two price ids from CLI args, falling back to billing.py's
    own env var names (``STRIPE_PRICE_ID_LOCAL`` / ``STRIPE_PRICE_ID_CLOUD``)."""
    local = args.price_id_local or os.environ.get("STRIPE_PRICE_ID_LOCAL", "")
    cloud = args.price_id_cloud or os.environ.get("STRIPE_PRICE_ID_CLOUD", "")
    if not local or not cloud:
        raise SystemExit(
            "Both price ids are required: pass --price-id-local/--price-id-cloud "
            "or set STRIPE_PRICE_ID_LOCAL/STRIPE_PRICE_ID_CLOUD."
        )
    if local == cloud:
        raise SystemExit("--price-id-local and --price-id-cloud must be distinct.")
    return local, cloud


def _resolve_product_id(price_id: str) -> str:
    """Look up the product id backing a price (real Stripe call — never made
    in --dry-run). The portal's ``subscription_update.products`` allowlist is
    keyed by product id, not price id, so this is required even though the
    caller only has price ids."""
    import stripe

    price = stripe.Price.retrieve(price_id)
    product = price["product"]
    # ``product`` may come back as a bare id (string) or an expanded object,
    # depending on API version/expand options — handle both.
    return product["id"] if isinstance(product, dict) else product


def build_configuration_payload(
    *, local_price_id: str, cloud_price_id: str, local_product_id: str, cloud_product_id: str
) -> dict:
    """Build the ``billing_portal.Configuration.create`` kwargs (U7 scoping)."""
    return {
        "business_profile": {
            "headline": "ScreenCap billing",
        },
        "features": {
            "subscription_update": {
                "enabled": True,
                "default_allowed_updates": ["price"],
                "proration_behavior": "create_prorations",
                "products": [
                    {"product": local_product_id, "prices": [local_price_id]},
                    {"product": cloud_product_id, "prices": [cloud_price_id]},
                ],
            },
            "subscription_cancel": {
                "enabled": True,
                "mode": "at_period_end",
            },
            "payment_method_update": {"enabled": True},
            "invoice_history": {"enabled": True},
        },
    }


def _existing_configuration_ids() -> list[str]:
    """List existing portal configurations (real Stripe call)."""
    import stripe

    result = stripe.billing_portal.Configuration.list(limit=100)
    return [item["id"] for item in result.get("data", [])]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--price-id-local",
        help="Local Pro Stripe price id (default: STRIPE_PRICE_ID_LOCAL env).",
    )
    parser.add_argument(
        "--price-id-cloud",
        help="Cloud Stripe price id (default: STRIPE_PRICE_ID_CLOUD env).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Create a new configuration even if one already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the configuration payload without calling Stripe. "
        "Does not require STRIPE_SECRET_KEY or resolve product ids.",
    )
    args = parser.parse_args(argv)

    local_price_id, cloud_price_id = _price_ids(args)

    if args.dry_run:
        payload = build_configuration_payload(
            local_price_id=local_price_id,
            cloud_price_id=cloud_price_id,
            # No Stripe call in --dry-run: product ids are shown as a
            # placeholder describing how they'd be resolved for real.
            local_product_id="<resolved via stripe.Price.retrieve, skipped in --dry-run>",
            cloud_product_id="<resolved via stripe.Price.retrieve, skipped in --dry-run>",
        )
        print("[dry-run] Would create billing_portal.Configuration with:")
        print(json.dumps(payload, indent=2))
        return 0

    if not _stripe_key():
        raise SystemExit(
            "STRIPE_SECRET_KEY is required (unset). Refusing to run without it."
        )

    import stripe

    stripe.api_key = _stripe_key()

    existing = _existing_configuration_ids()
    if existing and not args.force:
        print(
            "A portal configuration already exists: "
            f"{', '.join(existing)}. Not creating a duplicate — pass --force "
            "to create a new one anyway.",
            file=sys.stderr,
        )
        return 1
    if existing and args.force:
        print(
            f"Existing configuration(s) found ({', '.join(existing)}); "
            "--force set, creating a new one anyway.",
            file=sys.stderr,
        )

    local_product_id = _resolve_product_id(local_price_id)
    cloud_product_id = _resolve_product_id(cloud_price_id)

    payload = build_configuration_payload(
        local_price_id=local_price_id,
        cloud_price_id=cloud_price_id,
        local_product_id=local_product_id,
        cloud_product_id=cloud_product_id,
    )
    configuration = stripe.billing_portal.Configuration.create(**payload)
    config_id = configuration["id"]

    print(f"Created portal configuration: {config_id}")
    print(
        "Set this as STRIPE_PORTAL_CONFIGURATION_ID on the deployed "
        "stripe-portal-session Cloud Function and redeploy (see billing.py's "
        "deploy docstring)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
