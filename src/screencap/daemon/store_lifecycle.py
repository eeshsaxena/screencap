"""Sealed-capable store-state resolution for the daemon serve model (SCR-258 U4).

This module owns the **daemon-side lifecycle** that ``container.py`` deliberately
does not (its docstring defers ``ensure_store_mounted`` and store *state* to
"U4"). It is the KTD-14 amendment in code: instead of the base plan's
mount-before-bind, "never serve against an unmounted store" model — which makes a
sealed store indistinguishable from a dead daemon (launchd retry loops, R6
data-loss look) — the daemon binds its socket FIRST (``server.serve``) and then
calls :func:`resolve_store_state` to classify the store into a HEALTHY serving
state:

* :attr:`StoreState.MOUNTED`   — the store is available (an encrypted container
  attached at the recordings mountpoint, OR the container is disabled and today's
  plaintext directory is the store).
* :attr:`StoreState.LOCKED`    — a sealed sentinel is present; the daemon serves
  without ever attempting a mount (the whole point of bind-before-mount).
* :attr:`StoreState.ABSENT`    — the container is enabled but no bundle exists yet
  (the SMAppService-starts-daemon-before-onboarding window); ``storage init`` is
  the next step.
* :attr:`StoreState.ERROR`     — the store exists but cannot be served: the key is
  genuinely missing, the running binary is not entitled to read the key
  (KTD-22), the Keychain is locked (retryable), or the container was disabled on a
  bundle-present install (KTD-19 "downgrade unsupported"). Never a plaintext
  fallback.

Only a ROGUE mountpoint or a corrupted bundle remain operator hard-stops
(:func:`resolve_store_state` re-raises the container operator exception so
``serve`` can exit 1); every other condition is a healthy serving state.

Nothing here runs ``hdiutil`` unless the store is genuinely healthy (container
enabled, not sealed, bundle present, key readable, mountpoint not already a live
volume) — the sealed / absent / key-error / disabled paths short-circuit before
any subprocess, which is what keeps the model testable without real disk images.
"""

from __future__ import annotations

import fcntl
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Env var the daemon sets on the engine subprocess so its disk policy evaluates
# the HOST volume backing the bundle rather than the mounted volume's virtual
# (declared, sparse) free space (KTD-13). Consumed by
# ``screencap.engine.disk_policy``.
DISK_HOST_PATH_ENV = "SCREENCAP_DISK_HOST_PATH"

# Run-dir sentinel written by the lock verb (U9) to seal the store. U4 only
# READS it — a sealed sentinel present means the daemon serves ``locked`` and
# never attempts a mount. Writing/clearing it is U9's job.
_SEALED_SENTINEL_NAME = "store.sealed"
_MOUNT_LOCK_NAME = "mount.lock"


class StoreState(str, Enum):
    """The four healthy serving states surfaced on ``daemon.info`` (KTD-14).

    ``str`` mixin so ``.value`` serializes directly into the JSON envelope and an
    equality check against the wire string works without unwrapping (mirrors
    :class:`screencap.content_index.IndexState`).
    """

    MOUNTED = "mounted"
    LOCKED = "locked"
    ABSENT = "absent"
    ERROR = "error"


# ``store_state=error`` reason codes carried alongside the enum on ``daemon.info``
# so an operator (and the Swift app's ``error`` case) get an accurate cause rather
# than a bare "error". Never a plaintext fallback for any of these.
ERROR_KEY_MISSING = "key_missing"
ERROR_ENTITLEMENT_MISMATCH = "entitlement_mismatch"
ERROR_KEYCHAIN_LOCKED = "keychain_locked"
ERROR_DOWNGRADE_UNSUPPORTED = "downgrade_unsupported"


@dataclass(frozen=True)
class StoreResolution:
    """The resolved store state plus a diagnostic reason and (if mounted) path.

    ``reason`` is ``None`` for MOUNTED/LOCKED/ABSENT and one of the ``ERROR_*``
    codes for ERROR. ``mountpoint`` is set only when MOUNTED (so the daemon can
    thread the host-volume path to the engine).
    """

    state: StoreState
    reason: str | None = None
    mountpoint: str | None = None

    @property
    def is_mounted(self) -> bool:
        return self.state is StoreState.MOUNTED


# ---------------------------------------------------------------------------
# Path helpers (pure — no subprocess, no side effects beyond mkdir of run/)
# ---------------------------------------------------------------------------


