"""``hdiutil`` primitives for the at-rest encrypted recording store (SCR-236, U1).

This module wraps the macOS ``hdiutil`` CLI around an **encrypted sparse-bundle**
container (AES-256, APFS inside) that holds ScreenCap's recording data plane. It
is the foundation unit: it owns the raw create / attach / detach / compact /
status primitives, the single shared passphrase-piping helper, the typed
exception taxonomy a daemon can later map to exit codes, ``-plist`` output
parsing, and the per-attach host-leak hardening. It does **not** do key
management (Keychain read/write — that is U2) and it does **not** own mount
orchestration or lifecycle state (``ensure_store_mounted()`` — that is U4). Those
higher layers call the primitives here.

Design references (all in ``docs/plans/2026-07-06-001-feat-scr-236-encrypt-recordings-at-rest-plan.md``):

* **KTD-1** — encrypted sparse bundle via ``hdiutil`` (AES-256, APFS inside),
  tuned ``-imagekey sparse-band-size=``, declared size = host volume capacity
  (KTD-13) so the volume's virtual free space never invites writes the host
  cannot back.
* **KTD-6** — exactly one helper (:func:`_run_hdiutil`) constructs the passphrase
  pipe. Every ``hdiutil`` invocation that needs the key pipes it via
  ``proc.communicate(input=key)`` with **no trailing newline** — a trailing
  ``\\n`` becomes part of the passphrase and fails authentication (verified on
  this hardware). No call site rolls its own pipe.
* **KTD-9** — host-leak hardening (:func:`harden_mount`) runs on **every** attach,
  not once: ``mdutil -i off`` + verify with ``mdutil -s`` (macOS silently
  re-enables indexing after OS updates), ensure ``.fseventsd/no_log`` at the
  volume root, and always attach with ``-nobrowse -owners on -mountpoint``.
* **KTD-10** — the failure taxonomy: *retryable* states (Keychain locked,
  transient attach / DiskArbitration busy) vs *operator* states (rogue
  mountpoint, corrupted bundle, missing key). Detach is graceful-then-force with
  a ~3 s wait on transient failure.
* **KTD-13** — declared image size equals the host volume capacity.

Subprocess idiom mirrors ``src/screencap/engine/utils.py`` (deferred
``import subprocess``, ``capture_output``-equivalent pipes, explicit
``timeout=``) but **raises typed exceptions instead of returning None** — a
silently failed mount must never degrade to "empty recordings".

The on-hardware feasibility spike (real create→attach→write→read→detach
roundtrip, detached-band sentinel grep for AE1, ``flock`` canary, ``kill -9``
WAL crash safety, capture-shaped write benchmark, ``compact`` shrink, backup
restore, near-full host, Keychain portability) is laid out as
``@pytest.mark.macos_hw`` tests in ``tests/test_container.py``; **its results are
recorded in that file's module docstring**, and downstream units (U4+) assume
those outcomes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

BUNDLE_NAME = "store.sparsebundle"
"""Default on-disk basename of the encrypted sparse bundle (KTD-1). Lives at
``~/.screencap/store.sparsebundle``; the concrete parent directory is resolved
by the config/lifecycle layers (U3/U4), not here."""

DEFAULT_VOLUME_NAME = "ScreenCap"
"""APFS volume label for the mounted store. Cosmetic; ``-nobrowse`` keeps it out
of Finder regardless."""

DEFAULT_SPARSE_BAND_SIZE_SECTORS = 262144
"""``-imagekey sparse-band-size=`` value, in 512-byte sectors (KTD-1).

