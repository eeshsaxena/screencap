#!/usr/bin/env python3
"""Admin: promote founder-cleared recordings from private ``import-review/`` into
the public ``demo/`` gallery (SCR-139 / plan U8).

Given a founder-approved allow-list (one recording name per line; ``#`` comments
and blank lines ignored), ``rewrite()`` only those recordings into ``demo/``,
stripping the superseded ``_unlisted``/``show_on_website`` markers so each appears
in the marker-blind ``demo-list``. Per-object ``crc32c`` + ``content_type`` verify;
idempotent; NO deletes.

The allow-list is the content/title review gate (the public-exposure consent
decision — see the plan's Open Questions and the runbook). A recording is public
the moment it lands in ``demo/``; only promote what the founder has cleared.

Usage::

    python scripts/promote_staging_to_demo.py --bucket screencap-recordings \\
        --allow-list cleared-recordings.txt --dry-run
"""

from __future__ import annotations

import argparse
import sys

from cloud_migration import core


def _parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    core.add_common_args(p)
    p.add_argument(
        "--allow-list",
        required=True,
        help="File of founder-cleared recording names (one per line)",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    allow = core.load_allow_list(args.allow_list)
    if not allow:
        print(
            f"ERROR: allow-list {args.allow_list!r} is empty — nothing to promote. "
            "Add the founder-cleared recording names (one per line).",
            file=sys.stderr,
        )
        return 2
    print(f"allow-list: {len(allow)} recording(s) cleared for public demo/")

    client = core.build_client(args.project)
    result = core.run_promote(
        client=client,
        bucket_name=args.bucket,
        allow_list=allow,
        dry_run=args.dry_run,
    )

    if result.invalid_names:
        print(
            "WARNING: skipped (name rejected by the demo handlers — would be "
            f"hidden-but-public): {', '.join(result.invalid_names)}",
            file=sys.stderr,
        )
    if result.missing_from_staging:
        print(
            "WARNING: allow-listed recordings absent from import-review/: "
            f"{', '.join(result.missing_from_staging)}",
            file=sys.stderr,
        )
    if not result.ok:
        print(
            f"ERROR: {len(result.failures)} object(s) failed verification — "
            "the demo copy is incomplete.",
            file=sys.stderr,
        )
        return 1
    # A typo'd or unstaged allow-list entry, or a name the demo handlers reject,
    # means the promotion is not what the operator intended — exit non-zero so a
    # pipeline / `set -e` run does not treat a partial promote as complete.
    if result.missing_from_staging or result.invalid_names:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
