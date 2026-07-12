"""Tests for :mod:`screencap.container` — the ``hdiutil`` sparse-bundle primitives.

Two layers:

* **Pure-logic tests** (``@pytest.mark.privacy``, Vision-free — these run on CI):
  the passphrase helper never appends a newline, ``-plist`` parsing, the
  exception taxonomy (auth / corrupt / busy), and the graceful-then-force detach
  ordering. Every one mocks ``subprocess`` — no real ``hdiutil`` runs here.
* **On-hardware spike harness** (``@pytest.mark.macos_hw`` — *not* run by the CI
  ``-m privacy`` lane): the real create→attach→write→read→detach roundtrip and
  the KTD spikes. A human runs these later on a real Mac.

------------------------------------------------------------------------------
ON-HARDWARE SPIKE RESULTS (U1)
------------------------------------------------------------------------------
Record the outcome of each ``@pytest.mark.macos_hw`` test here after running
``PYTHONPATH=src python -m pytest tests/test_container.py -m macos_hw`` on real
hardware. Downstream units (U4+) assume these results.

* Roundtrip (create/attach/write/read/detach):        PENDING
* AE1 — detached bands contain no plaintext sentinel:  PENDING
* flock canary (mutual exclusion on mounted volume):   PENDING
* kill -9 WAL crash safety + force-detach + fsck:       PENDING
* compact shrinks APFS-in-bundle bands after deletes:   PENDING
* mounted-era backup file-copy restores + fscks:        PENDING
* capture-shaped write benchmark vs plaintext baseline: PENDING (gates R7)
* near-full host write failure mode (KTD-13):           PENDING
* Keychain portability across Migration Assistant/TM:   PENDING (manual)
------------------------------------------------------------------------------
"""

from __future__ import annotations

import plistlib
import subprocess
import sys

import pytest

from screencap import container

# ===========================================================================
# Test doubles for subprocess
# ===========================================================================


def _install_popen(monkeypatch, *, stdout=b"", stderr=b"", returncode=0, timeout=False):
    """Patch ``subprocess.Popen`` with a fake and return a recorder dict.

    The recorder captures the exact bytes piped to ``communicate(input=...)`` and
    the argv, so a test can assert the passphrase was piped verbatim.
    """
    rec: dict = {"input": "UNSET", "argv": None, "kills": 0, "comm_calls": 0}

    class _FakePopen:
        def __init__(self, argv, stdin=None, stdout=None, stderr=None):
            rec["argv"] = argv
            rec["stdin_mode"] = stdin
            self.returncode = returncode

        def communicate(self, input=None, timeout=None):
            rec["comm_calls"] += 1
            if timeout and rec["comm_calls"] == 1:
                # honor the requested-timeout signal only if the test asked
                pass
            if _FakePopen._timeout and rec["comm_calls"] == 1:
                raise subprocess.TimeoutExpired(cmd="hdiutil", timeout=timeout)
            rec["input"] = input
            return (stdout, stderr)

        def kill(self):
            rec["kills"] += 1

    _FakePopen._timeout = timeout
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return rec


def _attach_plist(mountpoint="/Users/x/.screencap/recordings", device="/dev/disk9"):
    """Build a representative ``hdiutil attach -plist`` payload."""
    return plistlib.dumps(
        {
            "system-entities": [
                {"content-hint": "GUID_partition_scheme", "dev-entry": device},
                {
                    "content-hint": "Apple_APFS",
                    "dev-entry": device + "s1",
                    "mount-point": mountpoint,
                },
            ]
        }
    )


def _info_plist(bundle_path, mountpoint="/Users/x/.screencap/recordings", device="/dev/disk9"):
    """Build a representative ``hdiutil info -plist`` payload listing one image."""
    return plistlib.dumps(
        {
            "images": [
                {
                    "image-path": bundle_path,
                    "system-entities": [
                        {"dev-entry": device},
                        {"dev-entry": device + "s1", "mount-point": mountpoint},
                    ],
                }
            ]
        }
    )


