"""Encrypted sparse-bundle container primitives (SCR-236).

ScreenCap's local recording data plane lives inside an app-managed
encrypted sparse bundle (AES-256, APFS inside) so the on-disk artifact
is ciphertext at all times. This module is the single wrapper around
``hdiutil`` — create / attach / detach / compact / status — plus the
shared passphrase helper, the typed failure taxonomy, per-attach
host-leak hardening, and the host-volume free-space resolver.

Scope boundaries within the container subsystem:

* **This module (U1):** pure ``hdiutil`` primitives + taxonomy +
  hardening. No Keychain access, no mount policy. Functions take
  explicit paths and raw key bytes.
* **``container`` key functions (U2):** Keychain-backed key lifecycle
  (added to this module).
* **``ensure_store_mounted()`` (U4):** the one mount owner that layers
  state-machine policy (LOCKED / ROGUE / KEY_MISSING / …) on top of
  these primitives, serialized by ``run/mount.lock``.

Design contracts:

* **Passphrase piping (KTD-6):** every ``hdiutil`` call that needs the
  key pipes it via ``communicate(input=key)`` with **no** trailing
  newline. A trailing ``\\n`` becomes part of the passphrase and fails
  authentication (verified on real hardware — see the U1 spike results
  in ``tests/test_container.py``). No call site constructs its own pipe.
* **Typed failures (KTD-10):** a silently failed mount must never
  degrade to "empty recordings". Every primitive raises a typed
  :class:`ContainerError`; retryable states carry ``exit_code`` 75
  (``EX_TEMPFAIL``, so launchd retries) and operator states carry 1.
* **Hardening on every attach (KTD-9):** Spotlight is disabled and
  re-asserted per mount (macOS silently re-enabled indexing across an
  OS point-release), ``.fseventsd/no_log`` is ensured, and attach always
  uses ``-nobrowse -owners on -mountpoint``.

Subprocess idiom mirrors ``engine/utils.py`` (``capture_output=True``,
explicit ``timeout=``) but raises typed exceptions instead of returning
``None``.
"""

from __future__ import annotations

import math
import os
import plistlib
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VOLUME_NAME = "ScreenCapStore"
"""APFS volume label inside the bundle. Stable — informational only."""

SPARSE_BAND_SIZE_SECTORS = 32768
"""Sparse-bundle band size in 512-byte sectors → 16 MiB bands.

Tuned up from ``hdiutil``'s 8 MiB default (KTD-1: "U1 picks the value").
The bundle is created with a declared size equal to the host volume's
capacity (KTD-13), so band count = declared_size / band_size must stay
far below the sparsebundle band-count ceiling that degrades performance:
16 MiB bands keep a 4 TB declaration at ~256 k bands while keeping
allocation granularity modest (each populated band is 16 MiB on the
host). Larger bands would lower the count further but coarsen
``store compact`` reclaim (KTD-12) and inflate the small-store
footprint; 16 MiB is the balance for a low-volume launch.
"""

_HDIUTIL = "hdiutil"

# hdiutil operation timeouts (seconds). compact scans the whole image so
# it gets a generous budget; the rest are quick metadata/mount ops.
_CREATE_TIMEOUT = 120
_ATTACH_TIMEOUT = 60
_DETACH_TIMEOUT = 60
_INFO_TIMEOUT = 30
_COMPACT_TIMEOUT = 600

_DETACH_FORCE_WAIT_S = 3.0
"""Graceful-then-force detach wait (KTD-10) — shipped-practice ~3 s
(electron-builder ``dmgUtil.ts``)."""

EX_TEMPFAIL = getattr(os, "EX_TEMPFAIL", 75)
"""launchd retries a job that exits 75; operator-fatal states exit 1."""


# ---------------------------------------------------------------------------
# Failure taxonomy (KTD-10)
# ---------------------------------------------------------------------------


class ContainerError(Exception):
    """Base for all container failures. Defaults to operator-fatal."""

    exit_code = 1


