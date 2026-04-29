"""Pre-flight, sentinel write/read, stale-cleanup orchestration.

This module owns the ``--network`` lifecycle bookkeeping: the global
single-instance lock, the activity sentinel, the per-recording handoff
file, port auto-negotiation, and orphan-state recovery.

The engine layer drives ``preflight_or_raise`` and the snapshot/sentinel
write path; the standalone ``screencap network restore`` command (Unit 8)
re-uses ``restore_orphaned_proxy_state`` directly.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from screencap.network.config import NetworkConfig
from screencap.network.system_proxy import (
    SystemProxyError,
    list_active_services,
    read_snapshot,
)

logger = logging.getLogger(__name__)
console = Console(stderr=True)

_DEFAULT_HOME = Path("~").expanduser()
_DEFAULT_SCREENCAP_DIR = _DEFAULT_HOME / ".screencap"
_DEFAULT_PROXY_DIR = _DEFAULT_SCREENCAP_DIR / "proxy"
_DEFAULT_PROXY_SNAPSHOTS_DIR = _DEFAULT_PROXY_DIR / "snapshots"
_DEFAULT_SENTINEL_PATH = _DEFAULT_SCREENCAP_DIR / ".network_active"
_DEFAULT_LOCK_PATH = _DEFAULT_SCREENCAP_DIR / ".network_active.lock"

# Daemon-thread orphan-cleanup osascript timeout: 30s covers the AFK admin
# dialog without blocking forever.
_ORPHAN_RESTORE_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NoFreePortError(Exception):
    """Raised when port auto-negotiation exhausts the entire range."""


class NetworkAlreadyActiveError(Exception):
    """Raised when another --network recording is already in progress."""


class MitmproxyImportError(Exception):
    """Raised when the mitmproxy package cannot be imported."""


class AdminAuthDeniedError(Exception):
    """Raised when the user cancels the macOS admin auth dialog."""


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


@dataclass
class _LockHandle:
    """Holds an open fd with an exclusive flock for the lifetime of a recording.

    The OS releases the lock automatically when the fd is closed (process
    death or explicit ``release``). Callers MUST keep this object alive for
    the recording's lifetime.
    """

    fd: int
    path: Path

    def release(self) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass


def acquire_network_lock(path: Path | None = None) -> _LockHandle:
    """Acquire ``~/.screencap/.network_active.lock`` exclusively (non-blocking).

    Raises ``NetworkAlreadyActiveError`` if another recording holds the lock.
    """
    lock_path = path or _DEFAULT_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise NetworkAlreadyActiveError(
            "another --network recording is already in progress"
        ) from exc
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise NetworkAlreadyActiveError(
                "another --network recording is already in progress"
            ) from exc
        raise
    return _LockHandle(fd=fd, path=lock_path)


# ---------------------------------------------------------------------------
# Port auto-negotiation
# ---------------------------------------------------------------------------


def auto_negotiate_port(start: int = 8080, end: int = 8090) -> int:
    """Bind-test each port in [start, end]; return first free.

    Raises ``NoFreePortError`` listing every attempted port if all are busy.
    """
    attempted: list[int] = []
    for port in range(start, end + 1):
        attempted.append(port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise NoFreePortError(
        f"all proxy ports busy: tried {attempted}. "
        f"Pin a port via [network] proxy_port in config.toml or stop the "
        f"conflicting service."
    )


# ---------------------------------------------------------------------------
# Sentinel + handoff
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)
    os.chmod(path, mode)


def write_sentinel(
    *,
    worker_pid: int,
    worker_create_time: float,
    worker_cmdline_tail: str,
    proxy_pid: int,
    proxy_create_time: float,
    proxy_cmdline_tail: str,
    started_at: float,
    port: int,
    recording_dir: str,
    snapshot_path: str,
    sentinel_path: Path | None = None,
) -> None:
    """Write the global ``~/.screencap/.network_active`` sentinel atomically."""
    payload = {
        "worker_pid": worker_pid,
        "worker_create_time": worker_create_time,
        "worker_cmdline_tail": worker_cmdline_tail,
        "proxy_pid": proxy_pid,
        "proxy_create_time": proxy_create_time,
        "proxy_cmdline_tail": proxy_cmdline_tail,
        "started_at": started_at,
        "port": port,
        "recording_dir": recording_dir,
        "snapshot_path": snapshot_path,
    }
    _atomic_write_json(sentinel_path or _DEFAULT_SENTINEL_PATH, payload)


def read_sentinel(sentinel_path: Path | None = None) -> dict[str, Any] | None:
    """Return parsed sentinel JSON or None if missing or unparseable."""
    path = sentinel_path or _DEFAULT_SENTINEL_PATH
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        logger.warning("sentinel at %s is unreadable; treating as absent", path)
        return None


def delete_sentinel(sentinel_path: Path | None = None) -> None:
    """Idempotent unlink of the global sentinel."""
    path = sentinel_path or _DEFAULT_SENTINEL_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_network_child_handoff(
    recording_dir: Path,
    *,
    proxy_pid: int,
    worker_pid: int,
    started_at: float,
) -> None:
    """Atomic-write the per-recording handoff file consumed by SessionController."""
    payload = {
        "proxy_pid": proxy_pid,
        "worker_pid": worker_pid,
        "started_at": started_at,
    }
    _atomic_write_json(recording_dir / ".network_child.json", payload)


def read_network_child_handoff(recording_dir: Path) -> dict[str, Any] | None:
    """Return the handoff dict, or None if missing/corrupted/incomplete.

    Validates ``proxy_pid`` and ``worker_pid`` are positive ints >= 100;
    smaller values (e.g. partial-int truncation) are treated as ABSENT to
    prevent stale-PID poisoning of ``recording.pid``.
    """
    path = recording_dir / ".network_child.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, KeyError):
        logger.warning("handoff at %s is unreadable; treating as absent", path)
        return None
    proxy_pid = data.get("proxy_pid")
    worker_pid = data.get("worker_pid")
    if not isinstance(proxy_pid, int) or proxy_pid < 100:
        return None
    if not isinstance(worker_pid, int) or worker_pid < 100:
        return None
    return data


def delete_network_child_handoff(recording_dir: Path) -> None:
    try:
        (recording_dir / ".network_child.json").unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# PID-reuse defense (F3-v9)
# ---------------------------------------------------------------------------


def is_pid_alive_with_create_time(
    pid: int,
    expected_create_time: float | None = None,
    expected_cmdline_tail: str | None = None,
) -> bool:
    """psutil-based liveness check that defeats PID reuse.

    Returns False if:
    - pid < 100 (suspicious)
    - psutil reports the PID does not exist
    - the recorded create_time does not match (PID was reused)
    - the recorded cmdline tail does not match (PID was reused)
    """
    if not isinstance(pid, int) or pid < 100:
        return False
    try:
        import psutil
    except ImportError:
        # Without psutil we can only do `pid_exists` via `os.kill(pid, 0)`,
        # which can't detect PID reuse. Fail conservatively.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if expected_create_time is not None:
        try:
            actual_create_time = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        if abs(actual_create_time - expected_create_time) > 1.0:
            return False
    if expected_cmdline_tail is not None:
        try:
            cmdline = " ".join(proc.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        if expected_cmdline_tail not in cmdline:
            return False
    return True


def cmdline_tail(pid: int, *, n: int = 3) -> str:
    """Return the last ``n`` whitespace-tokens of pid's cmdline (best-effort)."""
    try:
        import psutil

        proc = psutil.Process(pid)
        tokens = proc.cmdline()
    except Exception:  # noqa: BLE001 — best-effort attribution
        return ""
    if not tokens:
        return ""
    return " ".join(tokens[-n:])