# ===========================================================================
# Pure-logic tests (CI lane) — passphrase helper (KTD-6)
# ===========================================================================


@pytest.mark.privacy
def test_passphrase_piped_verbatim_no_trailing_newline(monkeypatch):
    """The shared helper pipes the key exactly, appending no newline (KTD-6)."""
    key = b"c2NyZWVuY2FwLWtleQ=="  # base64-ish; deliberately no trailing \n
    rec = _install_popen(monkeypatch, stdout=_attach_plist())

    container.attach("/tmp/store.sparsebundle", key, "/tmp/mp")

    assert rec["input"] == key
    assert not rec["input"].endswith(b"\n")
    # The child's stdin was actually opened as a pipe.
    assert rec["stdin_mode"] == subprocess.PIPE


@pytest.mark.privacy
@pytest.mark.parametrize(
    "call",
    [
        lambda k: container.attach("/tmp/b.sparsebundle", k, "/tmp/mp"),
        lambda k: container.create_bundle(
            "/tmp/b.sparsebundle", k, size_bytes=1_000_000
        ),
        lambda k: container.compact("/tmp/b.sparsebundle", k),
    ],
)
def test_every_key_piping_wrapper_uses_the_shared_helper(monkeypatch, call):
    """create / attach / compact all pipe through the one helper, no newline."""
    key = b"raw-passphrase-bytes"
    rec = _install_popen(monkeypatch, stdout=_attach_plist())

    call(key)

    assert rec["input"] == key
    assert not rec["input"].endswith(b"\n")


@pytest.mark.privacy
def test_status_and_detach_run_without_a_key(monkeypatch):
    """``info``/``detach`` take no passphrase — nothing is piped to stdin."""
    rec = _install_popen(monkeypatch, stdout=_info_plist("/tmp/b.sparsebundle"))
    container.status("/tmp/b.sparsebundle")
    assert rec["input"] is None
    assert rec["stdin_mode"] == subprocess.DEVNULL


# ===========================================================================
# Pure-logic tests — plist parsing
# ===========================================================================


@pytest.mark.privacy
def test_parse_attach_plist_extracts_mountpoint_and_device():
    info = container.parse_attach_plist(_attach_plist(mountpoint="/mnt/rec", device="/dev/disk9"))
    assert info.mountpoint == "/mnt/rec"
    # Whole-disk device, not the partition slice.
    assert info.device == "/dev/disk9"


@pytest.mark.privacy
def test_status_reports_mounted_for_matching_bundle(monkeypatch):
    bundle = "/tmp/b.sparsebundle"
    _install_popen(monkeypatch, stdout=_info_plist(bundle, mountpoint="/mnt/rec"))
    st = container.status(bundle)
    assert st.attached is True
    assert st.mountpoint == "/mnt/rec"


@pytest.mark.privacy
def test_status_reports_unmounted_when_bundle_absent_from_info(monkeypatch):
    # info lists a *different* image -> our bundle is not attached.
    _install_popen(monkeypatch, stdout=_info_plist("/tmp/other.sparsebundle"))
    st = container.status("/tmp/b.sparsebundle")
    assert st.attached is False
    assert st.mountpoint is None
    assert st.device is None


# ===========================================================================
# Pure-logic tests — exception taxonomy (KTD-10)
# ===========================================================================


@pytest.mark.privacy
def test_wrong_passphrase_maps_to_typed_auth_error(monkeypatch):
    _install_popen(
        monkeypatch,
        returncode=1,
        stderr=b"hdiutil: attach failed - Authentication error",
    )
    with pytest.raises(container.ContainerAuthError) as exc:
        container.attach("/tmp/b.sparsebundle", b"badkey", "/tmp/mp")
    assert exc.value.retryable is False
    assert exc.value.exit_code == 1