262144 sectors = 128 MiB per band. Tuned so a multi-hundred-GB declared store
stays far below the sparse-bundle band-count ceiling (a 512 GB store is ~4096
bands at this size) while keeping each band small enough that ``compact`` and
Time Machine band churn stay cheap. The U1 hardware spike is the authority for
the final value; this is the pre-spike default."""

_DETACH_FORCE_WAIT_SECONDS = 3.0
"""Wait between a graceful detach that fails *transiently* and the ``-force``
retry (KTD-10). Shipped practice (electron-builder ``dmgUtil.ts``)."""

_DEFAULT_TIMEOUT = 120.0
"""Default subprocess wall-clock timeout, in seconds. ``create`` on a
large declared size and ``compact`` can be slow; callers may override."""

# --- container-key identifiers (U2, split-custody per KTD-22) ---------------

CONTAINER_KEYCHAIN_SERVICE = "screencap-container"
"""Keychain ``service`` for the container passphrase. **Distinct** from the corpus
key's ``screencap-corpus`` (:data:`screencap.corpus_crypto.CORPUS_KEYCHAIN_SERVICE`)
so the two keys never collide. Stable across versions — changing it orphans an
existing encrypted store (the key is a KEK, not re-derivable)."""

CONTAINER_KEYCHAIN_ACCOUNT = "key"
"""Keychain ``account`` for the container passphrase."""

CONTAINER_KEY_FILE_ENV = "SCREENCAP_CONTAINER_KEY_FILE"
"""Env var naming a ``0600`` file holding the base64 container key, mirroring
:data:`screencap.corpus_crypto.CORPUS_KEY_FILE_ENV`. A headless / test channel;
when set it is the SOLE key source (the Keychain is never consulted)."""

KEYCHAIN_ACCESS_GROUP = os.environ.get(
    "SCREENCAP_KEYCHAIN_ACCESS_GROUP", "2A8S6MV8DZ.com.screencap.shared"
)
"""Shared Keychain access group (SCR-241). Same value + env override as
:data:`screencap.corpus_crypto.KEYCHAIN_ACCESS_GROUP`; every binary signed with
the Team ID *and* carrying the ``keychain-access-groups`` entitlement reads the
key without a prompt. Re-declared here to avoid importing the heavier crypto
module."""

_CONTAINER_KEY_LEN = 32
"""Container passphrase length in bytes (AES-256 → 256-bit key)."""

_ENTITLEMENT_MISMATCH_MSG = (
    "container key unreadable: the key is present in the shared Keychain access group "
    "but this binary is not entitled to read it (errSecMissingEntitlement). This is NOT "
    "data loss — the encrypted store and its key are intact. Run the vault from the "
    "entitled ScreenCap app or its bundled CLI; a pip/pyenv terminal CLI or a Debug "
    "'python3 -m screencap.cli' build is an unsupported vault consumer (KTD-22)."
)
"""Message for :class:`ContainerKeyUnreachableError` — names the cause, points at the
entitled binary, and explicitly is not a data-loss statement (KTD-22)."""

_NO_KEY_ANYWHERE_MSG = (
    "container key missing: the encrypted store exists on disk but no key was found in "
    "any channel (shared Keychain access group or key-file). The key appears lost — the "
    "video data is intact but the store cannot be unlocked without it. Restore the key "
    "from backup."
)
"""Message for :class:`ContainerKeyMissingError` — the genuine key-loss diagnosis (KTD-22)."""

# ---------------------------------------------------------------------------
# Exception taxonomy (KTD-10)
# ---------------------------------------------------------------------------


class ContainerError(Exception):
    """Base class for every typed container failure.

    Carries a ``retryable`` flag and a ``exit_code`` hint so a daemon can map an
    exception straight to a process exit code (KTD-10) without re-classifying:
    retryable failures exit ``EX_TEMPFAIL`` (75) so launchd retries; operator
    failures exit 1 with a distinct message. The two axes are also expressed as
    distinct subclasses (:class:`ContainerRetryableError` /
    :class:`ContainerOperatorError`) so call sites can branch on ``isinstance``
    or on the flag, whichever reads cleaner.
    """

    #: Whether launchd/the daemon should retry (transient) rather than stop.
    retryable: bool = False
    #: Process exit code a daemon should use when this escapes to top level.
    exit_code: int = 1


class ContainerRetryableError(ContainerError):
    """Transient failure — the operation may succeed if retried.

    Maps to ``EX_TEMPFAIL`` (75). Concrete cases: the Keychain is locked, or the
    attach/detach hit a busy DiskArbitration / resource-busy condition.
    """

    retryable = True
    exit_code = 75  # os.EX_TEMPFAIL


class ContainerBusyError(ContainerRetryableError):
    """The image/volume is busy (``Resource busy`` / DiskArbitration busy).

    Raised on a transient attach or detach failure; the graceful-then-force
    detach path (:func:`detach`) keys its ~3 s wait off this class.
    """


class KeychainLockedError(ContainerRetryableError):
    """The login Keychain is locked, so the key could not be read.

    U2's key-management layer raises this by mapping
    ``keyring.errors.KeyringLocked``; it lives in the taxonomy here so the whole
    retryable-vs-operator surface is defined in one place.
    """


class ContainerOperatorError(ContainerError):
    """Permanent failure — a human must intervene; retrying will not help.

    Exits 1. Concrete cases: wrong passphrase, corrupted bundle/plist, a rogue
    non-volume directory at the mountpoint, or a bundle that exists with its key
    gone (AE2).
    """

    retryable = False
    exit_code = 1


class ContainerAuthError(ContainerOperatorError):
    """The passphrase was rejected (wrong key, or a trailing-newline pipe bug)."""


class ContainerCorruptError(ContainerOperatorError):
    """The bundle or its ``Info.plist`` / ``hdiutil -plist`` output is corrupt
    or unparseable."""


class RogueMountpointError(ContainerOperatorError):
    """The intended mountpoint holds a non-empty directory that is *not* a
    mounted volume — never auto-cleaned (mirrors ``RogueFileAtSocketPath``)."""


class ContainerKeyMissingError(ContainerOperatorError):
    """The bundle exists on disk but no key is available to unlock it (AE2).

    Minting a fresh key here would orphan every existing recording, so this is a
    hard stop, not a create. This is the **genuine key-loss** case (KTD-22): no
    key was found in *any* channel — restore it from backup or the store stays
    unreadable.
    """


class ContainerKeyUnreachableError(ContainerOperatorError):
    """The key exists in the shared access group but *this binary* can't read it.

    The KTD-22 split partner of :class:`ContainerKeyMissingError`. Raised when a
    shared-group Keychain read returns ``errSecMissingEntitlement`` (-34018): the
    store and its key are **intact**, but the running binary is not entitled for
    the group (a pip/pyenv terminal CLI or a Debug ``python3 -m screencap.cli``
    build — a documented *unsupported* vault consumer). Its message names the
    entitlement mismatch and points at the entitled app/CLI, so an operator gets
    an accurate diagnosis instead of a false "key lost / data loss" alarm. Unlike
    the corpus key (:mod:`screencap.corpus_crypto`, where a mismatch merely
    degrades Search and silently falls back to a legacy ``keyring`` item), the
    container key must NEVER fall back — a different key would render the whole
    library unmountable — so the mismatch is surfaced, not swallowed.

    Operator failure (exit 1), but semantically distinct from data loss.
    """


# ---------------------------------------------------------------------------
# Result / status value objects
# ---------------------------------------------------------------------------


class _HdiutilResult(NamedTuple):
    """Raw outcome of one ``hdiutil`` invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class AttachInfo:
    """The relevant entities from an ``hdiutil attach -plist`` result."""

    device: str | None
    """The whole-disk ``dev-entry`` (e.g. ``/dev/disk4``), or ``None`` if the
    plist carried no device entity."""

    mountpoint: str | None
    """The filesystem mount point (e.g. ``~/.screencap/recordings``), or ``None``
    if no entity was mounted."""