def store_bundle_path() -> Path:
    """Absolute path of the encrypted sparse bundle (KTD-1).

    ``~/.screencap/store.sparsebundle`` by default — it lives in the plaintext
    run/base dir OUTSIDE the container (it *is* the container), resolved through
    ``config.get_store_bundle_dir`` (which defaults to ``config.get_base_dir``)
    so tests isolate it AND a vault install that relocated its storage location
    (SCR-258 U11, KTD-19) resolves the bundle from the chosen volume while the
    recordings mountpoint stays put.
    """
    from screencap import config, container

    return config.get_store_bundle_dir() / container.BUNDLE_NAME


def sealed_sentinel_path() -> Path:
    """Run-dir sealed-sentinel path (written by U9's lock verb; read here)."""
    from screencap import config

    return config.get_base_dir() / "run" / _SEALED_SENTINEL_NAME


def mount_lock_path() -> Path:
    """Run-dir ``mount.lock`` path serializing store-state resolution (KTD-3)."""
    from screencap import config

    return config.get_base_dir() / "run" / _MOUNT_LOCK_NAME


def is_sealed() -> bool:
    """True iff the sealed sentinel is present (store locked by the user)."""
    return sealed_sentinel_path().exists()


# ---------------------------------------------------------------------------
# Sealed-sentinel write / clear (U9 lock/unlock; the SHARED home per the U5 seam
# note — the CLI-local seal/unseal fallback imports these rather than duplicating
# the O_NOFOLLOW discipline).
# ---------------------------------------------------------------------------


def write_sealed_sentinel() -> Path:
    """Write the sealed sentinel with ``O_NOFOLLOW`` + realpath-parent discipline.

    Mirrors :func:`mount_lock` / ``_autospawn._open_auto_log``: a pre-planted
    symlink AT the sentinel path is refused (``O_NOFOLLOW`` → ``ELOOP``), not
    followed, and a symlinked run-dir is rejected. Mode ``0o600``. Returns the
    sentinel path. The U9 lock verb writes this LAST (after a successful detach);
    the CLI-local fallback (no daemon) writes it directly.
    """
    sentinel = sealed_sentinel_path()
    parent = sentinel.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise RuntimeError(
            f"sealed-sentinel run-dir is a symlink and will not be followed: {parent}"
        )
    fd = os.open(
        str(sentinel),
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, b"sealed\n")
    finally:
        os.close(fd)
    try:
        os.chmod(str(sentinel), 0o600)  # tighten if it pre-existed looser
    except OSError:
        pass
    return sentinel


def clear_sealed_sentinel() -> None:
    """Remove the sealed sentinel (unseal).

    ``unlink`` removes the link itself, never a symlink's target, so it is safe
    against a swapped-in symlink. Idempotent — a missing sentinel is a no-op.
    """
    try:
        os.unlink(str(sealed_sentinel_path()))
    except FileNotFoundError:
        pass


def _bundle_exists() -> bool:
    return store_bundle_path().exists()


def _migration_holds_mountpoint() -> bool:
    """True iff an upgrade migration is mid-flight with plaintext still at the
    recordings mountpoint (FIX 3, SCR-258 U6).

    Consulted by :func:`resolve_store_state` BEFORE the rogue-mountpoint hard stop
    so a daemon restart mid-``COPYING`` (plaintext legitimately occupying the real
    mountpoint) serves + auto-resumes instead of exiting 1. Cheap and side-effect-
    free: it does NOT create a ledger DB when none exists, and never raises.
    """
    try:
        from screencap import migration

        if not migration.default_ledger_path().exists():
            return False
        ledger = migration.MigrationLedger()
        return migration.migration_holds_plaintext_mountpoint(ledger)
    except Exception:  # noqa: BLE001 — a ledger read must never break resolve/serve
        logger.debug("migration-in-progress probe failed", exc_info=True)
        return False


def host_disk_path() -> Path | None:
    """The HOST volume path whose free space bounds recording (KTD-13).

    When the container is active, recordings land inside the mounted volume whose
    ``shutil.disk_usage`` reports the *declared* (virtual, sparse) size — so the
    disk guards must instead watch the bundle's backing host volume. Returns the
    bundle's parent directory (a real host path); ``None`` when the container is
    inactive (today's behavior — the capture dir already sits on the host volume).
    """
    from screencap import config

    if not config.get_container_enabled():
        return None
    return store_bundle_path().parent


