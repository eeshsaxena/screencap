"""PID file tracking for recording sessions."""

from __future__ import annotations

import json
import os
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


def _is_screencap_process(
    pid: int,
    name_allowlist: set[str] | None = None,
) -> bool:
    """Check if a PID belongs to the screencap process tree.

    The default heuristic checks for ``"screencap"`` in the cmdline. Network
    proxy children (``mp.Process`` running mitmproxy DumpMaster under macOS
    ``spawn`` mode) carry NO "screencap" substring in their cmdline -- their
    cmdline is the Python interpreter path + the multiprocessing bootstrap
    args. ``name_allowlist`` widens the filter to also accept any cmdline
    that contains one of the supplied names (e.g. ``{"mitmproxy"}`` from
    the recording.pid children entries).
    """
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline()).lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    if "screencap" in cmdline:
        return True
    if name_allowlist:
        for name in name_allowlist:
            if name.lower() in cmdline:
                return True
    return False


def _name_allowlist_from_children(children: list[dict[str, object]]) -> set[str]:
    """Extract `{"name": "mitmproxy", ...}` entries from the children list."""
    names: set[str] = set()
    for child in children or []:
        name = child.get("name") if isinstance(child, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return names


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
        allowlist = _name_allowlist_from_children(data.get("children", []))

        for child in data.get("children", []):
            pid = child.get("pid")
            if pid and _pid_exists(pid) and _is_screencap_process(pid, allowlist):
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
    allowlist = _name_allowlist_from_children(pids)

    for entry in pids:
        pid = entry.get("pid")
        if not pid or not _pid_exists(pid):
            continue
        if not _is_screencap_process(pid, allowlist):
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


def _atomic_write_pidfile(data: dict) -> None:
    """Write the PID file atomically (.tmp + os.replace)."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = PID_FILE.with_suffix(PID_FILE.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, PID_FILE)


def add_child(
    name: str,
    *,
    proxy_pid: int,
    worker_pid: int,
    proxy_create_time: float,
    proxy_cmdline_tail: str,
) -> None:
    """Append a child process entry to the PID file.

    Used by the SessionController's network-handoff daemon thread after the
    proxy mp.Process is alive. The ``name`` field also widens
    ``_is_screencap_process``'s cmdline filter so ``terminate_processes``
    can reach the proxy on shutdown (mitmproxy's spawn-mode cmdline does
    NOT contain "screencap").

    Idempotent: if an entry with the same name already exists, it is
    REPLACED with the new values. Atomic-write protects against partial
    writes.
    """
    data = read_pidfile() or {
        "parent_pid": os.getpid(),
        "children": [],
        "started_at": time.time(),
    }
    children = [c for c in data.get("children", []) if c.get("name") != name]
    children.append({
        "name": name,
        "pid": proxy_pid,
        "worker_pid": worker_pid,
        "create_time": proxy_create_time,
        "cmdline_tail": proxy_cmdline_tail,
    })
    data["children"] = children
    _atomic_write_pidfile(data)


def remove_child(name: str) -> None:
    """Remove a child process entry from the PID file by ``name``.

    No-op if the PID file is missing or the entry is absent.
    """
    data = read_pidfile()
    if not data:
        return
    children = [c for c in data.get("children", []) if c.get("name") != name]
    if len(children) == len(data.get("children", [])):
        return  # nothing changed
    data["children"] = children
    _atomic_write_pidfile(data)
