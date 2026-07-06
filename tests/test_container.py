"""U1 — encrypted sparse-bundle container primitives (SCR-236).

===========================================================================
U1 FEASIBILITY SPIKE RESULTS — recorded on real hardware (Execution note).
Downstream units (U2–U8) assume these outcomes. Re-run the harness with
``SCREENCAP_HW_SPIKE=1 pytest tests/test_container.py -m macos_hw`` (or the
standalone spike script) to reproduce.

Hardware: macOS 26.5.1 (build 25F80), Apple Silicon, APFS host.

| Spike check                       | Result                                    |
|-----------------------------------|-------------------------------------------|
| create AES-256 APFS sparsebundle  | PASS — 100 GiB declared, ~11 MiB on disk  |
|   (declared size is sparse-free)  |        (sparseness makes declaration free)|
| attach -nobrowse + plist parse    | PASS                                      |
| write/read through mountpoint     | PASS                                      |
| AE1: no plaintext in bands/        | PASS — sentinel absent from detached bands|
| flock mutual exclusion in volume  | PASS — 2nd holder BLOCKED (terminal_stage)|
| SQLite WAL + integrity_check       | PASS — journal_mode=wal, integrity=ok    |
| crash: kill -9 mid-WAL + force     | PASS — reattach integrity=ok, 331k rows,  |
|   detach + reattach + verifyVolume |        diskutil verifyVolume rc=0         |
| attach idempotency                | PASS — hdiutil info detects; naive 2nd    |
|                                   |        attach errors (rc=1) → guard first |
| passphrase trailing-newline (KTD-6)| PASS — no-newline attaches; "\\n" → rc=1 |
| wrong passphrase                  | PASS — "Authentication error", rc=1       |
| compact shrinks bands (KTD-12)    | PASS — needs -stdinpass; du shrank        |
| perf capture-shaped writes (R7)   | PASS — 1.68x on a fixed-cost microbench   |
|   (300KB frames + WAL commits)    |        (abs overhead negligible/frame)    |
| backup restorability (mounted-era)| PASS — file-copy attaches + verifies      |
| KTD-13 host-vs-volume free space  | PASS — distinct st_dev; statvfs(parent)   |
|                                   |        yields host free directly          |
| mdutil -i off + verify (KTD-9)    | PASS(runs) — macOS 26 `mdutil -s` reports |
|                                   |   "unknown indexing state" (non-indexing) |

Manual / hardware-dependent (NOT automated here — documented fallbacks):
* Keychain portability across Migration Assistant / full TM restore →
  U8 selective-restore warning + recovery-code fast-follow.
* Headless LaunchAgent mount at login → U4 manual verification.

No stop condition triggered: every load-bearing assumption held.
===========================================================================

The pure-logic tests below carry ``@pytest.mark.privacy`` (mock hdiutil,
Vision-free) so the CI privacy lane runs them. The real-hardware tests
carry ``@pytest.mark.macos_hw`` and skip unless ``SCREENCAP_HW_SPIKE`` is
set, so neither CI nor the default full-suite run spawns disk images.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from screencap import container

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _info_plist(image_path: str, mount_point: str | None) -> bytes:
    import plistlib

    entity: dict = {"dev-entry": "/dev/disk9s1"}
    if mount_point is not None:
        entity["mount-point"] = mount_point
    return plistlib.dumps({"images": [{"image-path": image_path, "system-entities": [entity]}]})


# ---------------------------------------------------------------------------
# Failure taxonomy (KTD-10) — exit codes
# ---------------------------------------------------------------------------


def test_exit_codes_split_retryable_vs_fatal():
    assert container.RetryableContainerError().exit_code == container.EX_TEMPFAIL == 75
    assert container.FatalContainerError().exit_code == 1
    # leaves inherit the right side of the split
    assert container.ContainerBusyError().exit_code == 75
    assert container.ContainerAuthError().exit_code == 1
    assert container.ContainerCorruptError().exit_code == 1
    assert container.RogueLockError().exit_code == 1
    # everything is a ContainerError
    for exc in (container.ContainerBusyError, container.ContainerAuthError):
        assert issubclass(exc, container.ContainerError)


# ---------------------------------------------------------------------------
# Passphrase helper (KTD-6)
# ---------------------------------------------------------------------------


def test_run_hdiutil_rejects_trailing_newline_key():
    with pytest.raises(ValueError, match="trailing newline"):
        container._run_hdiutil(["attach"], key=b"secret\n", timeout=5)


def test_run_hdiutil_pipes_key_verbatim(monkeypatch):
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return _FakeProc(0)

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    key = b"raw-key-no-newline"
    container._run_hdiutil(["create", "-stdinpass"], key=key, timeout=5)
    assert captured["input"] == key
    assert not captured["input"].endswith(b"\n")
    assert captured["cmd"][0] == "hdiutil"


def test_run_hdiutil_timeout_is_retryable(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="hdiutil", timeout=5)

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    with pytest.raises(container.ContainerBusyError):
        container._run_hdiutil(["attach"], timeout=5)


# ---------------------------------------------------------------------------
# Status / idempotency parsing (hdiutil info)
# ---------------------------------------------------------------------------


def test_container_mountpoint_matches_image_path(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    bundle.mkdir()
    plist = _info_plist(str(bundle), "/Volumes/ScreenCapStore")
    monkeypatch.setattr(container, "_run_hdiutil", lambda *a, **k: _FakeProc(0, plist))
    assert container.container_mountpoint(bundle) == "/Volumes/ScreenCapStore"
    assert container.is_attached(bundle) is True


def test_container_mountpoint_none_when_absent(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    other = _info_plist("/some/other.sparsebundle", "/Volumes/Other")
    monkeypatch.setattr(container, "_run_hdiutil", lambda *a, **k: _FakeProc(0, other))
    assert container.container_mountpoint(bundle) is None
    assert container.is_attached(bundle) is False


def test_container_mountpoint_empty_when_attached_not_mounted(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    bundle.mkdir()
    plist = _info_plist(str(bundle), None)  # attached, no mount-point
    monkeypatch.setattr(container, "_run_hdiutil", lambda *a, **k: _FakeProc(0, plist))
    assert container.container_mountpoint(bundle) == ""


def test_container_mountpoint_corrupt_info_raises(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    monkeypatch.setattr(container, "_run_hdiutil", lambda *a, **k: _FakeProc(0, b"not a plist"))
    with pytest.raises(container.ContainerCorruptError):
        container.container_mountpoint(bundle)


def test_container_mountpoint_info_failure_returns_none(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    monkeypatch.setattr(container, "_run_hdiutil", lambda *a, **k: _FakeProc(1, b"", b"boom"))
    assert container.container_mountpoint(bundle) is None


# ---------------------------------------------------------------------------
# Attach failure classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (b"hdiutil: attach failed - Authentication error", container.ContainerAuthError),
        (b"hdiutil: attach failed - image not recognized", container.ContainerCorruptError),
        (b"hdiutil: attach failed - Resource busy", container.ContainerBusyError),
        (b"hdiutil: attach failed - some novel failure", container.FatalContainerError),
    ],
)
def test_classify_attach_failure(stderr, expected):
    exc = container._classify_attach_failure(_FakeProc(1, b"", stderr))
    assert isinstance(exc, expected)


# ---------------------------------------------------------------------------
# create / attach / detach / compact
# ---------------------------------------------------------------------------


def test_create_refuses_existing_bundle(tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    bundle.mkdir()
    with pytest.raises(container.FatalContainerError, match="already exists"):
        container.create_container(bundle, b"key", size="1g")


def test_create_builds_encrypted_apfs_args(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    captured = {}

    def fake_run(args, *, key=None, timeout):
        captured["args"] = args
        captured["key"] = key
        return _FakeProc(0)

    monkeypatch.setattr(container, "_run_hdiutil", fake_run)
    container.create_container(bundle, b"the-key", size="500g")
    args = captured["args"]
    assert args[0] == "create"
    assert "-encryption" in args and "AES-256" in args
    assert "APFS" in args and "SPARSEBUNDLE" in args
    assert f"sparse-band-size={container.SPARSE_BAND_SIZE_SECTORS}" in args
    assert "500g" in args
    assert captured["key"] == b"the-key"


def test_create_failure_raises_fatal(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    monkeypatch.setattr(container, "_run_hdiutil", lambda *a, **k: _FakeProc(1, b"", b"disk full"))
    with pytest.raises(container.FatalContainerError, match="create failed"):
        container.create_container(bundle, b"key", size="1g")


def test_attach_reuses_existing_mount(monkeypatch, tmp_path):
    """Idempotency: an already-attached bundle is reused, never re-attached."""
    bundle = tmp_path / "store.sparsebundle"
    hardened = []
    monkeypatch.setattr(container, "container_mountpoint", lambda b: "/Volumes/ScreenCapStore")
    monkeypatch.setattr(container, "harden_mount", lambda mp: hardened.append(mp))

    def fail_if_called(*a, **k):
        raise AssertionError("must not attach when already mounted")

    monkeypatch.setattr(container, "_run_hdiutil", fail_if_called)
    assert container.attach_container(bundle, b"key", tmp_path / "mp") == "/Volumes/ScreenCapStore"
    assert hardened == ["/Volumes/ScreenCapStore"]


def test_attach_auth_failure_classified(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    monkeypatch.setattr(container, "container_mountpoint", lambda b: None)
    monkeypatch.setattr(
        container,
        "_run_hdiutil",
        lambda *a, **k: _FakeProc(1, b"", b"hdiutil: attach failed - Authentication error"),
    )
    with pytest.raises(container.ContainerAuthError):
        container.attach_container(bundle, b"key", tmp_path / "mp")


def test_attach_success_hardens_and_returns_plist_mountpoint(monkeypatch, tmp_path):
    import plistlib

    bundle = tmp_path / "store.sparsebundle"
    mp = tmp_path / "recordings"
    attach_plist = plistlib.dumps(
        {"system-entities": [{"mount-point": str(mp)}]}
    )
    hardened = []
    monkeypatch.setattr(container, "container_mountpoint", lambda b: None)
    monkeypatch.setattr(container, "harden_mount", lambda p: hardened.append(p))
    monkeypatch.setattr(container, "_run_hdiutil", lambda *a, **k: _FakeProc(0, attach_plist))
    result = container.attach_container(bundle, b"key", mp)
    assert result == str(mp)
    assert hardened == [str(mp)]


def test_detach_graceful_then_force(monkeypatch):
    calls = []

    def fake_run(args, *, key=None, timeout):
        calls.append(args)
        # graceful (first) fails, force (second) succeeds
        if "-force" in args:
            return _FakeProc(0)
        return _FakeProc(1, b"", b"resource busy")

    monkeypatch.setattr(container, "_run_hdiutil", fake_run)
    monkeypatch.setattr(container.time, "sleep", lambda s: None)
    container.detach_container("/Volumes/ScreenCapStore", wait=0)
    assert calls[0] == ["detach", "/Volumes/ScreenCapStore"]
    assert calls[1] == ["detach", "-force", "/Volumes/ScreenCapStore"]


def test_detach_force_failure_is_retryable(monkeypatch):
    monkeypatch.setattr(container, "_run_hdiutil", lambda *a, **k: _FakeProc(1, b"", b"busy"))
    monkeypatch.setattr(container.time, "sleep", lambda s: None)
    with pytest.raises(container.ContainerBusyError):
        container.detach_container("/Volumes/ScreenCapStore", wait=0)


def test_compact_pipes_key_with_stdinpass(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    captured = {}

    def fake_run(args, *, key=None, timeout):
        captured["args"] = args
        captured["key"] = key
        return _FakeProc(0)

    monkeypatch.setattr(container, "_run_hdiutil", fake_run)
    monkeypatch.setattr(container, "_bundle_disk_usage_bytes", lambda b: 0)
    container.compact_container(bundle, b"key")
    assert "-stdinpass" in captured["args"]
    assert "compact" in captured["args"]
    assert captured["key"] == b"key"


def test_compact_on_attached_is_retryable(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    monkeypatch.setattr(container, "_bundle_disk_usage_bytes", lambda b: 0)
    monkeypatch.setattr(
        container, "_run_hdiutil", lambda *a, **k: _FakeProc(1, b"", b"resource is in use")
    )
    with pytest.raises(container.ContainerBusyError):
        container.compact_container(bundle, b"key")


# ---------------------------------------------------------------------------
# Host-volume resolver (KTD-13)
# ---------------------------------------------------------------------------


def test_host_backing_free_bytes_uses_bundle_parent(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    seen = {}

    class _SV:
        f_bavail = 1000
        f_frsize = 4096
        f_blocks = 5000

    def fake_statvfs(path):
        seen["path"] = path
        return _SV()

    monkeypatch.setattr(container.os, "statvfs", fake_statvfs)
    assert container.host_backing_free_bytes(bundle) == 1000 * 4096
    # resolves the HOST (bundle's parent), not the mounted volume
    assert seen["path"] == str(bundle.parent)


# ---------------------------------------------------------------------------
# Spotlight verification tolerance (KTD-9)
# ---------------------------------------------------------------------------


class _Text:
    """A text-mode CompletedProcess stand-in for ``mdutil -s``."""

    def __init__(self, stdout: str):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Indexing enabled.", True),
        ("Indexing disabled.", False),
        ("Error: unknown indexing state.", False),  # macOS 26 non-indexing
        ("", False),
    ],
)
def test_spotlight_indexing_enabled_tolerates_unknown(monkeypatch, text, expected):
    monkeypatch.setattr(container.subprocess, "run", lambda *a, **k: _Text(text))
    assert container.spotlight_indexing_enabled("/Volumes/X") is expected


def test_spotlight_indexing_enabled_none_on_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("mdutil missing")

    monkeypatch.setattr(container.subprocess, "run", boom)
    assert container.spotlight_indexing_enabled("/Volumes/X") is None


# ---------------------------------------------------------------------------
# Hardened lock open (KTD-3 symlink guard) — real filesystem, no hardware
# ---------------------------------------------------------------------------


def test_open_hardened_lock_normal_path(tmp_path):
    lock = tmp_path / "run" / "mount.lock"
    fd = container.open_hardened_lock(lock)
    try:
        assert lock.exists()
        assert oct(lock.stat().st_mode)[-3:] == "600"
    finally:
        os.close(fd)


def test_open_hardened_lock_rejects_symlinked_path(tmp_path):
    target = tmp_path / "real.lock"
    target.touch()
    lock = tmp_path / "mount.lock"
    lock.symlink_to(target)
    with pytest.raises(container.RogueLockError, match="symlink"):
        container.open_hardened_lock(lock)


def test_open_hardened_lock_rejects_symlinked_parent(tmp_path):
    real_parent = tmp_path / "real_run"
    real_parent.mkdir()
    link_parent = tmp_path / "run"
    link_parent.symlink_to(real_parent)
    with pytest.raises(container.RogueLockError, match="symlink"):
        container.open_hardened_lock(link_parent / "mount.lock")


# ===========================================================================
# Real-hardware spike tests (opt-in; skip unless SCREENCAP_HW_SPIKE=1).
# Not privacy-marked → CI's `-m privacy` skips them; the env guard keeps the
# default full-suite run from spawning disk images.
# ===========================================================================

_HW = pytest.mark.skipif(
    not os.environ.get("SCREENCAP_HW_SPIKE"),
    reason="real-hardware spike; set SCREENCAP_HW_SPIKE=1 to run",
)


@pytest.mark.macos_hw
@_HW
def test_hw_create_attach_roundtrip_and_ae1(tmp_path):
    """End-to-end: create → attach → write → detach → bands are ciphertext."""
    bundle = tmp_path / "store.sparsebundle"
    mp = tmp_path / "recordings"
    key = b"hw-spike-passphrase-1234567890"
    container.create_container(bundle, key, size="1g")
    try:
        mount = container.attach_container(bundle, key, mp)
        sentinel = "HW_SENTINEL_" + "deadbeefcafe"
        (Path(mount) / "secret.txt").write_text(sentinel + " confidential\n")
        subprocess.run(["sync"], check=False)
        container.detach_container(mount, wait=1)
        grep = subprocess.run(
            ["grep", "-rl", sentinel, str(bundle / "bands")],
            capture_output=True,
            text=True,
        )
        assert grep.returncode != 0, "plaintext sentinel leaked into bands"
    finally:
        existing = container.container_mountpoint(bundle)
        if existing:
            container.detach_container(existing, wait=1)


@pytest.mark.macos_hw
@_HW
def test_hw_wrong_key_raises_auth_error(tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    mp = tmp_path / "recordings"
    container.create_container(bundle, b"correct-key-1234567890", size="1g")
    with pytest.raises(container.ContainerAuthError):
        container.attach_container(bundle, b"wrong-key-9876543210", mp)
