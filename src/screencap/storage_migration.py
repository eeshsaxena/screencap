"""Same-volume recordings-library relocation engine (SCR-228).

Daemon-free core for moving the recordings directory to a new location on the
**same filesystem**. A same-volume move is an atomic, O(1) ``os.rename`` of the
directory tree regardless of size — so this module deliberately has no copy
engine, no progress stream, and needs no extra free space. Cross-volume /
external-drive moves (which would require copy-verify-then-delete) are out of
scope for v1 and rejected here up front (``Reason.CROSS_VOLUME``).

Split into pure functions so the data-movement logic is unit-testable without a
daemon:

- :func:`validate_target` — preflight checks, each returning a distinct reason
  code (the daemon maps these to typed API errors and the UI to copy).
- :func:`migrate` — the breadcrumbed atomic move: write intent → rename →
  ``chmod 0o700`` the new root → commit the config flip (injected) → clear the
  breadcrumb. The config flip is the single commit point.
- :func:`reconcile_pending` — daemon-start recovery from a leftover breadcrumb,
  so a crash between the rename and the config flip converges on exactly one
  intact location.

This module imports :mod:`screencap.config` (a leaf config module) but never
:mod:`screencap.daemon` — the daemon verb (SCR-228 U3) is the only consumer.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BREADCRUMB_NAME = "migration.intent"


class Reason:
    """Machine reason codes for a rejected or failed migration.

    Stable strings — the daemon returns them verbatim and the macOS UI maps
    them to human copy (SCR-228 U7). Do not rename without updating both.
    """

    CROSS_VOLUME = "cross_volume"
    NOT_WRITABLE = "not_writable"
    SAME_AS_SOURCE = "same_as_source"
    NESTED = "nested"
    TARGET_NOT_EMPTY = "target_not_empty"
    ENV_OVERRIDE = "env_override"
    CLOUD_SYNCED = "cloud_synced"
    SOURCE_MISSING = "source_missing"


# Default human-oriented messages. The UI owns final copy; these are the
# CLI/headless fallback and the daemon's default `message`.
MESSAGES = {
    Reason.CROSS_VOLUME: (
        "The chosen folder is on a different disk. Moving recordings to an "
        "external or separate volume isn't supported yet — pick a folder on "
        "the same disk."
    ),
    Reason.NOT_WRITABLE: "The chosen folder isn't writable.",
    Reason.SAME_AS_SOURCE: "That's already where recordings are stored.",
    Reason.NESTED: (
        "Pick a folder that isn't inside the current recordings folder (or a "
        "parent of it)."
    ),
    Reason.TARGET_NOT_EMPTY: "Choose an empty folder.",
    Reason.ENV_OVERRIDE: (
        "SCREENCAP_RECORDINGS_DIR is set, which overrides the stored location. "
        "Unset it before changing the storage location here."
    ),
    Reason.CLOUD_SYNCED: (
        "That folder is synced to iCloud/Dropbox/OneDrive/Google Drive, which "
        "would upload your recordings off this Mac. Choose a folder that isn't "
        "synced to the cloud."
    ),
    Reason.SOURCE_MISSING: "The current recordings folder could not be found.",
}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    code: str | None = None
    message: str | None = None

    @classmethod
    def valid(cls) -> "ValidationResult":
        return cls(ok=True)

    @classmethod
    def invalid(cls, code: str) -> "ValidationResult":
        return cls(ok=False, code=code, message=MESSAGES.get(code, code))


@dataclass(frozen=True)
class MigrationOutcome:
    ok: bool
    code: str | None = None
    message: str | None = None
    moved_from: str | None = None
    moved_to: str | None = None


def _run_dir(run_dir: Path | None) -> Path:
    if run_dir is not None:
        return run_dir
    from screencap import config

    return config._DEFAULT_BASE / "run"


def _breadcrumb_path(run_dir: Path | None) -> Path:
    return _run_dir(run_dir) / BREADCRUMB_NAME


def _nearest_existing(p: Path) -> Path:
    """Return ``p`` if it exists, else its nearest existing ancestor.

    Used for the volume / writability probes when the target does not exist
    yet (the common case — the user picks a folder we will create).
    """
    cur = p
    while not cur.exists():
        parent = cur.parent
        if parent == cur:  # reached filesystem root without finding one
            return cur
        cur = parent
    return cur


def _st_dev(path: Path) -> int:
    """Filesystem device id for ``path`` (indirection for test injection)."""
    return os.stat(path).st_dev


def _is_cloud_sync_path(target: Path) -> bool:
    """Return True if ``target`` is under a known cloud-sync root other than
    the iCloud locations handled by ``check_icloud_sync``.

    Covers the macOS File Provider roots (Dropbox, OneDrive, Google Drive, Box
    all mount under ``~/Library/CloudStorage``) plus the classic home-dir
    folders. Path-existence/prefix checks only, matching ``check_icloud_sync``.
    """
    home = Path.home()
    try:
        resolved = target.resolve()
    except OSError:
        return False
    roots = [
        home / "Library" / "CloudStorage",
        home / "Dropbox",
        home / "OneDrive",
        home / "Google Drive",
    ]
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _is_cloud_synced(target: Path) -> bool:
    from screencap.network.ca_lifecycle import check_icloud_sync

    return check_icloud_sync(target) or _is_cloud_sync_path(target)


def validate_target(source: Path, target: Path) -> ValidationResult:
    """Preflight the same-volume move. Returns the first failing reason.

    Order is chosen so the clearest rejection wins: env override (would mask
    the change) → source present → identity → nesting → cloud-sync
    (exfiltration) → same-volume → writable → empty.
    """
    if os.environ.get("SCREENCAP_RECORDINGS_DIR"):
        return ValidationResult.invalid(Reason.ENV_OVERRIDE)

    source = source.resolve()
    if not source.exists() or not source.is_dir():
        return ValidationResult.invalid(Reason.SOURCE_MISSING)

    # target may not exist yet; resolve without requiring existence.
    target = Path(os.path.realpath(target))

    if target == source:
        return ValidationResult.invalid(Reason.SAME_AS_SOURCE)

    # Nesting either direction: target inside source, or source inside target.
    if _is_relative_to(target, source) or _is_relative_to(source, target):
        return ValidationResult.invalid(Reason.NESTED)

    if _is_cloud_synced(target):
        return ValidationResult.invalid(Reason.CLOUD_SYNCED)

    anchor = _nearest_existing(target)
    try:
        same_volume = _st_dev(source) == _st_dev(anchor)
    except OSError:
        return ValidationResult.invalid(Reason.NOT_WRITABLE)
    if not same_volume:
        return ValidationResult.invalid(Reason.CROSS_VOLUME)

    if not os.access(anchor, os.W_OK):
        return ValidationResult.invalid(Reason.NOT_WRITABLE)

    # Target must be empty or not-yet-existing (KTD-4). A bare empty dir is
    # acceptable — a stray get_recordings_dir() read may have mkdir'd it.
    if target.exists():
        if not target.is_dir():
            return ValidationResult.invalid(Reason.NOT_WRITABLE)
        if any(target.iterdir()):
            return ValidationResult.invalid(Reason.TARGET_NOT_EMPTY)

    return ValidationResult.valid()


def _is_relative_to(a: Path, b: Path) -> bool:
    try:
        a.relative_to(b)
        return True
    except ValueError:
        return False


def migrate(
    source: Path,
    target: Path,
    commit_config: Callable[[Path], None],
    *,
    run_dir: Path | None = None,
) -> MigrationOutcome:
    """Perform the breadcrumbed atomic same-volume move.

    Caller must have validated ``target`` first. Sequence (KTD-3):

    1. Write ``run/migration.intent`` = ``{"from": ..., "to": ...}``.
    2. ``rmdir`` the target if it exists as an empty dir, then
       ``os.rename(source, target)`` (atomic, O(1)).
    3. ``chmod 0o700`` the new root — the load-bearing cross-user gate; a
       0o700 root blocks directory traversal regardless of per-file mode, so
       the move stays O(1) (no per-file walk).
    4. ``commit_config(target)`` — the single commit point (the daemon passes
       ``config.set_recordings_dir``, which flips config and invalidates the
       in-process cache).
    5. Delete the breadcrumb.

    A crash before step 4 leaves the breadcrumb for :func:`reconcile_pending`.
    """
    source = source.resolve()
    target = Path(os.path.realpath(target))

    crumb = _breadcrumb_path(run_dir)
    crumb.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"from": str(source), "to": str(target)})
    _atomic_write(crumb, payload)
    _fsync_dir(crumb.parent)  # durability: breadcrumb must survive power-loss

    # validate_target only probed the nearest EXISTING ancestor; a headless CLI
    # caller may pass a nested path whose parent doesn't exist yet. Create it so
    # os.rename doesn't fail with ENOENT after the breadcrumb is written.
    target.parent.mkdir(parents=True, exist_ok=True)

    # A pre-existing empty target must be removed so os.rename lands cleanly (a
    # stray get_recordings_dir() read may have mkdir'd it). A racing writer that
    # filled it since validation makes rmdir raise ENOTEMPTY — surface a clean
    # typed outcome, not a raw 500, with source untouched.
    if target.exists():
        try:
            target.rmdir()
        except OSError:
            _unlink_quiet(crumb)
            return MigrationOutcome(
                ok=False,
                code=Reason.TARGET_NOT_EMPTY,
                message=MESSAGES[Reason.TARGET_NOT_EMPTY],
            )

    os.rename(source, target)

    # The tree now lives at `target`. Everything below must either fully commit
    # (the config flip) or roll the rename back — a failure must never leave
    # config pointing at the vanished source (a split-brain the live daemon
    # can't recover from until restart). The same-volume rename-back is atomic
    # and O(1), so rollback is safe.
    try:
        _harden_root(target)  # chmod 0o700 + verify; raises if it didn't land
        commit_config(target)  # the single commit point
    except Exception:
        os.rename(target, source)  # roll back to the pre-move state
        _unlink_quiet(crumb)
        raise

    _unlink_quiet(crumb)
    return MigrationOutcome(
        ok=True, moved_from=str(source), moved_to=str(target)
    )


def reconcile_pending(
    commit_config: Callable[[Path], None],
    *,
    run_dir: Path | None = None,
) -> MigrationOutcome | None:
    """Recover from a leftover breadcrumb at daemon start (KTD-3).

    Returns None when there is nothing to reconcile. Idempotent — safe to run
    on every start.
    """
    crumb = _breadcrumb_path(run_dir)
    if not crumb.exists():
        return None

    try:
        data = json.loads(crumb.read_text())
        from_p = Path(data["from"])
        to_p = Path(data["to"])
    except (OSError, ValueError, KeyError):
        _unlink_quiet(crumb)
        return MigrationOutcome(ok=False, code="corrupt_breadcrumb")

    to_exists = to_p.exists()
    from_exists = from_p.exists()

    # Converge on whichever location actually holds the library. The rename
    # either happened (data at `to`) or didn't (data at `from`). When BOTH
    # exist — e.g. a crash between the rename and the config flip left `to`
    # populated, and a later get_recordings_dir() mkdir recreated an empty
    # `from` — pick the non-empty one rather than blindly keeping `from`, which
    # would strand the moved library permanently.
    converge: Path | None = None
    if to_exists and not from_exists:
        converge = to_p
    elif _nonempty_dir(to_p) and not _nonempty_dir(from_p):
        converge = to_p

    if converge is not None:
        # Re-apply the 0o700 gate: a crash between rename and chmod leaves the
        # tree at the source's original mode. Best-effort in recovery — a chmod
        # failure must never block daemon start.
        with contextlib.suppress(OSError):
            os.chmod(converge, 0o700)
        commit_config(converge)
        _unlink_quiet(crumb)
        return MigrationOutcome(
            ok=True, moved_from=str(from_p), moved_to=str(converge)
        )

    # Rename never happened (data intact at `from`) — leave config at the
    # source and clear.
    _unlink_quiet(crumb)
    return MigrationOutcome(
        ok=True, moved_from=str(from_p), moved_to=str(from_p)
    )


def _harden_root(root: Path) -> None:
    """``chmod`` the new recordings root to ``0o700`` and verify it landed.

    A ``0o700`` root is the load-bearing cross-user gate — it blocks directory
    traversal regardless of per-file mode, which is what protects a library
    relocated outside ``~`` to a world-traversable parent. A silently-failed or
    ineffective chmod there would leave the tree exposed while the caller
    reports success, so this raises on failure (the caller rolls the move back).
    """
    os.chmod(root, 0o700)
    mode = os.stat(root).st_mode & 0o777
    if mode & 0o077:
        raise OSError(
            f"could not harden {root} to 0o700 (mode is {oct(mode)})"
        )


def _nonempty_dir(p: Path) -> bool:
    """True if ``p`` is a directory containing at least one entry."""
    try:
        return p.is_dir() and any(p.iterdir())
    except OSError:
        return False


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory entry (breadcrumb durability)."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    fd = os.open(
        str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    try:
        os.write(fd, text.encode())
        os.fsync(fd)  # durable before the rename it guards (KTD-3)
    finally:
        os.close(fd)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


__all__ = [
    "Reason",
    "MESSAGES",
    "ValidationResult",
    "MigrationOutcome",
    "validate_target",
    "migrate",
    "reconcile_pending",
]