def disk_host_env() -> dict[str, str]:
    """The engine-subprocess env fragment carrying the host-volume path (KTD-13).

    Empty when the container is inactive, so the daemon->engine pass-through adds
    nothing on a plaintext install (byte-identical to today).
    """
    host = host_disk_path()
    return {DISK_HOST_PATH_ENV: str(host)} if host is not None else {}


# ---------------------------------------------------------------------------
# mount.lock (KTD-3): O_NOFOLLOW + realpath-parent discipline, fcntl.flock
# ---------------------------------------------------------------------------


@contextmanager
def mount_lock() -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``run/mount.lock`` for the resolution.

    Uses the ``_autospawn._open_auto_log`` discipline (``O_NOFOLLOW`` on the lock
    path itself) rather than the check-then-connect shape, so a pre-planted
    symlink AT the lock path is refused, not followed (KTD-3). The run-dir itself
    is additionally rejected if it was swapped for a symlink (the tamper vector) —
    checked on the final component only, so a legitimately symlinked *ancestor*
    (macOS ``/var`` -> ``/private/var``, a test's ``/tmp`` link) does not
    false-positive the way a ``realpath == abspath`` whole-path check would.
    """
    lock_path = mount_lock_path()
    parent = lock_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise RuntimeError(
            f"mount.lock run-dir is a symlink and will not be followed: {parent}"
        )
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# The healthy-path mount attempt (only reached when everything checks out)
# ---------------------------------------------------------------------------


def _attempt_mount(bundle: Path, mountpoint: Path, key: bytes) -> StoreResolution:
    """Reuse an existing healthy mount, hard-stop on ROGUE, else attach (KTD-3).

    Ordered so the common/safe branches never invoke ``hdiutil``:

    1. mountpoint is already a mounted volume -> reuse it (no attach).
    2. mountpoint exists, is a non-empty plain directory (NOT a volume) -> ROGUE
       hard stop (mirrors ``RogueFileAtSocketPath``); never auto-cleaned.
    3. otherwise attach the bundle and harden the mount.

    Raises the container operator exceptions (``RogueMountpointError`` /
    ``ContainerCorruptError`` / ``ContainerAuthError``) unchanged so ``serve`` can
    map them to exit 1.
    """
    from screencap import container

    if os.path.ismount(str(mountpoint)):
        # A live volume is already here (a prior daemon left it mounted — the
        # mount outlives the daemon, base KTD-4). Reuse it.
        return StoreResolution(StoreState.MOUNTED, mountpoint=str(mountpoint))

    if mountpoint.exists() and mountpoint.is_dir() and any(mountpoint.iterdir()):
        raise container.RogueMountpointError(
            f"{mountpoint} is a non-empty directory but not a mounted volume; "
            "refusing to attach over it (rogue mountpoint, never auto-cleaned)"
        )

    mountpoint.mkdir(parents=True, exist_ok=True)
    info = container.attach(str(bundle), key, str(mountpoint))
    resolved_mp = info.mountpoint or str(mountpoint)
    container.harden_mount(resolved_mp)
    return StoreResolution(StoreState.MOUNTED, mountpoint=resolved_mp)


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def resolve_store_state(*, attempt_mount: bool = True) -> StoreResolution:
    """Classify the store into a healthy serving state (KTD-14, KTD-19, KTD-22).

    Called by ``server.serve`` AFTER the socket is bound. Only ROGUE / corrupted
    bundle raise (the caller exits 1); every other condition returns a
    :class:`StoreResolution`.

    ``attempt_mount=False`` resolves state without ever running ``hdiutil`` (the
    healthy path returns MOUNTED without attaching) — used where a caller only
    needs the classification, not a live mount.
    """
    from screencap import config, container

    # container OFF: the store is today's plaintext dir. No mount.lock, no
    # subprocess, no exit-code change — byte-identical to today (KTD-19 regression)
    # UNLESS a bundle already exists, in which case honoring the off-switch would
    # be a silent plaintext downgrade (R10) — refuse instead (KTD-19).
    if not config.get_container_enabled():
        if _bundle_exists():
            logger.warning(
                "container_enabled is false but an encrypted store bundle exists; "
                "refusing to serve plaintext (downgrade unsupported, KTD-19)"
            )
            return StoreResolution(
                StoreState.ERROR, reason=ERROR_DOWNGRADE_UNSUPPORTED
            )
        return StoreResolution(StoreState.MOUNTED)

    # container ON: resolve under mount.lock so a concurrent resolver (or the U9
    # lock verb) never races the sealed-sentinel / attach decision.
    with mount_lock():
        # Sealed sentinel wins over everything: serve locked, NEVER attempt a
        # mount. This is the load-bearing bind-before-mount amendment.
        if is_sealed():
            return StoreResolution(StoreState.LOCKED)

        bundle = store_bundle_path()
        if not bundle.exists():
            # Fresh install / pre-init (bundle absent, key may or may not exist).
            # ``storage init`` creates/relinks the bundle.
            return StoreResolution(StoreState.ABSENT)

        # Bundle exists — resolve the key with the KTD-22 cause-distinguishing
        # diagnosis. NONE of these touch the bundle bytes.
        try:
            key = container.require_container_key()
        except container.ContainerKeyMissingError:
            return StoreResolution(StoreState.ERROR, reason=ERROR_KEY_MISSING)
        except container.ContainerKeyUnreachableError:
            return StoreResolution(
                StoreState.ERROR, reason=ERROR_ENTITLEMENT_MISMATCH
            )
        except container.KeychainLockedError:
            # Retryable in-band (NOT a daemon exit): the Keychain may unlock and a
            # later mount attempt succeed.
            return StoreResolution(StoreState.ERROR, reason=ERROR_KEYCHAIN_LOCKED)

        if not attempt_mount:
            return StoreResolution(StoreState.MOUNTED)

        mountpoint = config.get_recordings_dir()
        # FIX 3 (availability): if an upgrade migration is mid-flight, the
        # plaintext library legitimately still occupies the recordings mountpoint
        # (phase COPYING / CUTTING_OVER — the container is at the interim mount).
        # That non-empty mountpoint is NOT a rogue mountpoint; attaching the
        # container over it would clobber the plaintext mid-migration. Serve the
        # plaintext mountpoint (MOUNTED, no attach) so the daemon binds and the
        # lifespan auto-resume drives the migration to completion, instead of
        # tripping RogueMountpointError and exiting 1.
        if _migration_holds_mountpoint():
            logger.info(
                "upgrade migration in progress; serving the plaintext recordings "
                "mountpoint and deferring the container attach to the migration "
                "cutover (not a rogue mountpoint)"
            )
            return StoreResolution(StoreState.MOUNTED, mountpoint=str(mountpoint))
        return _attempt_mount(bundle, Path(mountpoint), key)


def mount_now() -> StoreResolution:
    """Mount the store IGNORING the sealed sentinel — the U9 unlock remount path.

    :func:`resolve_store_state` deliberately short-circuits to ``LOCKED`` when the
    sentinel is present (bind-before-mount), so the unlock verb cannot use it to
    remount. This is the sibling that resolves the key + attaches WITHOUT the
    sentinel gate, under the same ``mount.lock``. The unlock verb calls this
    BEFORE clearing the sentinel: on ``ERROR`` (e.g. a locked Keychain) it returns
    the typed reason and the caller leaves the sentinel intact (retryable in-band,
    KTD-14); only on ``MOUNTED`` does the caller clear the sentinel and reconcile.

    Container-disabled → ``MOUNTED`` (nothing to mount; a plaintext install has no
    sentinel to reach here). ROGUE / corrupted bundle re-raise the container
    operator exception unchanged, matching :func:`resolve_store_state`.
    """
    from screencap import config, container

    if not config.get_container_enabled():
        return StoreResolution(StoreState.MOUNTED)

    with mount_lock():
        bundle = store_bundle_path()
        if not bundle.exists():
            return StoreResolution(StoreState.ABSENT)
        try:
            key = container.require_container_key()
        except container.ContainerKeyMissingError:
            return StoreResolution(StoreState.ERROR, reason=ERROR_KEY_MISSING)
        except container.ContainerKeyUnreachableError:
            return StoreResolution(
                StoreState.ERROR, reason=ERROR_ENTITLEMENT_MISMATCH
            )
        except container.KeychainLockedError:
            return StoreResolution(StoreState.ERROR, reason=ERROR_KEYCHAIN_LOCKED)

        mountpoint = config.get_recordings_dir()
        return _attempt_mount(bundle, Path(mountpoint), key)


def relocate_bundle(target_dir: Path) -> "Any":
    """Move the encrypted bundle to ``target_dir`` and remount (SCR-258 U11, KTD-19).

    Invariants:

    * **Never force-detach** (compact discipline, KTD-12/KTD-19): the quiescent
      detach uses ``force=False``. A busy volume (:class:`ContainerBusyError`)
      surfaces a typed refusal with **nothing moved** and the store left mounted —
      the move does not proceed.
    * The recordings **mountpoint is unchanged** — only the bundle's backing
      directory moves. The config flip (``config.set_store_bundle_dir``) is the
      single commit point, so a failed/interrupted move never leaves config
      pointing at a vanished bundle.
    * On any failure the source bundle stays authoritative and is **remounted**,
      so the store is never left detached.

    FIX 6 (availability): a **cross-volume** move's unbounded ``copytree`` is
    staged to the ``.partial`` path OUTSIDE ``mount.lock`` — so a concurrent
    ``unlock`` / ``resolve_store_state`` is not wedged behind the lock for the
    whole copy. ``mount.lock`` is then held only for the brief detach → promote
    (partial→final rename) → config-commit → remount. The store stays mounted +
    available during the copy; the daemon verb already refused an active recording
    / terminal stage / running encrypt job, so the bundle has no in-flight writer
    and the staged copy is quiescent. A **same-volume** move is an O(1) rename and
    stays entirely inside the (brief) lock hold.

    Returns a :class:`storage_migration.MigrationOutcome`. Callers must have
    already refused an active recording / running encrypt job / sealed store /
    cloud-synced target (the daemon verb does this before calling here).
    """
    from screencap import config, container, storage_migration

    bundle = store_bundle_path()  # current bundle (pre-move)
    bundle_dst = target_dir / container.BUNDLE_NAME
    mountpoint = Path(config.get_recordings_dir())

    same_volume = storage_migration.bundle_move_is_same_volume(bundle, target_dir)

    # Stage the long cross-volume copy BEFORE taking the lock (FIX 6). Nothing
    # destructive happens here and the source stays authoritative, so an
    # interrupted copy just leaves a stale ``.partial`` a retry cleans.
    if not same_volume:
        storage_migration.stage_bundle_copy(bundle, bundle_dst)

    with mount_lock():
        # Resolve the key up front so the remount below always has it, whether
        # the move succeeds or we have to roll back to the source bundle.
        key = container.require_container_key()

        # Quiescent detach — NEVER force (KTD-12/KTD-19). A busy volume means an
        # in-flight writer we did not catch above; refuse rather than yank it.
        try:
            container.detach(str(mountpoint), force=False)
        except container.ContainerBusyError:
            if not same_volume:
                # Nothing was moved — drop the staged copy so a retry starts clean.
                storage_migration.discard_staged_bundle_copy(bundle_dst)
            return storage_migration.MigrationOutcome(
                ok=False,
                code="recording_active",
                message=(
                    "The storage is still in use. Stop any active recording and "
                    "try again in a moment."
                ),
            )

        try:
            if same_volume:
                outcome = storage_migration.migrate_bundle(
                    bundle, bundle_dst, config.set_store_bundle_dir
                )
            else:
                outcome = storage_migration.promote_bundle_copy(
                    bundle, bundle_dst, config.set_store_bundle_dir
                )
        except Exception:
            # The config flip is the commit point; a raised move never flipped
            # it, so store_bundle_path() still resolves the intact source bundle.
            _attempt_mount(store_bundle_path(), mountpoint, key)
            raise

        # Remount whichever bundle config now points at: the new location on
        # success, the untouched source on a soft failure.
        _attempt_mount(store_bundle_path(), mountpoint, key)
        return outcome


__all__ = [
    "StoreState",
    "StoreResolution",
    "DISK_HOST_PATH_ENV",
    "ERROR_KEY_MISSING",
    "ERROR_ENTITLEMENT_MISMATCH",
    "ERROR_KEYCHAIN_LOCKED",
    "ERROR_DOWNGRADE_UNSUPPORTED",
    "store_bundle_path",
    "sealed_sentinel_path",
    "mount_lock_path",
    "is_sealed",
    "write_sealed_sentinel",
    "clear_sealed_sentinel",
    "host_disk_path",
    "disk_host_env",
    "resolve_store_state",
    "mount_now",
    "relocate_bundle",
]