@pytest.mark.privacy
def test_busy_attach_maps_to_retryable_error(monkeypatch):
    _install_popen(
        monkeypatch,
        returncode=16,
        stderr=b"hdiutil: attach failed - Resource busy",
    )
    with pytest.raises(container.ContainerBusyError) as exc:
        container.attach("/tmp/b.sparsebundle", b"key", "/tmp/mp")
    assert isinstance(exc.value, container.ContainerRetryableError)
    assert exc.value.retryable is True
    assert exc.value.exit_code == 75  # EX_TEMPFAIL


@pytest.mark.privacy
def test_malformed_attach_plist_maps_to_corruption_error():
    with pytest.raises(container.ContainerCorruptError):
        container.parse_attach_plist(b"this is not a plist at all")


@pytest.mark.privacy
def test_malformed_info_plist_maps_to_corruption_error(monkeypatch):
    _install_popen(monkeypatch, stdout=b"<garbage>not-a-plist")
    with pytest.raises(container.ContainerCorruptError):
        container.status("/tmp/b.sparsebundle")


@pytest.mark.privacy
def test_generic_failure_maps_to_operator_error(monkeypatch):
    _install_popen(
        monkeypatch,
        returncode=1,
        stderr=b"hdiutil: attach failed - some unrecognized problem",
    )
    with pytest.raises(container.ContainerOperatorError) as exc:
        container.attach("/tmp/b.sparsebundle", b"key", "/tmp/mp")
    # Not a more specific subclass.
    assert type(exc.value) is container.ContainerOperatorError
    assert exc.value.retryable is False


@pytest.mark.privacy
def test_timeout_maps_to_retryable_busy(monkeypatch):
    _install_popen(monkeypatch, timeout=True)
    with pytest.raises(container.ContainerBusyError):
        container.attach("/tmp/b.sparsebundle", b"key", "/tmp/mp", timeout=0.01)


@pytest.mark.privacy
def test_exception_flags_are_consistent():
    """Retryable subclasses exit 75; operator subclasses exit 1."""
    for cls in (
        container.ContainerBusyError,
        container.KeychainLockedError,
        container.ContainerRetryableError,
    ):
        assert cls.retryable is True
        assert cls.exit_code == 75
    for cls in (
        container.ContainerAuthError,
        container.ContainerCorruptError,
        container.RogueMountpointError,
        container.ContainerKeyMissingError,
        container.ContainerOperatorError,
    ):
        assert cls.retryable is False
        assert cls.exit_code == 1


# ===========================================================================
# Pure-logic tests — graceful-then-force detach ordering (KTD-10)
# ===========================================================================


@pytest.mark.privacy
def test_detach_forces_after_wait_on_transient_failure(monkeypatch):
    """Graceful detach fails busy -> wait ~3 s -> force. Order is asserted."""
    import time

    events: list = []

    class _DetachPopen:
        def __init__(self, argv, stdin=None, stdout=None, stderr=None):
            self.argv = argv
            events.append(("popen", list(argv)))
            if "-force" in argv:
                self.returncode = 0
                self._err = b""
            else:
                self.returncode = 16
                self._err = b"hdiutil: detach failed - Resource busy"

        def communicate(self, input=None, timeout=None):
            return (b"", self._err)

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _DetachPopen)
    monkeypatch.setattr(time, "sleep", lambda s: events.append(("sleep", s)))

    container.detach("/tmp/mp", force_wait=3.0)

    assert events == [
        ("popen", ["hdiutil", "detach", "/tmp/mp"]),
        ("sleep", 3.0),
        ("popen", ["hdiutil", "detach", "-force", "/tmp/mp"]),
    ]


@pytest.mark.privacy
def test_detach_does_not_force_on_non_transient_failure(monkeypatch):
    """A non-busy graceful failure propagates; ``-force`` is never attempted."""
    calls: list = []

    class _DetachPopen:
        def __init__(self, argv, stdin=None, stdout=None, stderr=None):
            calls.append(list(argv))
            self.returncode = 1
            self._err = b"hdiutil: detach failed - image not recognized"

        def communicate(self, input=None, timeout=None):
            return (b"", self._err)

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _DetachPopen)

    with pytest.raises(container.ContainerCorruptError):
        container.detach("/tmp/mp")

    # Only the graceful attempt ran; no force.
    assert calls == [["hdiutil", "detach", "/tmp/mp"]]


