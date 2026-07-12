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
    hard stop, not a create.
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
    not "disabled") counts as enabled. Ambiguous/empty output is reported as
    still-enabled so the caller re-asserts rather than trusting a blank result.
    """
    low = text.lower()
    if "disabled" in low:
        return False
    if "enabled" in low:
        return True
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
) -> None:
    """Detach ``target`` (a mountpoint or ``/dev/diskN``), graceful then force.

    KTD-10: a plain ``hdiutil detach`` is attempted first. On a **transient**
    failure (a busy volume — :class:`ContainerBusyError`) we wait ``force_wait``
    seconds and retry with ``-force``. A non-transient failure propagates
    unchanged — we do not force past, say, a corruption error.

    Raises:
        ContainerError: if the graceful detach fails non-transiently, or if the
            forced detach also fails.
    """
    import time

    try:
        _run_hdiutil(["detach", target], timeout=timeout)
        return
    except ContainerBusyError:
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


__all__ = [
    # constants
    "BUNDLE_NAME",
    "DEFAULT_VOLUME_NAME",
    "DEFAULT_SPARSE_BAND_SIZE_SECTORS",
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
    # value objects
    "AttachInfo",
    "ContainerStatus",
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
    # parsing (exposed for tests + higher layers)
    "parse_attach_plist",
    "parse_info_plist",
]