@dataclass(frozen=True)
class ContainerStatus:
    """Whether a given bundle is currently attached, from ``hdiutil info``."""

    attached: bool
    mountpoint: str | None
    device: str | None


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

# stderr substrings, lower-cased, that mark each failure class. Ordered most- to
# least-specific so the first hit wins.
_AUTH_MARKERS = (
    "authentication error",
    "authentication failed",
    "auth error",
    "invalid password",
    "no valid keychain reference",
)
_BUSY_MARKERS = (
    "resource busy",
    "resource temporarily unavailable",
    "device busy",
    "device not configured",
    "could not be unmounted",
    "in use",
)
_CORRUPT_MARKERS = (
    "not a valid",
    "corrupt",
    "invalid image",
    "not recognized",
    "malformed",
)


def _classify_hdiutil_error(result: _HdiutilResult, action: str) -> ContainerError:
    """Map a failed ``hdiutil`` result to the right typed exception (KTD-10).

    Args:
        result: the non-zero :class:`_HdiutilResult`.
        action: the ``hdiutil`` subcommand for the message (``"attach"`` etc.).

    Returns:
        A :class:`ContainerError` subclass instance (not raised — the caller
        raises, so the traceback originates at the call site).
    """
    stderr = result.stderr.decode("utf-8", "replace")
    haystack = stderr.lower()
    detail = stderr.strip() or f"exit code {result.returncode}"
    msg = f"hdiutil {action} failed: {detail}"

    if any(m in haystack for m in _AUTH_MARKERS):
        return ContainerAuthError(msg)
    if any(m in haystack for m in _BUSY_MARKERS):
        return ContainerBusyError(msg)
    if any(m in haystack for m in _CORRUPT_MARKERS):
        return ContainerCorruptError(msg)
    return ContainerOperatorError(msg)


# ---------------------------------------------------------------------------
# The single passphrase-piping helper (KTD-6)
# ---------------------------------------------------------------------------