# ===========================================================================
# Pure-logic tests — mdutil verify parser (KTD-9) + sizing (KTD-13)
# ===========================================================================


@pytest.mark.privacy
@pytest.mark.parametrize(
    "text,enabled",
    [
        ("Indexing enabled.", True),
        ("Indexing disabled.", False),
        ("Indexing and searching disabled.", False),
        ("", True),  # unparseable -> assume still enabled (re-assert)
        ("some unexpected output", True),
    ],
)
def test_mdutil_status_parser(text, enabled):
    assert container._mdutil_indexing_enabled(text) is enabled


@pytest.mark.privacy
def test_host_volume_capacity_positive(tmp_path):
    """Declared-size source (KTD-13) resolves against an existing ancestor even
    when the bundle path itself does not exist yet."""
    not_yet = tmp_path / "sub" / "store.sparsebundle"
    cap = container.host_volume_capacity_bytes(str(not_yet))
    assert isinstance(cap, int)
    assert cap > 0


@pytest.mark.privacy
def test_create_bundle_declares_host_capacity_by_default(monkeypatch, tmp_path):
    """With no explicit size, create declares the host volume capacity (KTD-13)."""
    rec = _install_popen(monkeypatch)
    monkeypatch.setattr(container, "host_volume_capacity_bytes", lambda p: 123_456_789)

    container.create_bundle(str(tmp_path / "store.sparsebundle"), b"key")

    argv = rec["argv"]
    assert "-size" in argv
    assert argv[argv.index("-size") + 1] == "123456789b"
    # AES-256 + APFS + tuned band size are all present.
    assert "AES-256" in argv
    assert "APFS" in argv
    band = f"sparse-band-size={container.DEFAULT_SPARSE_BAND_SIZE_SECTORS}"
    assert band in argv


# ===========================================================================
# On-hardware spike harness (@pytest.mark.macos_hw) — SKIPPED by the CI lane.
#
# These run real hdiutil on a real Mac. They are deselected by `-m privacy`.
# Record outcomes in this file's module docstring after running them.
# ===========================================================================

_DARWIN = sys.platform == "darwin"
_needs_darwin = pytest.mark.skipif(not _DARWIN, reason="hdiutil is macOS-only")


def _mk_key() -> bytes:
    import secrets

    return secrets.token_hex(32).encode("ascii")


@pytest.mark.macos_hw
@_needs_darwin
def test_hw_create_attach_write_read_detach_roundtrip(tmp_path):
    """Real create -> attach -> write/read a file -> detach; status transitions."""
    bundle = str(tmp_path / "store.sparsebundle")
    mount = str(tmp_path / "mnt")
    import os

    os.makedirs(mount, exist_ok=True)
    key = _mk_key()

    container.create_bundle(bundle, key, size_bytes=64 * 1024 * 1024)
    assert container.status(bundle).attached is False

    info = container.attach(bundle, key, mount)
    try:
        assert info.mountpoint == mount
        st = container.status(bundle)
        assert st.attached is True and st.mountpoint == mount

        payload = b"roundtrip-payload"
        f = os.path.join(mount, "probe.bin")
        with open(f, "wb") as fh:
            fh.write(payload)
        with open(f, "rb") as fh:
            assert fh.read() == payload
    finally:
        container.detach(mount)

    assert container.status(bundle).attached is False


