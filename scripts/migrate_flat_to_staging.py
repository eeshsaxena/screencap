#!/usr/bin/env python3
"""Admin one-shot: stage the flat ``recordings/`` namespace into private
``import-review/`` (SCR-139 / plan U8). NO deletes — the flat namespace stays the
rollback source of truth until ``decommission_flat_namespace.py`` (U9).

Runs three gates before copying, in order:

1. **Bucket-IAM pre-check** — refuses if the bucket has any
   ``allUsers``/``allAuthenticatedUsers`` read binding (``--remove-public-iam`` to
   strip them). "Private staging" is a fiction otherwise.
2. **Quiesce guard** — refuses while a screencap recording is in flight on this
   machine (``--confirm-quiesced`` to attest when ``screencap status`` is
   unavailable on an admin box).
3. Per-object ``crc32c`` + ``content_type`` verified ``rewrite()`` into
   ``import-review/{name}/…`` (full nested suffix preserved), idempotent on re-run.

``sessions/`` is EXCLUDED by design. Always ``--dry-run`` first. See
``docs/runbooks/cloud-migration-runbook.md``.

Usage::

    python scripts/migrate_flat_to_staging.py --bucket screencap-recordings --dry-run
    python scripts/migrate_flat_to_staging.py --bucket screencap-recordings --remove-public-iam
"""

from __future__ import annotations

import argparse
import sys

from cloud_migration import core


def _parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    core.add_common_args(p)
    p.add_argument(
        "--remove-public-iam",
        action="store_true",
        help="Strip allUsers/allAuthenticatedUsers bindings before staging",
    )
    p.add_argument(
        "--confirm-quiesced",
        action="store_true",
        help="Attest the flat write path is closed when status is unavailable",
    )
    p.add_argument(
        "--manifest",
        default="cloud-migration-staging-manifest.json",
        help="Where to write the per-object staging manifest",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    client = core.build_client(args.project)
    try:
        result = core.run_stage(
            client=client,
            bucket_name=args.bucket,
            dry_run=args.dry_run,
            remove_public_iam=args.remove_public_iam,
            confirm_quiesced=args.confirm_quiesced,
        )
    except core.MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Write the manifest; on a dry-run, to a sidecar path so it can't clobber a
    # real one. U9 re-verifies live regardless — the manifest is provenance.
    manifest_path = args.manifest + (".dryrun.json" if args.dry_run else "")
    core.write_manifest(manifest_path, result.manifest)

    if not result.ok:
        print(
            f"ERROR: {len(result.failures)} object(s) failed verification — "
            "staging is incomplete; do NOT promote or decommission.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
