#!/usr/bin/env python3
"""Comp (or un-comp) an account's cloud entitlement — Stripe-independent (U12 / R12).

Sets or clears the ``subscribed`` Firebase custom claim DIRECTLY via the Admin
SDK, so internal / demo / press accounts get cloud access WITHOUT going through
Stripe Checkout or the webhook. Run it for your own + demo accounts BEFORE
flipping ``STRIPE_PAYWALL_ENFORCE`` on, so the team is never locked out mid-launch
even if the Stripe wiring is not yet green. This is the launch-day seatbelt; an
external-comp coupon is a separate, later concern.

Usage:
    # Grant (default):
    python scripts/set_entitlement.py --uid <UID>
    python scripts/set_entitlement.py --email <EMAIL>
    # Revoke:
    python scripts/set_entitlement.py --email <EMAIL> --revoke

Requires Firebase Admin credentials for the target project
(``GOOGLE_APPLICATION_CREDENTIALS`` or ADC). Never routes through Stripe.
"""

from __future__ import annotations

import argparse
import os
import sys

import firebase_admin
from firebase_admin import auth as fb_auth


def set_entitlement(
    identifier: str, *, is_email: bool, subscribed: bool, project_id: str
) -> str:
    """Set/clear ``subscribed`` for a uid or email; return the resolved uid.

    Merges with any existing custom claims so unrelated claims are preserved.
    """
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": project_id})
    user = (
        fb_auth.get_user_by_email(identifier)
        if is_email
        else fb_auth.get_user(identifier)
    )
    claims = dict(user.custom_claims or {})
    claims["subscribed"] = bool(subscribed)
    fb_auth.set_custom_user_claims(user.uid, claims)
    return user.uid


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Comp/un-comp an account's cloud entitlement (Stripe-independent)."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--uid", help="Firebase uid to comp.")
    target.add_argument("--email", help="Account email to comp.")
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="Clear the subscription entitlement (default: grant).",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("SCREENCAP_PROJECT_ID", "proteus-photos"),
        help="Firebase project id (default: SCREENCAP_PROJECT_ID or proteus-photos).",
    )
    args = parser.parse_args(argv)

    identifier = args.email or args.uid
    uid = set_entitlement(
        identifier,
        is_email=bool(args.email),
        subscribed=not args.revoke,
        project_id=args.project,
    )
    action = "Revoked" if args.revoke else "Granted"
    print(f"{action} cloud entitlement (subscribed={not args.revoke}) for uid={uid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
