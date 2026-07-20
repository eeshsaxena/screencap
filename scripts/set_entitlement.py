#!/usr/bin/env python3
"""Comp (or un-comp) an account's entitlement tier — Stripe-independent (U12 / U4).

Sets or clears the two-tier ``{tier, subscribed}`` Firebase custom claim DIRECTLY
via the Admin SDK, so internal / demo / press accounts get access WITHOUT going
through Stripe Checkout or the webhook. ``subscribed`` is always written as a pure
function of ``tier`` (``subscribed = (tier == "cloud")``, KTD-1), so a comp can
never drift into ``subscribed=true`` with a non-cloud tier. Run it for your own +
demo accounts BEFORE flipping ``STRIPE_PAYWALL_ENFORCE`` on, so the team is never
locked out mid-launch even if the Stripe wiring is not yet green.

Usage:
    # Grant cloud (default):
    python scripts/set_entitlement.py --uid <UID>
    python scripts/set_entitlement.py --email <EMAIL>
    # Grant Local Pro:
    python scripts/set_entitlement.py --email <EMAIL> --tier local
    # Revoke:
    python scripts/set_entitlement.py --email <EMAIL> --revoke
    # One-off grandfather backfill (legacy subscribed=true -> tier=cloud, KTD-6;
    # idempotent + re-runnable):
    python scripts/set_entitlement.py --backfill-grandfathered
    # Count-only preview of the backfill (writes NOTHING) — run this BEFORE the
    # real backfill to see how many accounts the irreversible bulk write touches:
    python scripts/set_entitlement.py --backfill-grandfathered --dry-run

Requires Firebase Admin credentials for the target project
(``GOOGLE_APPLICATION_CREDENTIALS`` or ADC). Never routes through Stripe.
"""

from __future__ import annotations

import argparse
import os
import sys

import firebase_admin
from firebase_admin import auth as fb_auth


def _ensure_app(project_id: str) -> None:
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": project_id})


def set_entitlement(
    identifier: str, *, is_email: bool, tier: str | None, project_id: str
) -> str:
    """Set/clear the two-tier entitlement claim for a uid or email; return uid.

    Writes ``{tier, subscribed = (tier == "cloud")}`` (KTD-1) so ``subscribed``
    is always a pure function of ``tier`` and can never drift. A ``tier`` of
    ``None`` is the revoke/clear state (``tier="none"``, ``subscribed=false``).
    Merges with any existing custom claims so unrelated claims are preserved.
    """
    _ensure_app(project_id)
    user = (
        fb_auth.get_user_by_email(identifier)
        if is_email
        else fb_auth.get_user(identifier)
    )
    resolved = tier or "none"
    claims = dict(user.custom_claims or {})
    claims["tier"] = resolved
    claims["subscribed"] = resolved == "cloud"
    fb_auth.set_custom_user_claims(user.uid, claims)
    return user.uid


def backfill_grandfathered(*, project_id: str, dry_run: bool = False) -> list[str]:
    """Migrate legacy ``subscribed=true`` accounts to ``tier=cloud`` (KTD-6).

    Idempotent + re-runnable: only accounts carrying ``subscribed=true`` but no
    ``tier`` are written (an already-migrated account carrying ``tier=cloud`` is
    skipped), so a partial run can be safely re-run. Preserves other claims.

    Iterates ALL users via ``iterate_all()`` (which pages through the whole
    project), not just the first ``list_users()`` page — otherwise both the
    write and any count derived from it would silently cap at ~1000 accounts.

    With ``dry_run=True`` it enumerates the accounts that WOULD be migrated and
    writes NOTHING: this is the count-preview to run before the irreversible
    bulk write. Returns the list of uids migrated (or, in a dry run, the uids
    that would be migrated).
    """
    _ensure_app(project_id)
    matched: list[str] = []
    for user in fb_auth.list_users().iterate_all():
        claims = dict(user.custom_claims or {})
        if claims.get("subscribed") and not claims.get("tier"):
            if not dry_run:
                claims["tier"] = "cloud"
                fb_auth.set_custom_user_claims(user.uid, claims)
            matched.append(user.uid)
    return matched


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Comp/un-comp an account's entitlement tier (Stripe-independent)."
    )
    # Target is required for the per-account grant/revoke path but not for the
    # one-off --backfill-grandfathered sweep (validated below).
    parser.add_argument("--uid", help="Firebase uid to comp.")
    parser.add_argument("--email", help="Account email to comp.")
    parser.add_argument(
        "--tier",
        choices=("local", "cloud"),
        default="cloud",
        help="Entitlement tier to grant (default: cloud). Ignored with --revoke.",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="Clear the entitlement (tier=none, subscribed=false).",
    )
    parser.add_argument(
        "--backfill-grandfathered",
        action="store_true",
        help="One-off: set tier=cloud for all legacy subscribed=true accounts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --backfill-grandfathered: count the accounts that WOULD be "
        "migrated and write nothing (the pre-write count preview).",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("SCREENCAP_PROJECT_ID", "proteus-photos"),
        help="Firebase project id (default: SCREENCAP_PROJECT_ID or proteus-photos).",
    )
    args = parser.parse_args(argv)

    if args.dry_run and not args.backfill_grandfathered:
        parser.error("--dry-run is only supported with --backfill-grandfathered")

    if args.backfill_grandfathered:
        matched = backfill_grandfathered(project_id=args.project, dry_run=args.dry_run)
        if args.dry_run:
            print(
                f"Grandfather backfill (dry-run): {len(matched)} account(s) WOULD "
                f"be migrated to tier=cloud in project {args.project}; no writes made"
            )
        else:
            print(f"Grandfather backfill: migrated {len(matched)} account(s) to tier=cloud")
        return 0

    if args.uid and args.email:
        parser.error("argument --email: not allowed with argument --uid")
    if not (args.uid or args.email):
        parser.error("one of the arguments --uid --email --backfill-grandfathered is required")

    tier = None if args.revoke else args.tier
    identifier = args.email or args.uid
    uid = set_entitlement(
        identifier,
        is_email=bool(args.email),
        tier=tier,
        project_id=args.project,
    )
    if args.revoke:
        print(f"Revoked entitlement (tier=none, subscribed=false) for uid={uid}")
    else:
        print(f"Granted tier={tier} (subscribed={tier == 'cloud'}) for uid={uid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