def proc_create_time(pid: int) -> float:
    """Return ``psutil.Process(pid).create_time()`` or 0.0 on failure."""
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Orphan recovery (R21)
# ---------------------------------------------------------------------------


def _candidate_snapshot_paths(
    sentinel_path: Path | None = None,
    durable_dir: Path | None = None,
    recordings_dirs: list[Path] | None = None,
) -> list[Path]:
    """Return snapshot paths to try, in priority order.

    1. Global sentinel's recording_dir/.proxy_state.json.
    2. Durable copies under ~/.screencap/proxy/snapshots/.
    3. Per-recording-dir scan over default recordings dirs.
    """
    paths: list[Path] = []
    seen: set[Path] = set()

    sentinel = read_sentinel(sentinel_path)
    if sentinel and sentinel.get("snapshot_path"):
        p = Path(sentinel["snapshot_path"])
        if p not in seen:
            seen.add(p)
            paths.append(p)

    durable = durable_dir or _DEFAULT_PROXY_SNAPSHOTS_DIR
    if durable.exists():
        for child in sorted(durable.glob("*.proxy_state.json")):
            if child not in seen:
                seen.add(child)
                paths.append(child)

    if recordings_dirs:
        for rdir in recordings_dirs:
            if not rdir.exists():
                continue
            for child in sorted(rdir.glob("*/.proxy_state.json")):
                if child not in seen:
                    seen.add(child)
                    paths.append(child)

    return paths


