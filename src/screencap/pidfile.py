"""PID file tracking for recording sessions."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import time
from pathlib import Path

import psutil

_DEFAULT_BASE = Path.home() / ".screencap"
PID_FILE = _DEFAULT_BASE / "recording.pid"

# Process-exclusive lock file (separate from PID_FILE so legacy readers of
# recording.pid keep working unchanged). Kernel-managed flock auto-releases
# on process death — no stale-lock cleanup needed.
LOCK_DIR = _DEFAULT_BASE / "run"
LOCK_FILE = LOCK_DIR / "recording.lock"

# Module-global fd holding the active flock. Held for the lifetime of the
# claiming process; kernel releases on death even if release_lock() is not
# called. A second claim_lock() in the same process is a no-op.
_LOCKED_FD: int | None = None


class LockContended(Exception):
    """Raised when claim_lock cannot acquire the lock — another holder is alive."""

    def __init__(self, owner: dict | None = None):
        self.owner = owner or {}
        super().__init__(f"recording lock held by another process: {self.owner}")


def write_pidfile(
    capture_dir: Path,
    child_pids: list[dict[str, int | str]],
) -> Path:
    """Write a PID file with parent and child process info.

    Args:
        capture_dir: Path to the recording directory.
        child_pids: List of dicts with 'pid' and 'name' keys.

    Returns:
        Path to the written PID file.
    """
    data = {
        "parent_pid": os.getpid(),
        "children": child_pids,
        "capture_dir": str(capture_dir),
        "started_at": time.time(),
    }
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(data, indent=2))
    return PID_FILE


def read_pidfile() -> dict | None:
    """Read and return the PID file contents, or None if missing/corrupt."""
    if not PID_FILE.exists():
        return None
    try:
        return json.loads(PID_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def delete_pidfile() -> None:
    """Delete the PID file if it exists."""
    PID_FILE.unlink(missing_ok=True)


def _is_screencap_process(pid: int) -> bool:
    """Check if a PID is actually a screencap process."""
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline()).lower()
        return "screencap" in cmdline
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def find_orphaned_processes() -> list[dict[str, int | str]]:
    """Find orphaned recording processes.

    Checks the PID file first, then falls back to scanning all processes.
    Returns a list of dicts with 'pid' and 'name' keys.
    """
    orphans = []

    # Try PID file first
    data = read_pidfile()
    if data:
        parent_pid = data.get("parent_pid")
        parent_alive = _pid_exists(parent_pid) if parent_pid else False

        for child in data.get("children", []):
            pid = child.get("pid")
            if pid and _pid_exists(pid) and _is_screencap_process(pid):
                orphans.append(child)

        # If parent is still alive and managing children, they're not orphans
        if parent_alive and _is_screencap_process(parent_pid):
            return []

        if orphans:
            return orphans

    # Fallback: scan all processes
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if (
                proc.pid != os.getpid()
                and "screencap" in cmdline
                and "recorder" in cmdline
            ):
                orphans.append({"pid": proc.pid, "name": proc.info.get("name", "unknown")})
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return orphans


def terminate_processes(
    pids: list[dict[str, int | str]],
    force: bool = False,
) -> list[dict[str, int | str]]:
    """Terminate a list of processes by PID.

    Args:
        pids: List of dicts with 'pid' and 'name' keys.
        force: If True, skip SIGTERM and go straight to SIGKILL.

    Returns:
        List of processes that were successfully terminated.
    """
    terminated = []

    for entry in pids:
        pid = entry.get("pid")
        if not pid or not _pid_exists(pid):
            continue
        if not _is_screencap_process(pid):
            continue

        try:
            proc = psutil.Process(pid)
            if force:
                proc.kill()
            else:
                proc.terminate()
            terminated.append(entry)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not force and terminated:
        # Wait for SIGTERM to take effect, then SIGKILL survivors
        gone, alive = psutil.wait_procs(
            [psutil.Process(e["pid"]) for e in terminated if _pid_exists(e["pid"])],
            timeout=5,
        )
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    return terminated


def _pid_exists(pid: int) -> bool:
    """Check if a process with the given PID exists."""
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def claim_lock(capture_dir: Path | str | None, claimant: str = "cli") -> int:
    """Acquire an exclusive flock on LOCK_FILE; write JSON metadata into it.

    Args:
        capture_dir: Per-recording directory if known (legacy single-shot
            ``screencap start`` knows it at this point). ``None`` when called
            from SessionController init — the per-recording dir is allocated
            later and plumbed in via :func:`update_lock_metadata` (todo 014).
            Persisted as ``null`` in the JSON when ``None``.
        claimant: "cli" for standalone invocations, "swiftui" when spawned by
            the SwiftUI app (via ``SCREENCAP_PARENT=swiftui``).

    Returns:
        The locked file descriptor (kept open; held in module-global state).
        Subsequent calls in the same process are no-ops and return the same fd.

    Raises:
        LockContended: If another live process holds the lock. The exception
            carries the existing lock metadata in ``.owner`` for diagnostics.
    """
    global _LOCKED_FD
    if _LOCKED_FD is not None:
        return _LOCKED_FD

    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    # Explicit perms on the dir so the lockfile content (PID, started_at,
    # capture_dir) is not world-readable even on misconfigured umasks (todo 042).
    try:
        os.chmod(LOCK_DIR, 0o700)
    except OSError:
        pass
    fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            existing = json.loads(LOCK_FILE.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            existing = {}
        os.close(fd)
        raise LockContended(owner=existing) from None

    # If the metadata-write sequence raises after flock succeeds, close the
    # fd so the kernel releases the lock immediately — otherwise the caller
    # is left in a confused state where the module thinks no lock is held
    # but the kernel still has it (todo 032).
    try:
        metadata = {
            "pid": os.getpid(),
            "started_at": time.time(),
            "capture_dir": str(capture_dir) if capture_dir is not None else None,
            "claimant": claimant,
        }
        payload = json.dumps(metadata).encode()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)  # releases the flock
        except OSError:
            pass
        raise

    _LOCKED_FD = fd
    return fd


def update_lock_metadata(capture_dir: Path | str) -> bool:
    """Update the held lock file's ``capture_dir`` field in place (todo 014).

    Called from ``SessionController._on_start_click`` once the per-recording
    directory is allocated, so ``screencap status --json`` (and any consumer
    reading the lock metadata) reflects the live recording's path instead of
    the placeholder ``null`` written by ``claim_lock``.

    Returns ``True`` on success, ``False`` if the process doesn't currently
    hold the lock (no-op so callers don't need to track state).
    """
    global _LOCKED_FD
    if _LOCKED_FD is None:
        return False
    try:
        # Re-read the metadata so we don't lose the pid/started_at/claimant
        # written at claim time. The file is held by our own flock, so a
        # racing read-modify-write is impossible from another process.
        existing = read_lock_metadata() or {}
        existing["capture_dir"] = str(capture_dir)
        payload = json.dumps(existing).encode()
        os.ftruncate(_LOCKED_FD, 0)
        os.lseek(_LOCKED_FD, 0, os.SEEK_SET)
        os.write(_LOCKED_FD, payload)
        os.fsync(_LOCKED_FD)
        return True
    except OSError:
        return False


def release_lock() -> None:
    """Release the flock held by this process. No-op if not held.

    Kernel auto-releases on process death; calling this is optional but lets
    long-lived parents (e.g., SessionController across multiple recordings)
    explicitly release between recordings if ever needed.
    """
    global _LOCKED_FD
    if _LOCKED_FD is None:
        return
    try:
        fcntl.flock(_LOCKED_FD, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_LOCKED_FD)
    except OSError:
        pass
    _LOCKED_FD = None


def read_lock_metadata() -> dict | None:
    """Read the lock file's JSON content without acquiring the flock.

    Used by ``screencap status --json`` and ``screencap stop`` to inspect the
    current holder without contending. Returns None if the file is missing or
    unparseable (treat as "no recording active").
    """
    if not LOCK_FILE.exists():
        return None
    try:
        return json.loads(LOCK_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def lock_is_active() -> bool:
    """Return True iff some live process holds the flock on LOCK_FILE.

    Detects the stale-lock case (file exists, last holder died, kernel
    auto-released) by attempting a non-blocking flock on a probe fd; success
    means no holder. Probe lock is released immediately.
    """
    if not LOCK_FILE.exists():
        return False
    try:
        probe_fd = os.open(str(LOCK_FILE), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
    finally:
        try:
            os.close(probe_fd)
        except OSError:
            pass