class RetryableContainerError(ContainerError):
    """Transient state — launchd should retry (exit ``EX_TEMPFAIL``).

    Keychain locked (U2), DiskArbitration busy, transient attach/detach
    failure. The daemon maps these to exit 75.
    """

    exit_code = EX_TEMPFAIL


class FatalContainerError(ContainerError):
    """Operator state — retrying will not help (exit 1).

    Wrong passphrase, corrupted bundle, missing key with an existing
    bundle (U2), rogue mountpoint (U4). Surfaces a distinct message.
    """

    exit_code = 1


class ContainerBusyError(RetryableContainerError):
    """The image or its DiskArbitration session is busy right now."""


class ContainerAuthError(FatalContainerError):
    """``hdiutil`` rejected the passphrase (key/bundle mismatch)."""


class ContainerCorruptError(FatalContainerError):
    """The bundle's ``Info.plist`` / structure could not be parsed."""


class RogueLockError(FatalContainerError):
    """The lock path (or its parent) is a symlink — refuse to follow it.

    Unlike ``_autospawn._open_auto_log`` (which degrades to ``/dev/null``
    for a *log*), a *lock* that silently no-ops would break mutual
    exclusion and let two daemons attach concurrently. So a symlinked
    lock path is a hard stop, not a fallback.
    """


class ContainerKeyLockedError(RetryableContainerError):
    """The login Keychain is locked; the key read should be retried (U2)."""


class ContainerKeyMissingError(FatalContainerError):
    """The bundle exists but its Keychain key is gone — unrecoverable (AE2).

    Minting a fresh key here would orphan every recording, so this is a
    loud operator stop, never a silent re-create.
    """


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def default_bundle_path() -> Path:
    """``~/.screencap/store.sparsebundle`` — the ciphertext-at-rest store.

    The mountpoint is the recordings directory itself (KTD-2), resolved
    by ``config``; this is only the on-disk bundle artifact.
    """
    return Path.home() / ".screencap" / "store.sparsebundle"


# ---------------------------------------------------------------------------
# Keychain-backed key lifecycle (KTD-5)
# ---------------------------------------------------------------------------

KEY_SERVICE = "com.screencap.container"
"""Keychain ``service`` for the container passphrase. Same shape as
``network/crypto.SERVICE`` (``com.screencap.network``); stable — changing
it orphans the store."""

KEY_ACCOUNT = "key"
"""Keychain ``account`` for the container passphrase."""

_KEY_RANDOM_BYTES = 32
"""Entropy behind the passphrase: 32 random bytes = 256 bits."""


def get_container_key() -> bytes | None:
    """Read-only lookup of the container passphrase. ``None`` if absent.

    Diverges from ``network/crypto.get_or_create_kek`` on purpose (KTD-5):
    the container read path **never creates**. A missing key with an
    existing bundle is unrecoverable and must surface loudly (AE2), not be
    papered over with a fresh key that orphans the store.

    The stored secret is the base64 text of 32 random bytes, and *that
    ASCII text is the passphrase* piped to ``hdiutil`` — never the raw
    random bytes, which could contain a ``0x0a`` and trip the KTD-6
    no-trailing-newline rule (or an embedded NUL on stdin). Returning the
    ASCII bytes keeps the passphrase newline/NUL-free by construction.

    Raises:
        ContainerKeyLockedError: the login Keychain is locked (retryable —
            ``keyring`` raises ``KeyringLocked`` rather than hanging).
    """
    # Lazy import: keyring's Darwin backend dispatches to the Security
    # framework on first access; a module-level import would slow
    # ``screencap --help``. Mirrors network/crypto.py.
    import keyring
    import keyring.errors

    try:
        stored = keyring.get_password(KEY_SERVICE, KEY_ACCOUNT)
    except keyring.errors.KeyringLocked as exc:
        raise ContainerKeyLockedError("login Keychain is locked") from exc
    if stored is None:
        return None
    return stored.encode("ascii")