def _run_hdiutil(
    args: list[str],
    *,
    key: bytes | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    check: bool = True,
) -> _HdiutilResult:
    """Run ``hdiutil`` with ``args``, optionally piping ``key`` as the passphrase.

    **This is the one and only place that constructs the passphrase pipe (KTD-6).**
    When ``key`` is given it is written to the child's stdin verbatim via
    ``proc.communicate(input=key)`` — the bytes are piped exactly as passed, with
    **no trailing newline appended**. A trailing ``\\n`` would become part of the
    passphrase and fail authentication, so callers must pass the raw key bytes
    and this helper must never mutate them. No other function in this module (or
    its callers) may build its own ``hdiutil`` pipe.

    Mirrors the ``screencapture`` subprocess idiom in ``engine/utils.py``
    (deferred ``import subprocess``, explicit ``timeout=``) but raises a typed
    :class:`ContainerError` on failure instead of returning ``None``.

    Args:
        args: the ``hdiutil`` subcommand + flags (without the leading
            ``"hdiutil"``).
        key: raw passphrase bytes to pipe on stdin, or ``None`` to run with no
            stdin. Passed through untouched — no newline is added.
        timeout: subprocess wall-clock timeout in seconds.
        check: when ``True`` (default) a non-zero exit raises the classified
            typed exception; when ``False`` the raw result is returned so the
            caller can branch (e.g. graceful-then-force detach).

    Returns:
        The :class:`_HdiutilResult` (always, when ``check=False``; only on
        success when ``check=True``).

    Raises:
        ContainerError: (a subclass) when ``check`` and the exit is non-zero, or
            :class:`ContainerBusyError` on timeout (a timeout is treated as a
            transient/busy condition worth retrying).
    """
    import subprocess

    action = args[0] if args else "hdiutil"
    proc = subprocess.Popen(
        ["hdiutil", *args],
        stdin=subprocess.PIPE if key is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # KTD-6: pipe the key verbatim, no trailing newline. communicate() does
        # NOT append anything to `input`.
        stdout, stderr = proc.communicate(input=key, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise ContainerBusyError(
            f"hdiutil {action} timed out after {timeout}s"
        ) from exc

    result = _HdiutilResult(proc.returncode, stdout or b"", stderr or b"")
    if check and result.returncode != 0:
        raise _classify_hdiutil_error(result, action)
    return result


def _run_cmd(
    args: list[str],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    check: bool = False,
) -> _HdiutilResult:
    """Run a non-``hdiutil`` helper command (``mdutil`` / ``fdesetup``) with no
    passphrase pipe.

    Kept distinct from :func:`_run_hdiutil` precisely because these commands
    never take the key on stdin — the KTD-6 single-pipe invariant is about the
    passphrase, and only :func:`_run_hdiutil` ever passes ``input=key``.
    """
    import subprocess

    proc = subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
    )
    result = _HdiutilResult(proc.returncode, proc.stdout or b"", proc.stderr or b"")
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ContainerError(f"{args[0]} failed: {detail or result.returncode}")
    return result


# ---------------------------------------------------------------------------
# plist parsing
# ---------------------------------------------------------------------------


def parse_attach_plist(data: bytes) -> AttachInfo:
    """Parse ``hdiutil attach -plist`` output into an :class:`AttachInfo`.

    The plist is a dict with a ``system-entities`` array; each entity may carry a
    ``dev-entry`` and (for the mounted filesystem entity) a ``mount-point``. The
    whole-disk device is the shortest ``dev-entry`` (``/dev/disk4`` rather than
    ``/dev/disk4s1``).

    Raises:
        ContainerCorruptError: if the bytes are not a parseable plist or lack the
            expected ``system-entities`` shape.
    """
    import plistlib

    try:
        root = plistlib.loads(data)
    except Exception as exc:  # plistlib raises a grab-bag; normalize it.
        raise ContainerCorruptError(
            f"could not parse hdiutil attach plist: {exc}"
        ) from exc

    if not isinstance(root, dict) or "system-entities" not in root:
        raise ContainerCorruptError(
            "hdiutil attach plist missing 'system-entities'"
        )

    entities = root.get("system-entities") or []
    mountpoint: str | None = None
    devices: list[str] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        dev = ent.get("dev-entry")
        if isinstance(dev, str):
            devices.append(dev)
        mp = ent.get("mount-point")
        if isinstance(mp, str) and mp:
            mountpoint = mp

    device = min(devices, key=len) if devices else None
    return AttachInfo(device=device, mountpoint=mountpoint)


def parse_info_plist(data: bytes, bundle_path: str) -> ContainerStatus:
    """Parse ``hdiutil info -plist`` output, resolving ``bundle_path``'s status.

    ``hdiutil info -plist`` lists every attached image under ``images``; each has
    an ``image-path`` and a ``system-entities`` array. We match the image whose
    ``image-path`` resolves (via ``realpath``) to ``bundle_path`` and report
    whether it is attached and where it is mounted.

    Raises:
        ContainerCorruptError: if the bytes are not a parseable plist.
    """
    import plistlib

    try:
        root = plistlib.loads(data)
    except Exception as exc:
        raise ContainerCorruptError(
            f"could not parse hdiutil info plist: {exc}"
        ) from exc

    target = os.path.realpath(bundle_path)
    images = root.get("images") if isinstance(root, dict) else None
    for img in images or []:
        if not isinstance(img, dict):
            continue
        image_path = img.get("image-path")
        if not isinstance(image_path, str):
            continue
        if os.path.realpath(image_path) != target:
            continue
        # Matched our bundle -> it is attached. Find the mount point + device.
        mountpoint: str | None = None
        devices: list[str] = []
        for ent in img.get("system-entities") or []:
            if not isinstance(ent, dict):
                continue
            dev = ent.get("dev-entry")
            if isinstance(dev, str):
                devices.append(dev)
            mp = ent.get("mount-point")
            if isinstance(mp, str) and mp:
                mountpoint = mp
        device = min(devices, key=len) if devices else None
        return ContainerStatus(attached=True, mountpoint=mountpoint, device=device)

    return ContainerStatus(attached=False, mountpoint=None, device=None)


def _mdutil_indexing_enabled(text: str) -> bool:
    """Return whether ``mdutil -s`` output reports indexing as *enabled*.

    ``mdutil -s <vol>`` prints e.g. ``Indexing enabled.`` or
    ``Indexing disabled.`` (sometimes ``Indexing and searching disabled.``). We
    treat any explicit "disabled" as disabled; only an explicit "enabled" (and
    not "disabled") counts as enabled.

    On a freshly-attached sparse-bundle volume the spike (KTD-9) found ``mdutil
    -s`` reports ``Error: unknown indexing state`` — Spotlight is not managing or
    indexing the volume at all, which is the desired end state, so that explicit
    message counts as *not enabled*. Truly ambiguous/empty output is still
    reported as enabled so the caller re-asserts rather than trusting a blank
    result.
    """
    low = text.lower()
    if "disabled" in low:
        return False
    if "enabled" in low:
        return True
    if "unknown indexing state" in low:
        # Spotlight isn't tracking this volume — effectively not indexed.
        return False
    # Unknown / unparseable -> assume the worst (still enabled), forcing a
    # re-assert on the next call rather than a false "already off".
    return True


# ---------------------------------------------------------------------------
# Host-volume sizing (KTD-13)
# ---------------------------------------------------------------------------


def host_volume_capacity_bytes(path: str) -> int:
    """Total capacity, in bytes, of the host volume backing ``path`` (KTD-13).

    The bundle is created with a declared size equal to this so the mounted
    volume's virtual free space never exceeds what the host can actually back —
    guarding the 2018 sparse-bundle silent-write-on-full-host failure class. The
    first existing ancestor of ``path`` is used, so this works before the bundle
    (or its parent) is created.
    """
    import shutil

    probe = os.path.abspath(path)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).total


# ---------------------------------------------------------------------------
# hdiutil wrappers
# ---------------------------------------------------------------------------