def restore_orphaned_proxy_state(
    *,
    sentinel_path: Path | None = None,
    durable_dir: Path | None = None,
    recordings_dirs: list[Path] | None = None,
    osascript_timeout: int = _ORPHAN_RESTORE_TIMEOUT_S,
) -> list[Path]:
    """Restore system proxy from any orphaned snapshots; return restored paths.

    Idempotent. Safe to run anytime; no-op if no orphans.

    For each candidate path: read the snapshot + extra metadata; check
    ``worker_pid`` liveness via ``is_pid_alive_with_create_time``; if dead,
    run ``restore_all`` via osascript admin (with a 30s timeout to defeat
    AFK admin dialogs); delete snapshot/sentinel/handoff.
    """
    restored: list[Path] = []
    candidates = _candidate_snapshot_paths(
        sentinel_path=sentinel_path,
        durable_dir=durable_dir,
        recordings_dirs=recordings_dirs,
    )
    for path in candidates:
        try:
            services, extra = read_snapshot(path)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue

        worker_pid = extra.get("worker_pid")
        worker_create_time = extra.get("worker_create_time")
        worker_cmdline_tail = extra.get("worker_cmdline_tail")
        recording_dir_str = extra.get("recording_dir")
        recording_dir = Path(recording_dir_str) if recording_dir_str else path.parent

        # Prefer the handoff file's worker_pid if present (more recent than
        # the snapshot's embedded value).
        handoff = read_network_child_handoff(recording_dir)
        if handoff:
            worker_pid = handoff.get("worker_pid", worker_pid)

        # If the worker is alive and matches its recorded fingerprint, the
        # recording is still in progress — skip.
        if worker_pid and is_pid_alive_with_create_time(
            worker_pid,
            expected_create_time=worker_create_time,
            expected_cmdline_tail=worker_cmdline_tail,
        ):
            continue

        # Worker dead (or PID reused) → restore.
        try:
            _restore_with_timeout(services, osascript_timeout)
        except subprocess.TimeoutExpired:
            console.print(
                "[yellow]warning:[/yellow] proxy restore timed out (admin "
                "dialog may have been dismissed). Run `screencap network "
                "restore` to retry."
            )
            continue
        except SystemProxyError as exc:
            console.print(f"[yellow]warning:[/yellow] proxy restore failed: {exc}")
            continue

        restored.append(path)
        # Cleanup: delete the snapshot (per-recording + durable copy if both
        # exist), the sentinel if it pointed at this snapshot, and the
        # handoff file.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        # Durable copy
        recording_id = path.stem.replace(".proxy_state", "")
        durable_copy = (durable_dir or _DEFAULT_PROXY_SNAPSHOTS_DIR) / f"{recording_id}.proxy_state.json"
        try:
            durable_copy.unlink()
        except FileNotFoundError:
            pass
        # Per-recording-dir copy
        per_rec = recording_dir / ".proxy_state.json"
        if per_rec != path:
            try:
                per_rec.unlink()
            except FileNotFoundError:
                pass
        delete_network_child_handoff(recording_dir)
        # Only delete sentinel if it pointed at this snapshot
        sentinel = read_sentinel(sentinel_path)
        if sentinel and Path(sentinel.get("snapshot_path", "")) == path:
            delete_sentinel(sentinel_path)

    return restored