def create_container_key(bundle: Path) -> bytes:
    """Create and store a fresh container passphrase; return its bytes.

    Refuses when the bundle already exists (KTD-5): a new key would orphan
    every recording inside the existing store. Creation is only legal when
    *neither* the bundle *nor* a key exists — the ``store init`` /
    ``serve --install`` foreground path does the check-lock-check around
    this call. Runs only in a foreground CLI process (the one-time ACL
    prompt needs the right ``auth.py`` identity), never a launchd tick.

    Returns the passphrase bytes (base64 ASCII of 32 random bytes) so the
    caller can attach immediately without a second Keychain read.

    Raises:
        FatalContainerError: the bundle already exists.
        ContainerKeyLockedError: the Keychain is locked.
    """
    import base64
    import secrets

    import keyring
    import keyring.errors

    if Path(bundle).exists():
        raise FatalContainerError(
            f"refusing to create a container key: bundle already exists at "
            f"{bundle} (a new key would orphan it)"
        )
    passphrase = base64.b64encode(secrets.token_bytes(_KEY_RANDOM_BYTES)).decode("ascii")
    try:
        keyring.set_password(KEY_SERVICE, KEY_ACCOUNT, passphrase)
    except keyring.errors.KeyringLocked as exc:
        raise ContainerKeyLockedError("login Keychain is locked") from exc
    return passphrase.encode("ascii")


# ---------------------------------------------------------------------------
# Passphrase helper (KTD-6) + subprocess wrapper
# ---------------------------------------------------------------------------


