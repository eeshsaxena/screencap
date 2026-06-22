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

Every run writes a durable per-delete audit log (``--audit-log``, default
``cloud-migration-decommission-audit.jsonl``): one JSON object per delete/keep,
flushed per line, mode ``0o600``. It is the crash-safe record of this irreversible
step — live runs APPEND to it; ``--dry-run`` writes a ``.dryrun.jsonl`` preview.

Usage::

    python scripts/decommission_flat_namespace.py --bucket screencap-recordings --dry-run
    python scripts/decommission_flat_namespace.py --bucket screencap-recordings \\
        --include-sessions --confirm
    python scripts/decommission_flat_namespace.py --bucket screencap-recordings \\
        --confirm --audit-log /var/log/screencap/decommission.jsonl
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
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
        "--sessions-backup-confirmed",
        action="store_true",
        help=(
            "Required alongside --include-sessions: attest the zkairdrop session "
            "archive is accessible. sessions/ has no staging copy, so this is the "
            "only backup gate before the irreversible session delete."
        ),
    )
    p.add_argument(
        "--confirm-quiesced",
        action="store_true",
        help="Attest the flat write path is closed when status is unavailable",
    )
    p.add_argument(
        "--audit-log",
        default="cloud-migration-decommission-audit.jsonl",
        help=(
            "Path for the durable per-delete audit log: one JSON object per line "
            "(src/action/reason), flushed per line, mode 0o600. This is the irreversible "
            "step's crash-safe record. Live runs APPEND (a re-run never destroys the "
            "prior record); --dry-run writes a '.dryrun.jsonl' preview instead."
        ),
    )
    return p.parse_args(argv)


def _open_audit_log(path: str, *, dry_run: bool):
    """Open the decommission audit log at mode 0o600 (recording object names are the
    same sensitivity class as the recordings — they can leak that something was
    recorded). Live runs append so a re-run after a partial failure extends rather
    than truncates the prior deletion record; the dry-run sidecar is truncated."""
    # Co-locate the truncate/append flag with the matching fdopen mode so the two
    # never drift out of sync. O_NOFOLLOW refuses a symlinked target (mirrors the
    # other hardened sinks: daemon/audit_log.py, cli/_autospawn.py, content_index.py)
    # — the resulting ELOOP surfaces cleanly through main()'s `except OSError`.
    extra_flag, mode = (os.O_TRUNC, "w") if dry_run else (os.O_APPEND, "a")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | extra_flag
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)  # enforce 0o600 even if the file pre-existed with looser perms
    return os.fdopen(fd, mode, encoding="utf-8")


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

    # Open the durable audit log BEFORE the run and append one JSON line per outcome
    # AS the loop runs (flushed per line), so a crash mid-run still leaves an
    # auditable record of exactly what was deleted — this is the irreversible step.
    audit_path = args.audit_log + (".dryrun.jsonl" if args.dry_run else "")
    try:
        audit_fp = _open_audit_log(audit_path, dry_run=args.dry_run)
    except OSError as exc:
        print(f"ERROR: cannot open audit log {audit_path!r}: {exc}", file=sys.stderr)
        return 2

    def _record(outcome: core.DeleteOutcome) -> None:
        audit_fp.write(json.dumps(dataclasses.asdict(outcome), sort_keys=True) + "\n")
        audit_fp.flush()

    try:
        client = core.build_client(args.project)
        result = core.run_decommission(
            client=client,
            bucket_name=args.bucket,
            include_sessions=args.include_sessions,
            dry_run=args.dry_run,
            sessions_backup_confirmed=args.sessions_backup_confirmed,
            confirm_quiesced=args.confirm_quiesced,
            on_outcome=_record,
        )
    except core.MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # An OSError here means the audit sink itself failed mid-run (e.g. a full
        # disk during `_record`'s write/flush). This is the irreversible step: a
        # delete may have already committed, so its audit line could be lost. STOP
        # rather than keep doing unrecorded irreversible deletes — never crash with a
        # raw traceback, never overstate what was deleted. Exit 1 (the
        # incomplete/investigate bucket, consistent with kept-on-error).
        print(
            f"ERROR: audit log write failed ({exc}); aborting — the last deleted "
            f"object may be unlogged. Inspect {audit_path} before re-running.",
            file=sys.stderr,
        )
        return 1
    finally:
        audit_fp.close()

    print(f"decommission audit log (mode 0o600): {audit_path}", file=sys.stderr)

    kept = result.kept
    if kept:
        print(
            f"NOTE: {len(kept)} object(s) KEPT (no verified staging copy) — the "
            "gate held; re-stage them before they can be decommissioned.",
            file=sys.stderr,
        )

    # A source KEPT because a GCS error interrupted its delete means the run is
    # partial — exit non-zero so a pipeline / `set -e` run does not treat an
    # incomplete decommission as clean.
    kept_on_error = result.kept_on_error
    if kept_on_error:
        print(
            f"ERROR: {len(kept_on_error)} object(s) KEPT due to a GCS error during "
            "delete — the decommission is incomplete; re-run after investigating.",
            file=sys.stderr,
        )
        return 1

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