def _restore_with_timeout(services: dict, timeout_s: int) -> None:
    """Run ``restore_all`` with an outer wall-clock timeout.

    ``system_proxy._run_admin_osascript`` already passes ``timeout=300`` to
    subprocess; here we override that with a tighter ``timeout_s`` for the
    orphan-cleanup path so daemon threads don't block on AFK admin dialogs.
    """
    # We monkey-patch by directly building+running the inverse osascript
    # with a tight timeout. Prefer reusing system_proxy.restore_all but
    # threading the timeout through; keeping this local to avoid adding
    # a kwarg to system_proxy that no other caller wants.
    from screencap.network.system_proxy import _build_restore_commands  # noqa: PLC0415

    if not services:
        return
    shell = _build_restore_commands(services)
    apple_script = (
        f'do shell script "{shell.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}" '
        f'with administrator privileges '
        f'with prompt "screencap is restoring system proxy after a recording crash"'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", apple_script],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise SystemProxyError(
            f"orphan restore osascript failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def preflight_or_raise(
    network_config: NetworkConfig,
    privacy_config: Any,
    recording_dir: Path | None = None,
    *,
    confdir: Path | None = None,
) -> int:
    """Run all pre-flight checks for ``screencap start --network``.

    Returns the negotiated proxy port. Raises an actionable exception on
    any failure; the caller (top-level ``recorder.start_recording``) catches
    these and prints via ``rich.console.Console``.

    Order (load-bearing):
        0. (Caller acquires the network lock BEFORE invoking this — see
           ``acquire_network_lock``.)
        1. Restore any orphaned proxy state from a prior crash (no UI yet).
        2. mitmproxy import check (no UI).
        3. Port auto-negotiation 8080-8090 (no UI).
        4. networksetup callable check (no UI).
        5. Admin auth obtained for networksetup via osascript trial
           (FIRST user prompt).
        6. CA verify; if missing/expired, generate + install (Keychain
           prompt — SECOND user prompt).
        7. Set up ~/.screencap/proxy/ (mkdir 700 + xattr + iCloud check).
    """
    proxy_dir = confdir or _DEFAULT_PROXY_DIR

    # (1) stale-cleanup BEFORE prompts.
    restore_orphaned_proxy_state()

    # (2) mitmproxy import check.
    try:
        import mitmproxy.tools.dump  # noqa: F401, PLC0415
        from mitmproxy.options import Options  # noqa: F401, PLC0415
    except ImportError as exc:
        raise MitmproxyImportError(
            "mitmproxy is not importable. Run `pip install -e .` to refresh "
            "deps; the `--network` feature requires mitmproxy>=11.0,<11.1."
        ) from exc

    # (3) Port auto-negotiation.
    if network_config.proxy_port:
        # User pinned a port — try only that one.
        port = network_config.proxy_port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            sock.close()
            raise NoFreePortError(
                f"pinned proxy port {port} is busy. Stop the conflicting "
                f"service or change [network] proxy_port in config.toml."
            ) from exc
        sock.close()
    else:
        port = auto_negotiate_port(8080, 8090)

    # (4) networksetup callable check.
    try:
        result = subprocess.run(
            ["/usr/sbin/networksetup", "-listallnetworkservices"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            raise SystemProxyError(
                f"networksetup failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SystemProxyError(
            "networksetup is not callable. This feature requires macOS."
        ) from exc

    # (5) Admin auth trial — single osascript admin call to seed the auth
    # cache; subsequent set_proxy_all uses the cached auth.
    try:
        _run_admin_auth_trial()
    except subprocess.TimeoutExpired as exc:
        raise AdminAuthDeniedError(
            "admin auth dialog timed out. Run `screencap start --network` "
            "again and accept the prompt."
        ) from exc

    # (6) CA verify; install if needed.
    from screencap.network import ca_lifecycle  # noqa: PLC0415

    ca_pem = proxy_dir / "mitmproxy-ca.pem"
    cert_pem = proxy_dir / "mitmproxy-ca-cert.pem"
    if not ca_pem.exists() or ca_lifecycle.is_expiring_soon(threshold_days=7):
        # Set up the dir first so generate_ca has somewhere to write.
        ca_lifecycle.setup_proxy_dir(proxy_dir)
        ca_lifecycle.generate_ca(proxy_dir, days=30)
        identity = ca_lifecycle.install_ca(cert_pem)
        console.print(
            f"[green]Installed screencap proxy CA[/green] (CN={identity.cn}, "
            f"SHA-256={identity.sha256_hex[:16]}…). 30-day expiry."
        )
    elif not ca_lifecycle.verify_ca():
        # CA exists but trust is missing — re-install.
        ca_lifecycle.setup_proxy_dir(proxy_dir)
        identity = ca_lifecycle.install_ca(cert_pem)
        console.print(
            f"[yellow]Re-installed screencap proxy CA[/yellow] (CN={identity.cn})."
        )

    # (7) Setup proxy dir + iCloud warning for the recording dir.
    ca_lifecycle.setup_proxy_dir(proxy_dir)
    if recording_dir is not None:
        if ca_lifecycle.check_icloud_sync(recording_dir):
            console.print(
                f"[yellow]warning:[/yellow] recording dir {recording_dir} is "
                f"iCloud-synced. URLs and headers will upload to iCloud. "
                f"Use --output to specify a non-synced location."
            )

    return port


def _run_admin_auth_trial() -> None:
    """Single osascript admin call to seed the macOS auth cache.

    The actual command is a no-op (echo); the goal is to surface the admin
    dialog up-front rather than mid-recording.
    """
    apple_script = (
        'do shell script "echo screencap-network-preflight" '
        'with administrator privileges '
        'with prompt "screencap needs admin access to configure your network '
        'proxy for capture (will be restored on stop)"'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", apple_script],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,  # 2 min for the user to respond
    )
    if result.returncode != 0:
        raise AdminAuthDeniedError(
            f"admin auth was not granted (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def full_uninstall(
    *,
    confdir: Path | None = None,
    sentinel_path: Path | None = None,
    lock_path: Path | None = None,
    durable_dir: Path | None = None,
    recordings_dirs: list[Path] | None = None,
) -> None:
    """Uninstall the screencap proxy CA + clean up our state.

    Order is load-bearing per R4 and R20:
        1. ``restore_orphaned_proxy_state()`` FIRST -- leave the user's
           network in a working state regardless of how they got here.
        2. Read ``CertIdentity`` from ``<confdir>/ca-identity.json`` and
           call ``ca_lifecycle.uninstall_ca`` (SHA-256 primary, SHA-1
           fallback for older macOS, CN-fallback if the identity file is
           missing). All "not found" exits are treated as success.
        3. Delete ``<confdir>/`` contents -- but ONLY ours. NEVER touch
           ``~/.mitmproxy/`` (mitmproxy's own user dir, possibly an
           unrelated install). If ``~/.mitmproxy/`` exists, log an
           informational notice that it is being left untouched.
        4. Delete the global sentinel + lock file.

    Idempotent: every step gracefully handles "already done" / "never
    existed". No ``--remove-kek`` in V1 (V1.5 work).
    """
    from screencap.network import ca_lifecycle as _ca

    confdir = confdir or _DEFAULT_PROXY_DIR
    sentinel_path = sentinel_path or _DEFAULT_SENTINEL_PATH
    lock_path = lock_path or _DEFAULT_LOCK_PATH
    durable_dir = durable_dir or _DEFAULT_PROXY_SNAPSHOTS_DIR

    # (1) Restore orphaned state FIRST.
    try:
        restore_orphaned_proxy_state(
            sentinel_path=sentinel_path,
            durable_dir=durable_dir,
            recordings_dirs=recordings_dirs,
        )
    except Exception:  # noqa: BLE001
        logger.exception("restore_orphaned_proxy_state failed during uninstall")

    # (2) Uninstall the CA (idempotent at the helper level).
    identity_path = confdir / "ca-identity.json"
    identity = None
    if identity_path.exists():
        try:
            data = json.loads(identity_path.read_text())
            identity = _ca.CertIdentity(
                cn=data["cn"],
                sha256_hex=data["sha256_hex"],
                sha1_hex=data["sha1_hex"],
            )
        except (OSError, json.JSONDecodeError, KeyError):
            logger.warning(
                "ca-identity.json at %s is unreadable; "
                "falling back to CN-based delete",
                identity_path,
            )
    try:
        _ca.uninstall_ca(identity)
    except Exception:  # noqa: BLE001
        logger.exception("uninstall_ca failed; continuing cleanup")

    # (3) Delete OUR proxy dir -- never ~/.mitmproxy/.
    home = Path("~").expanduser()
    user_mitmproxy_dir = home / ".mitmproxy"
    if user_mitmproxy_dir.exists():
        console.print(
            f"[blue]note:[/blue] {user_mitmproxy_dir} exists but was not "
            f"modified (it belongs to a separate mitmproxy install)."
        )
    if confdir.exists():
        try:
            import shutil

            shutil.rmtree(confdir)
        except OSError:
            logger.exception("failed to delete %s", confdir)

    # (4) Delete sentinel + lock file.
    delete_sentinel(sentinel_path)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


# Re-export list_active_services for convenience in callers that already
# imported `lifecycle`.
__all__ = [
    "AdminAuthDeniedError",
    "MitmproxyImportError",
    "NetworkAlreadyActiveError",
    "NoFreePortError",
    "acquire_network_lock",
    "auto_negotiate_port",
    "cmdline_tail",
    "delete_network_child_handoff",
    "delete_sentinel",
    "full_uninstall",
    "is_pid_alive_with_create_time",
    "list_active_services",
    "preflight_or_raise",
    "proc_create_time",
    "read_network_child_handoff",
    "read_sentinel",
    "restore_orphaned_proxy_state",
    "write_network_child_handoff",
    "write_sentinel",
]