@pytest.mark.macos_hw
@_needs_darwin
def test_hw_detached_bands_have_no_plaintext_sentinel(tmp_path):
    """AE1: with the bundle detached, no band file contains the plaintext
    sentinel written into the mounted volume."""
    import os

    bundle = str(tmp_path / "store.sparsebundle")
    mount = str(tmp_path / "mnt")
    os.makedirs(mount, exist_ok=True)
    key = _mk_key()
    sentinel = b"SENTINEL-" + os.urandom(8).hex().encode("ascii") + b"-AE1"

    container.create_bundle(bundle, key, size_bytes=64 * 1024 * 1024)
    container.attach(bundle, key, mount)
    try:
        with open(os.path.join(mount, "secret.txt"), "wb") as fh:
            fh.write(sentinel * 128)
    finally:
        container.detach(mount)

    bands_dir = os.path.join(bundle, "bands")
    hits = []
    for name in os.listdir(bands_dir):
        with open(os.path.join(bands_dir, name), "rb") as fh:
            if sentinel in fh.read():
                hits.append(name)
    assert hits == [], f"plaintext sentinel leaked into bands: {hits}"


@pytest.mark.macos_hw
@_needs_darwin
def test_hw_flock_canary_is_mutually_exclusive(tmp_path):
    """Spike: ``fcntl.flock`` on a file inside the mounted volume is a real mutex
    across processes (guards terminal_stage's locking)."""
    import fcntl
    import multiprocessing as mp
    import os
    import time

    bundle = str(tmp_path / "store.sparsebundle")
    mount = str(tmp_path / "mnt")
    os.makedirs(mount, exist_ok=True)
    key = _mk_key()
    container.create_bundle(bundle, key, size_bytes=64 * 1024 * 1024)
    container.attach(bundle, key, mount)

    lock_path = os.path.join(mount, "canary.lock")
    log_path = str(tmp_path / "order.log")

    def _worker(tag):
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with open(log_path, "a") as lg:
            lg.write(f"{tag}-in\n")
        time.sleep(0.4)
        with open(log_path, "a") as lg:
            lg.write(f"{tag}-out\n")
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    try:
        p1 = mp.get_context("spawn").Process(target=_worker, args=("A",))
        p2 = mp.get_context("spawn").Process(target=_worker, args=("B",))
        p1.start()
        time.sleep(0.05)
        p2.start()
        p1.join(10)
        p2.join(10)
        with open(log_path) as lg:
            seq = [ln.strip() for ln in lg if ln.strip()]
        # Whichever entered first must exit before the other enters.
        first = seq[0][0]
        assert seq == [f"{first}-in", f"{first}-out"] + [
            f"{'B' if first == 'A' else 'A'}-in",
            f"{'B' if first == 'A' else 'A'}-out",
        ]
    finally:
        container.detach(mount)


@pytest.mark.macos_hw
@_needs_darwin
def test_hw_kill9_wal_crash_safety(tmp_path):
    """Spike: kill -9 a writer mid-WAL-transaction, force-detach, re-attach, and
    assert SQLite recovers (integrity_check passes)."""
    import os
    import signal
    import sqlite3
    import subprocess as sp
    import time

    bundle = str(tmp_path / "store.sparsebundle")
    mount = str(tmp_path / "mnt")
    os.makedirs(mount, exist_ok=True)
    key = _mk_key()
    container.create_bundle(bundle, key, size_bytes=128 * 1024 * 1024)
    container.attach(bundle, key, mount)

    db = os.path.join(mount, "crash.db")
    writer = (
        "import sqlite3,sys,time;"
        f"c=sqlite3.connect({db!r});"
        "c.execute('PRAGMA journal_mode=WAL');"
        "c.execute('CREATE TABLE IF NOT EXISTS t(x)');"
        "c.commit();"
        "sys.stdout.write('ready\\n');sys.stdout.flush();"
        "[c.execute('INSERT INTO t VALUES (?)',(i,)) for i in range(100000)]"
    )
    proc = sp.Popen([sys.executable, "-c", writer], stdout=sp.PIPE)
    try:
        proc.stdout.readline()  # wait for 'ready'
        time.sleep(0.2)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(5)
        # Force-detach a busy volume, then re-attach.
        container.detach(mount)
    finally:
        if container.status(bundle).attached:
            container.detach(mount)

    container.attach(bundle, key, mount)
    try:
        c = sqlite3.connect(db)
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        c.close()
    finally:
        container.detach(mount)


