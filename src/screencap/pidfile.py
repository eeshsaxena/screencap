"""PID file tracking for recording sessions."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import psutil

_DEFAULT_BASE = Path.home() / ".screencap"
PID_FILE = _DEFAULT_BASE / "recording.pid"


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
    """Check if a PID is actually a screencap/sc_engine process."""
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline()).lower()
        return "sc_engine" in cmdline or "screencap" in cmdline
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
                and ("sc_engine" in cmdline or "screencap" in cmdline)
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