def create_bundle(
    bundle_path: str,
    key: bytes,
    *,
    size_bytes: int | None = None,
    band_size_sectors: int = DEFAULT_SPARSE_BAND_SIZE_SECTORS,
    volume_name: str = DEFAULT_VOLUME_NAME,
    timeout: float = _DEFAULT_TIMEOUT,
) -> None:
    """Create the encrypted APFS sparse bundle at ``bundle_path`` (KTD-1, KTD-13).

    AES-256 encryption, APFS filesystem inside, a tuned ``sparse-band-size``, and
    a declared size equal to the host volume capacity (sparseness makes the large
    declaration free). The passphrase is piped via the shared helper (KTD-6).

    Args:
        bundle_path: destination ``*.sparsebundle`` path (must not yet exist).
        key: raw passphrase bytes (no trailing newline).
        size_bytes: declared image size; defaults to the host volume capacity
            backing ``bundle_path`` (KTD-13).
        band_size_sectors: ``-imagekey sparse-band-size=`` value in 512-byte
            sectors.
        volume_name: APFS volume label.
        timeout: subprocess timeout in seconds.

    Raises:
        ContainerError: a typed subclass on any ``hdiutil create`` failure.
    """
    if size_bytes is None:
        size_bytes = host_volume_capacity_bytes(bundle_path)
    args = [
        "create",
        "-encryption",
        "AES-256",
        "-stdinpass",
        "-type",
        "SPARSEBUNDLE",
        "-fs",
        "APFS",
        "-size",
        f"{size_bytes}b",
        "-imagekey",
        f"sparse-band-size={band_size_sectors}",
        "-volname",
        volume_name,
        bundle_path,
    ]
    _run_hdiutil(args, key=key, timeout=timeout)


