#!/usr/bin/env python3
"""Admin: decommission the flat namespace after the website cutover is verified
live (SCR-139 / plan U9). **This is the irreversible step.**

Run ONLY after U7 (``screencap-website`` repoint) renders and plays the ``demo/``
set correctly in production and the founder confirms promotion is complete.

Each ``recordings/`` delete is gated on a FRESH live re-verify of its
``import-review/`` staging copy (``crc32c`` + ``content_type``) — a source whose
staging copy is missing or mismatched is KEPT, never deleted, so a crash mid-run
leaves the remainder intact and the run is safely re-runnable. ``sessions/`` is
retired data with no staging copy, so it is deleted only behind the explicit
``--include-sessions`` opt-in. A post-run re-scan asserts the prefixes are empty
(R11) and surfaces any new flat blob that raced past U8's quiesce instead of
deleting it.

A second quiesce guard runs first (``--confirm-quiesced`` to attest on an admin
box without ``screencap``). Always ``--dry-run`` and confirm the empty-prefix
re-scan before the live run. See ``docs/runbooks/cloud-migration-runbook.md``.

Usage::

    python scripts/decommission_flat_namespace.py --bucket screencap-recordings --dry-run
    python scripts/decommission_flat_namespace.py --bucket screencap-recordings \\
        --include-sessions --confirm
"""

from __future__ import annotations

import argparse
import sys

from cloud_migration import core


def _parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    core.add_common_args(p)
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Required for a live (non-dry-run) deletion — the irreversible step",
    )
    p.add_argument(
        "--include-sessions",
        action="store_true",
        help="Also delete retired sessions/ blobs (no staging copy exists to gate on)",
    )
    p.add_argument(
        "--confirm-quiesced",
        action="store_true",
        help="Attest the flat write path is closed when status is unavailable",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # The live deletion is irreversible: require an explicit --confirm so a bare
    # invocation can never delete. --dry-run never needs it.
    if not args.dry_run and not args.confirm:
        print(
            "ERROR: refusing to delete without --confirm. Re-run with --dry-run "
            "to preview, or add --confirm for the live (irreversible) deletion.",
            file=sys.stderr,
        )
        return 2

    client = core.build_client(args.project)
    try:
        result = core.run_decommission(
            client=client,
            bucket_name=args.bucket,
            include_sessions=args.include_sessions,
            dry_run=args.dry_run,
            confirm_quiesced=args.confirm_quiesced,
        )
    except core.MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    kept = result.kept
    if kept:
        print(
            f"NOTE: {len(kept)} object(s) KEPT (no verified staging copy) — the "
            "gate held; re-stage them before they can be decommissioned.",
            file=sys.stderr,
        )

    rescan = result.rescan
    if rescan is not None:
        if rescan.new_blobs:
            print(
                f"WARNING: {len(rescan.new_blobs)} NEW flat blob(s) appeared since "
                "enumeration (missed-write race) — investigate, do not assume R11.",
                file=sys.stderr,
            )
            return 1
        if not rescan.empty:
            print(
                f"NOTE: {len(rescan.remaining)} object(s) still under the flat "
                "prefixes; R11 is satisfied only once they are empty.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