def _run_hdiutil(
    args: list[str],
    *,
    key: bytes | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``hdiutil <args>``; pipe ``key`` verbatim when a passphrase is needed.

    THE ONLY place a passphrase is piped to ``hdiutil``. ``key`` is
    written to stdin exactly as given — never with an appended newline
    (KTD-6). Callers pass ``-stdinpass`` in ``args`` when ``key`` is set.

    Raises:
        ContainerBusyError: if ``hdiutil`` does not return within
            ``timeout`` (treated as transient — a stuck DiskArbitration
            op is retryable).
    """
    if key is not None and key.endswith(b"\n"):
        # Defensive: a trailing newline silently fails authentication.
        raise ValueError("container key must not carry a trailing newline")
    try:
        return subprocess.run(
            [_HDIUTIL, *args],
            input=key,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContainerBusyError(
            f"hdiutil {args[0] if args else ''} timed out after {timeout}s"
        ) from exc
    except FileNotFoundError as exc:  # pragma: no cover - hdiutil always present on macOS
        raise FatalContainerError("hdiutil not found (macOS only)") from exc


def _stderr(cp: subprocess.CompletedProcess[bytes]) -> str:
    return cp.stderr.decode("utf-8", errors="replace").strip()


def _classify_attach_failure(cp: subprocess.CompletedProcess[bytes]) -> ContainerError:
    """Map an ``hdiutil attach`` non-zero exit to a typed error."""
    err = _stderr(cp).lower()
    if "authentication" in err or "passphrase" in err or "no valid" in err:
        return ContainerAuthError(f"container authentication failed: {_stderr(cp)}")
    if "corrupt" in err or "not recognized" in err or "invalid" in err:
        return ContainerCorruptError(f"container image unreadable: {_stderr(cp)}")
    if "busy" in err or "resource temporarily" in err or "in use" in err:
        return ContainerBusyError(f"container busy: {_stderr(cp)}")
    # Unknown attach failure — operator-fatal so it is loud, not retried
    # into a launchd loop.
    return FatalContainerError(f"hdiutil attach failed: {_stderr(cp)}")


# ---------------------------------------------------------------------------
# Status / idempotency (hdiutil info)
# ---------------------------------------------------------------------------


def container_mountpoint(bundle: Path) -> str | None:
    """Return the mountpoint if ``bundle`` is currently attached, else ``None``.

    Reads ``hdiutil info -plist`` and matches on the realpath of
    ``image-path`` — the idempotency signal the U1 spike confirmed: a
    naive second ``hdiutil attach`` errors, so the one mount owner must
    detect an existing mount here and reuse it rather than re-attach.

    An empty string means "attached but not mounted at a path" (a
    transient/degenerate DiskArbitration state); callers treat it as
    not-usable-yet.
    """
    cp = _run_hdiutil(["info", "-plist"], timeout=_INFO_TIMEOUT)
    if cp.returncode != 0:
        return None
    try:
        info = plistlib.loads(cp.stdout)
    except Exception as exc:
        raise ContainerCorruptError("could not parse `hdiutil info` output") from exc
    want = os.path.realpath(str(bundle))
    for image in info.get("images", []):
        image_path = image.get("image-path", "")
        if os.path.realpath(image_path) == want:
            for entity in image.get("system-entities", []):
                mount_point = entity.get("mount-point")
                if mount_point:
                    return mount_point
            return ""  # attached, no mountpoint
    return None


def is_attached(bundle: Path) -> bool:
    """True iff ``bundle`` is currently attached (mounted or not)."""
    return container_mountpoint(bundle) is not None


# ---------------------------------------------------------------------------
# Host-volume free-space resolver (KTD-13)
# ---------------------------------------------------------------------------


def host_backing_free_bytes(bundle: Path) -> int:
    """Free bytes on the **host volume backing the bundle**, not the mounted store.

    The mounted volume reports free space against its *declared* size
    (host capacity, KTD-13), so capture's ``disk_warn_mb`` /
    ``disk_stop_mb`` guards must evaluate the host — otherwise they never
    fire until the host is already full (the 2018 sparse-bundle
    silent-write failure class). The U1 spike confirmed the mount
    boundary is real (distinct ``st_dev``) and that ``statvfs`` on the
    bundle's parent yields host free space directly — no ``diskutil``
    parsing or ``st_dev`` walking needed.
    """
    sv = os.statvfs(str(bundle.parent))
    return sv.f_bavail * sv.f_frsize


def _host_capacity_size_arg(bundle: Path) -> str:
    """``hdiutil -size`` string = host volume capacity rounded up to whole GB.

    Declared size = host capacity makes the declaration free (sparseness)
    while never advertising virtual space the host cannot back (KTD-13).
    """
    sv = os.statvfs(str(bundle.parent))
    capacity_bytes = sv.f_blocks * sv.f_frsize
    gb = max(1, math.ceil(capacity_bytes / 1_000_000_000))
    return f"{gb}g"


# ---------------------------------------------------------------------------
# create / attach / detach / compact
# ---------------------------------------------------------------------------


def create_container(
    bundle: Path,
    key: bytes,
    *,
    size: str | None = None,
    band_size_sectors: int = SPARSE_BAND_SIZE_SECTORS,
) -> None:
    """Create an AES-256 APFS sparse bundle at ``bundle`` (KTD-1).

    ``size`` is an ``hdiutil`` size string (e.g. ``"500g"``); when
    omitted it defaults to the host volume's capacity (KTD-13). Refuses
    to overwrite an existing bundle — the one mount owner and
    ``store init`` do the check-lock-check that guards against orphaning
    a key/bundle pair; this primitive just fails loud on a surprise.

    Raises:
        FatalContainerError: the bundle already exists, or ``hdiutil
            create`` failed.
    """
    bundle = Path(bundle)
    if bundle.exists():
        raise FatalContainerError(f"container already exists at {bundle}")
    bundle.parent.mkdir(parents=True, exist_ok=True)
    size_arg = size or _host_capacity_size_arg(bundle)
    cp = _run_hdiutil(
        [
            "create",
            "-size", size_arg,
            "-encryption", "AES-256",
            "-stdinpass",
            "-fs", "APFS",
            "-type", "SPARSEBUNDLE",
            "-volname", VOLUME_NAME,
            "-imagekey", f"sparse-band-size={band_size_sectors}",
            str(bundle),
        ],
        key=key,
        timeout=_CREATE_TIMEOUT,
    )
    if cp.returncode != 0:
        raise FatalContainerError(f"hdiutil create failed: {_stderr(cp)}")


def attach_container(bundle: Path, key: bytes, mountpoint: Path) -> str:
    """Attach ``bundle`` at ``mountpoint``, hardened; return the live mountpoint.

    Idempotent: if the bundle is already attached (spike: a naive second
    attach errors), the existing mountpoint is reused and re-hardened
    rather than re-attaching. Attach always uses ``-nobrowse -owners on
    -mountpoint`` (KTD-9). Hardening (Spotlight off, ``no_log``) runs on
    every return path, since macOS re-enables indexing across OS updates.

    Raises:
        ContainerAuthError / ContainerCorruptError / ContainerBusyError /
        FatalContainerError: per the attach failure classification.
    """
    existing = container_mountpoint(bundle)
    if existing:
        harden_mount(existing)
        return existing

    mountpoint = Path(mountpoint)
    mountpoint.mkdir(parents=True, exist_ok=True)
    cp = _run_hdiutil(
        [
            "attach",
            "-stdinpass",
            "-nobrowse",
            "-owners", "on",
            "-mountpoint", str(mountpoint),
            "-plist",
            str(bundle),
        ],
        key=key,
        timeout=_ATTACH_TIMEOUT,
    )
    if cp.returncode != 0:
        raise _classify_attach_failure(cp)

    # Resolve the actual mountpoint from the attach plist (authoritative),
    # falling back to the requested path.
    resolved = _mountpoint_from_attach_plist(cp.stdout) or str(mountpoint)
    harden_mount(resolved)
    return resolved


def _mountpoint_from_attach_plist(stdout: bytes) -> str | None:
    try:
        parsed = plistlib.loads(stdout)
    except Exception:
        return None
    for entity in parsed.get("system-entities", []):
        mount_point = entity.get("mount-point")
        if mount_point:
            return mount_point
    return None


def detach_container(mountpoint: str, *, wait: float = _DETACH_FORCE_WAIT_S) -> None:
    """Detach the volume, graceful-then-force (KTD-10).

    Plain ``detach`` first; on failure wait ~3 s (a reader may be mid-op)
    then ``detach -force``. A still-busy force is retryable ("not now").

    Raises:
        ContainerBusyError: the volume could not be detached even forcibly.
    """
    cp = _run_hdiutil(["detach", str(mountpoint)], timeout=_DETACH_TIMEOUT)
    if cp.returncode == 0:
        return
    time.sleep(wait)
    forced = _run_hdiutil(["detach", "-force", str(mountpoint)], timeout=_DETACH_TIMEOUT)
    if forced.returncode != 0:
        raise ContainerBusyError(f"could not detach {mountpoint}: {_stderr(forced)}")


def compact_container(bundle: Path, key: bytes) -> int:
    """Compact ``bundle`` to reclaim host space from deleted bands (KTD-12).

    Requires the image **detached** (caller's responsibility). Encrypted
    images need the passphrase piped (spike: without ``-stdinpass`` the
    call prompts and hangs). Returns the host bytes freed (>= 0).

    Raises:
        ContainerBusyError: the image is attached / busy.
        FatalContainerError: compact failed for another reason.
    """
    bundle = Path(bundle)
    before = _bundle_disk_usage_bytes(bundle)
    cp = _run_hdiutil(
        ["compact", str(bundle), "-batteryallowed", "-stdinpass"],
        key=key,
        timeout=_COMPACT_TIMEOUT,
    )
    if cp.returncode != 0:
        err = _stderr(cp).lower()
        if "attach" in err or "busy" in err or "in use" in err:
            raise ContainerBusyError(f"cannot compact an attached image: {_stderr(cp)}")
        raise FatalContainerError(f"hdiutil compact failed: {_stderr(cp)}")
    after = _bundle_disk_usage_bytes(bundle)
    return max(0, before - after)


def _bundle_disk_usage_bytes(bundle: Path) -> int:
    """Actual host bytes occupied by the bundle's ``bands`` (best-effort)."""
    total = 0
    bands = Path(bundle) / "bands"
    try:
        for entry in bands.iterdir():
            try:
                total += entry.stat().st_blocks * 512
            except OSError:
                continue
    except OSError:
        return 0
    return total


# ---------------------------------------------------------------------------
# Per-attach host-leak hardening (KTD-9)
# ---------------------------------------------------------------------------


def harden_mount(mountpoint: str) -> None:
    """Re-assert host-leak hardening on ``mountpoint`` (KTD-9).

    Disables Spotlight (``mdutil -i off``) and ensures
    ``.fseventsd/no_log`` at the volume root. Best-effort and warn-only —
    a failed hardening step never blocks the mount. Spotlight state is
    re-asserted on *every* attach because macOS silently re-enabled
    indexing on all volumes after an OS point-release.

    The verification is tolerant: on macOS 26 ``mdutil -s`` reports
    ``"unknown indexing state"`` for a volume with no Spotlight store
    (confirmed by the U1 spike), which is a non-indexing outcome — only a
    positive ``"Indexing enabled"`` is a concern, and even then we only
    log it (no per-volume opt-out is guaranteed).
    """
    root = Path(mountpoint)
    try:
        subprocess.run(
            ["mdutil", "-i", "off", str(root)],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        no_log = root / ".fseventsd" / "no_log"
        no_log.parent.mkdir(parents=True, exist_ok=True)
        if not no_log.exists():
            no_log.touch()
    except OSError:
        pass


def spotlight_indexing_enabled(mountpoint: str) -> bool | None:
    """Best-effort read of ``mdutil -s``: ``True`` only on a positive
    "Indexing enabled", ``False`` on disabled / unknown-indexing-state,
    ``None`` if the check itself failed.

    Split out from :func:`harden_mount` so U4/U7 can surface the state
    without re-running the disable. "unknown indexing state" (macOS 26,
    per the U1 spike) maps to ``False`` — the volume is not being indexed.
    """
    try:
        cp = subprocess.run(
            ["mdutil", "-s", str(mountpoint)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    text = (cp.stdout + cp.stderr).lower()
    if "indexing enabled" in text or ("enabled" in text and "disabled" not in text and "unknown" not in text):
        return True
    return False


# ---------------------------------------------------------------------------
# Hardened lock-file open (KTD-3 symlink guard)
# ---------------------------------------------------------------------------


def open_hardened_lock(path: Path) -> int:
    """Open a lock file at ``path`` with the ``_autospawn`` hardening, but
    **raise** on a symlinked path instead of degrading to ``/dev/null``.

    ``os.open(..., O_NOFOLLOW)`` plus a ``realpath == abspath`` parent
    check (KTD-3), matching ``cli/_autospawn._open_auto_log`` — but a
    lock must never silently no-op (two daemons would both "hold" it), so
    a symlink at the lock path or a symlinked parent is a hard stop.

    Returns an open ``O_RDWR`` fd (mode ``0o600``) the caller
    ``fcntl.flock``s and is responsible for closing.

    Raises:
        RogueLockError: the lock path or its parent resolves through a
            symlink.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    if os.path.realpath(str(parent)) != os.path.abspath(str(parent)):
        raise RogueLockError(f"lock parent {parent} resolves through a symlink")
    try:
        return os.open(
            str(path),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        # ELOOP: a symlink sits at the lock path itself.
        raise RogueLockError(f"lock path {path} is a symlink") from exc


__all__ = [
    "VOLUME_NAME",
    "SPARSE_BAND_SIZE_SECTORS",
    "EX_TEMPFAIL",
    "ContainerError",
    "RetryableContainerError",
    "FatalContainerError",
    "ContainerBusyError",
    "ContainerAuthError",
    "ContainerCorruptError",
    "RogueLockError",
    "ContainerKeyLockedError",
    "ContainerKeyMissingError",
    "KEY_SERVICE",
    "KEY_ACCOUNT",
    "get_container_key",
    "create_container_key",
    "default_bundle_path",
    "container_mountpoint",
    "is_attached",
    "host_backing_free_bytes",
    "create_container",
    "attach_container",
    "detach_container",
    "compact_container",
    "harden_mount",
    "spotlight_indexing_enabled",
    "open_hardened_lock",
]