def attach(
    bundle_path: str,
    key: bytes,
    mountpoint: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> AttachInfo:
    """Attach ``bundle_path`` at ``mountpoint`` and return the :class:`AttachInfo`.

    Always uses ``-nobrowse -owners on -mountpoint`` (KTD-9) and pipes the
    passphrase through the shared helper (KTD-6). Parses the ``-plist`` result.

    Note: this does **not** run the per-attach hardening — the caller is expected
    to invoke :func:`harden_mount` immediately after a successful attach (KTD-9);
    keeping them separate lets the mount-orchestration layer sequence and log
    each step.

    Raises:
        ContainerAuthError: wrong passphrase.
        ContainerBusyError: transient / DiskArbitration-busy attach failure
            (retryable).
        ContainerCorruptError: unparseable ``-plist`` output.
        ContainerError: any other ``hdiutil attach`` failure.
    """
    args = [
        "attach",
        "-stdinpass",
        "-nobrowse",
        "-owners",
        "on",
        "-mountpoint",
        mountpoint,
        "-plist",
        bundle_path,
    ]
    result = _run_hdiutil(args, key=key, timeout=timeout)
    return parse_attach_plist(result.stdout)


def detach(
    target: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    force_wait: float = _DETACH_FORCE_WAIT_SECONDS,
    force: bool = True,
) -> None:
    """Detach ``target`` (a mountpoint or ``/dev/diskN``), graceful then force.

    KTD-10: a plain ``hdiutil detach`` is attempted first. On a **transient**
    failure (a busy volume — :class:`ContainerBusyError`) we wait ``force_wait``
    seconds and retry with ``-force``. A non-transient failure propagates
    unchanged — we do not force past, say, a corruption error.

    ``force=False`` is the **compact discipline** (KTD-12): a busy volume raises
    :class:`ContainerBusyError` instead of being force-detached, so ``storage
    compact`` never yanks a volume out from under an active writer. The lock verb
    (U9) keeps the default ``force=True`` — force is acceptable there precisely
    because every writer is ledger-disciplined and readers were signalled.

    Raises:
        ContainerError: if the graceful detach fails non-transiently, or if the
            forced detach also fails.
        ContainerBusyError: when ``force=False`` and the graceful detach hit a
            transient busy condition (never force-detached).
    """
    import time

    try:
        _run_hdiutil(["detach", target], timeout=timeout)
        return
    except ContainerBusyError:
        if not force:
            # Compact discipline (KTD-12): never force past a busy volume.
            raise
        # Transient busy -> give in-flight I/O a moment to drain, then force.
        time.sleep(force_wait)
    _run_hdiutil(["detach", "-force", target], timeout=timeout)


def compact(
    bundle_path: str,
    key: bytes,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> None:
    """Compact ``bundle_path`` to reclaim freed bands to the host (KTD-12).

    The image must be **detached** for this to shrink; the caller is responsible
    for the quiescent, never-force detach cycle (KTD-12) before calling here.
    The passphrase is piped through the shared helper (KTD-6).

    Raises:
        ContainerError: a typed subclass on any ``hdiutil compact`` failure.
    """
    _run_hdiutil(
        ["compact", "-stdinpass", bundle_path],
        key=key,
        timeout=timeout,
    )


def status(bundle_path: str, *, timeout: float = _DEFAULT_TIMEOUT) -> ContainerStatus:
    """Return whether ``bundle_path`` is currently attached, via ``hdiutil info``.

    ``hdiutil info`` needs no passphrase, so it runs without a key. Parses the
    ``-plist`` output and resolves the entry matching ``bundle_path``.

    Raises:
        ContainerCorruptError: unparseable ``-plist`` output.
        ContainerError: any ``hdiutil info`` failure.
    """
    result = _run_hdiutil(["info", "-plist"], timeout=timeout)
    return parse_info_plist(result.stdout, bundle_path)


# ---------------------------------------------------------------------------
# Per-attach host-leak hardening (KTD-9)
# ---------------------------------------------------------------------------


def harden_mount(mountpoint: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
    """Re-assert host-leak hardening on a freshly attached volume (KTD-9).

    Runs on **every** attach, not once, because macOS silently re-enables
    Spotlight indexing after OS updates:

    1. ``mdutil -i off <mountpoint>`` to disable Spotlight indexing.
    2. ``mdutil -s <mountpoint>`` to verify it took; retried once if it still
       reports enabled.
    3. Ensure ``<mountpoint>/.fseventsd/no_log`` exists so FSEvents keeps no log
       for the volume.

    Fail-open on the ``mdutil`` disable/verify (indexing hardening is defense in
    depth, not a data-integrity gate) but raises if the ``.fseventsd/no_log``
    sentinel cannot be created — that is a filesystem-level problem worth
    surfacing.

    Raises:
        ContainerError: if the ``.fseventsd/no_log`` sentinel cannot be written.
    """
    # 1 + 2: disable indexing and verify, re-asserting once if needed.
    for _attempt in range(2):
        _run_cmd(["mdutil", "-i", "off", mountpoint], timeout=timeout)
        probe = _run_cmd(["mdutil", "-s", mountpoint], timeout=timeout)
        if not _mdutil_indexing_enabled(probe.stdout.decode("utf-8", "replace")):
            break

    # 3: FSEvents no-log sentinel at the volume root.
    fseventsd = os.path.join(mountpoint, ".fseventsd")
    no_log = os.path.join(fseventsd, "no_log")
    try:
        os.makedirs(fseventsd, exist_ok=True)
        # Touch the sentinel (empty file) if missing.
        with open(no_log, "a"):
            pass
    except OSError as exc:
        raise ContainerError(
            f"could not create FSEvents no_log sentinel at {no_log}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# FileVault detection (U7 — warn-only, KTD-11, R11/R15)
# ---------------------------------------------------------------------------
#
# A live, warn-only at-rest signal, checked at each daemon start (NOT cached from
# install time): ``fdesetup status`` runs unprivileged (verified on this
# hardware). The result is surfaced on ``daemon.info`` and rendered by
# ``screencap status`` only when FileVault is OFF. Nothing here ever blocks or
# refuses recording — a machine with FileVault off records exactly as before.
#
# **Strictly fail-open.** Any non-zero exit, timeout, unexpected/garbled output,
# or subprocess error degrades to :data:`FileVaultStatus.UNKNOWN`; this function
# never raises, so a broken check can never block daemon startup.

_FDESETUP_TIMEOUT = 10.0
"""Wall-clock timeout for the ``fdesetup status`` probe, in seconds. Short: this
is a warn-only signal on the startup path, never worth stalling boot for."""


class FileVaultStatus(str, Enum):
    """The tri-state result of the ``fdesetup status`` FileVault probe.

    ``str``-valued so the enum member serializes directly onto the ``daemon.info``
    envelope (``FileVaultStatus.OFF.value == "off"``) with no extra mapping.
    ``UNKNOWN`` is the fail-open sentinel — it is NOT alarming and renders no
    warning; only ``OFF`` warrants the warn-only surface.
    """

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


def _parse_fdesetup_status(text: str) -> FileVaultStatus:
    """Map ``fdesetup status`` stdout to a :class:`FileVaultStatus`.

    ``fdesetup status`` prints ``FileVault is On.`` or ``FileVault is Off.`` (with
    extra ``Encryption in progress`` / ``Decryption in progress`` lines during a
    transition — the leading ``FileVault is On./Off.`` line still resolves those).
    Anything else (empty, garbled, an unexpected future format) is ``UNKNOWN`` so
    the caller stays fail-open rather than guessing.
    """
    low = text.lower()
    if "filevault is on" in low:
        return FileVaultStatus.ON
    if "filevault is off" in low:
        return FileVaultStatus.OFF
    return FileVaultStatus.UNKNOWN


def filevault_status(*, timeout: float = _FDESETUP_TIMEOUT) -> FileVaultStatus:
    """Live-check FileVault via ``fdesetup status``; fail-open to ``UNKNOWN``.

    Runs the unprivileged ``fdesetup status`` command and parses its text output.
    **Warn-only and strictly fail-open (KTD-11, R11/R15):** any non-zero exit,
    timeout, subprocess failure, or unexpected output returns
    :data:`FileVaultStatus.UNKNOWN`. This function **never raises** — a broken or
    slow check must never block daemon startup or refuse recording.

    Args:
        timeout: subprocess wall-clock timeout in seconds.

    Returns:
        :data:`FileVaultStatus.ON` / ``OFF`` on a clean parse, else
        :data:`FileVaultStatus.UNKNOWN`.
    """
    import logging

    try:
        result = _run_cmd(["fdesetup", "status"], timeout=timeout)
    except Exception:  # noqa: BLE001 - warn-only probe must never raise (fail-open)
        logging.getLogger(__name__).debug("fdesetup status probe failed", exc_info=True)
        return FileVaultStatus.UNKNOWN
    if result.returncode != 0:
        return FileVaultStatus.UNKNOWN
    return _parse_fdesetup_status(result.stdout.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Container-key management (U2 — read-only get, never-orphan create, KTD-22)
# ---------------------------------------------------------------------------
#
# Mirrors the corpus-key channel structure (key-file env → shared-group Keychain)
# from ``corpus_crypto.py`` and the SCR-241 shared-access-group custody, with the
# KTD-22 divergence: on ``errSecMissingEntitlement`` the container key is NEVER
# silently read from a separate legacy ``keyring`` item (that would hand back a
# *different* key and leave the real store unmountable). The mismatch is surfaced
# so the caller can render the entitlement diagnosis. Un-entitled binaries are a
# documented *unsupported* container consumer.
#
# Losing the key makes the store unreadable (the video is untouched, but the KEK
# is not re-derivable), so :func:`get_container_key` is strictly read-only and
# never regenerates; only :func:`create_container_key` mints a key, and it refuses
# when a bundle already exists on disk.


def _encode_key(key: bytes) -> str:
    import base64

    return base64.b64encode(key).decode("ascii")


def _decode_key(stored: str) -> bytes | None:
    """Decode a base64 key string; ``None`` (with a warning) if it is not a valid
    32-byte key — treated as "absent" so a corrupt value never masquerades as a
    good key and the caller degrades to the not-ready / diagnosis path."""
    import base64
    import logging

    try:
        raw = base64.b64decode(stored.encode("ascii"))
    except Exception:  # noqa: BLE001 — any decode failure is "not a usable key"
        logging.getLogger(__name__).warning(
            "container key: stored value is not valid base64; treating as absent"
        )
        return None
    if len(raw) != _CONTAINER_KEY_LEN:
        logging.getLogger(__name__).warning(
            "container key: stored value decodes to %d bytes (expected %d); treating as absent",
            len(raw),
            _CONTAINER_KEY_LEN,
        )
        return None
    return raw


def _read_key_file(path: str) -> bytes | None:
    try:
        with open(path, encoding="ascii") as fh:
            text = fh.read().strip()
    except OSError:
        return None
    return _decode_key(text) if text else None


def _write_key_file(path: str, key: bytes) -> None:
    """Write the base64 key to ``path`` at ``0600`` (create-or-truncate)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, _encode_key(key).encode("ascii"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)  # tighten if the file pre-existed with looser perms
    except OSError:
        pass


def _load_from_keychain() -> bytes | None:
    """Read the container key from the shared Keychain group; ``None`` if absent.

    Raises:
        ContainerKeyUnreachableError: the group read returned
            ``errSecMissingEntitlement`` (KTD-22 — surfaced, never fallen back).
        KeychainLockedError: the Keychain/keyring is locked (retryable, exit 75).
        ContainerOperatorError: any other unexpected Keychain backend failure.
    """
    import sys

    if sys.platform != "darwin":
        # No shared-group Keychain off-Mac; use plain keyring (test / non-mac dev).
        import keyring
        import keyring.errors

        try:
            stored = keyring.get_password(CONTAINER_KEYCHAIN_SERVICE, CONTAINER_KEYCHAIN_ACCOUNT)
        except keyring.errors.KeyringLocked as exc:
            raise KeychainLockedError(
                "the keyring is locked; the container key could not be read"
            ) from exc
        return _decode_key(stored) if stored else None

    from screencap import keychain_group

    try:
        stored = keychain_group.load(
            CONTAINER_KEYCHAIN_SERVICE, CONTAINER_KEYCHAIN_ACCOUNT, KEYCHAIN_ACCESS_GROUP
        )
    except keychain_group.MissingEntitlement as exc:
        # KTD-22: do NOT fall back to a separate legacy keyring item and hand back a
        # DIFFERENT key. Surface the mismatch for the entitlement diagnosis.
        raise ContainerKeyUnreachableError(_ENTITLEMENT_MISMATCH_MSG) from exc
    except keychain_group.KeychainError as exc:
        if exc.status == keychain_group.errSecInteractionNotAllowed:
            # The device/Keychain is locked — the group-load equivalent of
            # keyring's KeyringLocked. Retryable so launchd tries again (exit 75).
            raise KeychainLockedError(
                "the Keychain is locked; the container key could not be read"
            ) from exc
        raise ContainerOperatorError(
            f"could not read the container key from the shared Keychain group "
            f"(status {exc.status})"
        ) from exc
    return _decode_key(stored) if stored is not None else None


def get_container_key() -> bytes | None:
    """Read-only container-key lookup; ``None`` when genuinely absent. NEVER creates.

    Channel order (mirrors :func:`screencap.corpus_crypto.load_corpus_key`): the
    :data:`CONTAINER_KEY_FILE_ENV` file — the SOLE source when that env var is set
    (headless / test channel) — else the shared-group Keychain. A transiently
    missing or locked Keychain must never silently orphan the store, so this
    function does not regenerate; use :func:`create_container_key` to mint a first
    key.

    Raises:
        ContainerKeyUnreachableError: the shared-group read returned
            ``errSecMissingEntitlement`` (KTD-22 — this binary is not entitled;
            the key is intact but unreadable here). Surfaced, never swallowed.
        KeychainLockedError: the Keychain/keyring is locked (retryable, exit 75).
    """
    env_path = os.environ.get(CONTAINER_KEY_FILE_ENV)
    if env_path:
        return _read_key_file(env_path)
    return _load_from_keychain()


def require_container_key() -> bytes:
    """Resolve the key for a store **known to exist on disk**, or raise the
    cause-distinguishing hard stop (KTD-22). NEVER creates.

    This is the diagnosis helper the daemon-mount and CLI-funnel layers call at
    the bundle-exists-but-key-unreadable stop. It converts the three failure modes
    into the right typed error so the operator sees an accurate cause:

    * key present in the shared group but unreadable here →
      :class:`ContainerKeyUnreachableError` (entitlement mismatch, *not* data loss;
      points at the entitled app/CLI);
    * Keychain/keyring locked → :class:`KeychainLockedError` (retryable, exit 75);
    * no key in any channel → :class:`ContainerKeyMissingError` (genuine loss, exit 1).

    Precondition: the caller has already confirmed the bundle exists (this helper
    never touches the bundle bytes).
    """
    key = get_container_key()  # raises the unreachable / locked cases
    if key is None:
        raise ContainerKeyMissingError(_NO_KEY_ANYWHERE_MSG)
    return key


def _persist_container_key(key: bytes) -> None:
    """Store ``key`` on the first available channel, or raise a typed failure.

    Same channel order as :func:`get_container_key`: the key-file env path when set,
    else the shared Keychain group (else plain ``keyring`` off-Mac).
    """
    import sys

    env_path = os.environ.get(CONTAINER_KEY_FILE_ENV)
    if env_path:
        try:
            _write_key_file(env_path, key)
            return
        except OSError as exc:
            raise ContainerOperatorError(
                f"could not write the container key file {env_path!r}: {exc}"
            ) from exc

    encoded = _encode_key(key)
    if sys.platform != "darwin":
        import keyring
        import keyring.errors

        try:
            keyring.set_password(CONTAINER_KEYCHAIN_SERVICE, CONTAINER_KEYCHAIN_ACCOUNT, encoded)
            return
        except keyring.errors.KeyringLocked as exc:
            raise KeychainLockedError(
                "the keyring is locked; the container key could not be stored"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — any backend failure = can't persist
            raise ContainerOperatorError(
                f"could not store the container key in keyring: {exc}"
            ) from exc

    from screencap import keychain_group

    try:
        # Device-local (synchronizable=False → never iCloud-synced) + readable
        # after first unlock (keychain_group's fixed accessibility) so the entitled
        # all-day daemon reads it headlessly.
        keychain_group.store(
            CONTAINER_KEYCHAIN_SERVICE, CONTAINER_KEYCHAIN_ACCOUNT, encoded, KEYCHAIN_ACCESS_GROUP
        )
    except keychain_group.MissingEntitlement as exc:
        raise ContainerKeyUnreachableError(_ENTITLEMENT_MISMATCH_MSG) from exc
    except keychain_group.KeychainError as exc:
        if exc.status == keychain_group.errSecInteractionNotAllowed:
            raise KeychainLockedError(
                "the Keychain is locked; the container key could not be stored"
            ) from exc
        raise ContainerOperatorError(
            f"could not store the container key in the shared Keychain group "
            f"(status {exc.status})"
        ) from exc


def create_container_key(bundle_path: str) -> bytes:
    """Mint + persist a fresh 256-bit container key; return the raw key bytes.

    **Refuses (raises) when ``bundle_path`` already exists on disk** — minting a
    new key for an existing store would orphan every recording inside it, so a
    bundle-present create is a hard error, never a silent overwrite (KTD-5).

    **Foreground-only (caller contract).** The first ``keychain_group.store`` from
    the packaged daemon triggers one visible ACL prompt, so only an interactive
    entry point may call this — the base plan's ``serve --install`` / ``storage
    init`` do, in their foreground CLI process. The daemon-spawned engine
    subprocess never creates (or reads) the key.

    Raises:
        ContainerError: a typed subclass — an operator error when a bundle already
            exists, :class:`ContainerKeyUnreachableError` /
            :class:`KeychainLockedError` / :class:`ContainerOperatorError` when the
            fresh key cannot be persisted.
    """
    import secrets

    if os.path.exists(bundle_path):
        raise ContainerOperatorError(
            f"refusing to create a new container key: {bundle_path!r} already exists "
            "(a new key would orphan the existing encrypted store)"
        )
    key = secrets.token_bytes(_CONTAINER_KEY_LEN)
    _persist_container_key(key)
    return key


__all__ = [
    # constants
    "BUNDLE_NAME",
    "DEFAULT_VOLUME_NAME",
    "DEFAULT_SPARSE_BAND_SIZE_SECTORS",
    "CONTAINER_KEYCHAIN_SERVICE",
    "CONTAINER_KEYCHAIN_ACCOUNT",
    "CONTAINER_KEY_FILE_ENV",
    "KEYCHAIN_ACCESS_GROUP",
    # exceptions
    "ContainerError",
    "ContainerRetryableError",
    "ContainerBusyError",
    "KeychainLockedError",
    "ContainerOperatorError",
    "ContainerAuthError",
    "ContainerCorruptError",
    "RogueMountpointError",
    "ContainerKeyMissingError",
    "ContainerKeyUnreachableError",
    # value objects
    "AttachInfo",
    "ContainerStatus",
    "FileVaultStatus",
    # FileVault (warn-only)
    "filevault_status",
    # sizing
    "host_volume_capacity_bytes",
    # wrappers
    "create_bundle",
    "attach",
    "detach",
    "compact",
    "status",
    # hardening
    "harden_mount",
    # key management
    "get_container_key",
    "require_container_key",
    "create_container_key",
    # parsing (exposed for tests + higher layers)
    "parse_attach_plist",
    "parse_info_plist",
]