@pytest.mark.macos_hw
@_needs_darwin
def test_hw_compact_shrinks_after_deletes(tmp_path):
    """Spike (KTD-12): ``hdiutil compact`` shrinks bands after in-volume deletes."""
    import os

    bundle = str(tmp_path / "store.sparsebundle")
    mount = str(tmp_path / "mnt")
    os.makedirs(mount, exist_ok=True)
    key = _mk_key()
    container.create_bundle(bundle, key, size_bytes=256 * 1024 * 1024)
    container.attach(bundle, key, mount)
    big = os.path.join(mount, "big.bin")
    with open(big, "wb") as fh:
        fh.write(os.urandom(80 * 1024 * 1024))
    container.detach(mount)

    def _bundle_bytes():
        total = 0
        for root, _dirs, files in os.walk(bundle):
            for n in files:
                total += os.path.getsize(os.path.join(root, n))
        return total

    grown = _bundle_bytes()

    container.attach(bundle, key, mount)
    os.remove(big)
    container.detach(mount)
    container.compact(bundle, key)

    assert _bundle_bytes() < grown


@pytest.mark.macos_hw
@_needs_darwin
def test_hw_mounted_era_file_copy_restores(tmp_path):
    """Spike: a file-copy of the bundle taken while mounted under writes restores
    and attaches + reads cleanly (scopes KTD-1's backup claim)."""
    import os
    import shutil

    bundle = str(tmp_path / "store.sparsebundle")
    mount = str(tmp_path / "mnt")
    os.makedirs(mount, exist_ok=True)
    key = _mk_key()
    container.create_bundle(bundle, key, size_bytes=64 * 1024 * 1024)
    container.attach(bundle, key, mount)
    with open(os.path.join(mount, "keep.txt"), "wb") as fh:
        fh.write(b"restore-me")
        fh.flush()
        os.fsync(fh.fileno())
    copy = str(tmp_path / "copy.sparsebundle")
    shutil.copytree(bundle, copy)  # mounted-era copy
    container.detach(mount)

    restore_mount = str(tmp_path / "restore-mnt")
    os.makedirs(restore_mount, exist_ok=True)
    container.attach(copy, key, restore_mount)
    try:
        with open(os.path.join(restore_mount, "keep.txt"), "rb") as fh:
            assert fh.read() == b"restore-me"
    finally:
        container.detach(restore_mount)


@pytest.mark.macos_hw
@_needs_darwin
def test_hw_hardening_disables_indexing_and_writes_no_log(tmp_path):
    """KTD-9: after attach + harden_mount, mdutil reports indexing disabled and
    ``.fseventsd/no_log`` exists at the volume root."""
    import os
    import subprocess as sp

    bundle = str(tmp_path / "store.sparsebundle")
    mount = str(tmp_path / "mnt")
    os.makedirs(mount, exist_ok=True)
    key = _mk_key()
    container.create_bundle(bundle, key, size_bytes=64 * 1024 * 1024)
    container.attach(bundle, key, mount)
    try:
        container.harden_mount(mount)
        out = sp.run(["mdutil", "-s", mount], capture_output=True, text=True).stdout
        assert not container._mdutil_indexing_enabled(out)
        assert os.path.exists(os.path.join(mount, ".fseventsd", "no_log"))
    finally:
        container.detach(mount)


@pytest.mark.macos_hw
@pytest.mark.skip(reason="near-full-host spike (KTD-13): needs a deliberately near-full host volume")
def test_hw_near_full_host_write_failure_mode(tmp_path):
    """Spike (KTD-13): characterize what happens when the mounted volume is
    written while the *host* disk is near capacity. Manual setup required."""
    raise NotImplementedError


@pytest.mark.macos_hw
@pytest.mark.skip(reason="Keychain-portability spike: needs Migration Assistant / full TM restore")
def test_hw_keychain_portability_across_machine_restore():
    """Spike: verify the container key entry survives Migration Assistant / a
    full Time Machine restore. Manual, cross-machine — cannot be automated."""
    raise NotImplementedError
