"""LaunchAgent install/uninstall for the ScreenCap daemon."""

from __future__ import annotations

import errno
import json
import logging
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DAEMON_LABEL = "com.screencap.daemon"
DEFAULT_BINARY_NAME = "screencap"
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"

# sysexits.h "temporary failure" — daemon raises this when the socket is
# already held by a same-EUID rogue process. The install verifier reads it
# back from `launchctl print` to classify failures.
_EX_TEMPFAIL = 75

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstallResult:
    state: str
    plist_path: Path
    detail: str


@dataclass(frozen=True)
class UninstallResult:
    state: str
    plist_path: Path
    detail: str


@dataclass(frozen=True)
class StatusResult:
    state: str
    launchd_state: str | None
    detail: str


def render_plist(
    *,
    program: str | Path,
    label: str = DAEMON_LABEL,
    args: Iterable[str] = ("serve",),
    log_dir: str | Path | None = None,
    env_vars: dict[str, str] | None = None,
    bundle_program: str | None = None,
) -> bytes:
    """Build deterministic LaunchAgent plist XML bytes.

    `log_dir` semantics:
        - `None` (default): omit `StandardErrorPath` and `StandardOutPath`.
          launchd captures the daemon's stdout/stderr in the unified system
          log; access via `log show --predicate 'process == "screencap"'`.
          This is the right shape for the bundled (SMAppService) plist
          because that file ships with the app and must work for every
          user — but **launchd in macOS 26+ does NOT expand `$HOME` or `~`
          in path keys** (verified empirically against `launchd.plist(5)`'s
          ambiguous tilde-expansion language; literal `~/...` produces
          `EX_CONFIG` on spawn). So we cannot bake any user-relative path
          into the bundled plist.
        - Any path-like value: emit it verbatim into the path keys. Used
          by `install()` below to pass an absolute path resolved at
          install time (`str(Path.home() / "Library" / "Logs" / "ScreenCap")`)
          so file-based logs work on the per-user CLI install path.

    `SCREENCAP_RUN_DIR` is intentionally absent from the default
    EnvironmentVariables: the daemon's own `default_socket_path()`
    resolves `~/.screencap/run/api.sock` via `Path.home()` at runtime.
    launchd does NOT perform tilde or `$HOME` expansion in
    EnvironmentVariables values either, so any pre-baked value here would
    be wrong on every machine but the developer's.
    """
    program_path = str(Path(program).expanduser())

    if env_vars is None:
        env_vars = {"PATH": DEFAULT_PATH}

    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [program_path, *list(args)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ProcessType": "Adaptive",
        "ExitTimeOut": 30,
        "EnvironmentVariables": dict(env_vars),
    }
    if log_dir is not None:
        log_dir_str = str(log_dir)
        plist["StandardErrorPath"] = f"{log_dir_str}/daemon.err.log"
        plist["StandardOutPath"] = f"{log_dir_str}/daemon.out.log"
    if bundle_program is not None:
        plist["BundleProgram"] = bundle_program

    return plistlib.dumps(plist, fmt=plistlib.FMT_XML)


def default_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{DAEMON_LABEL}.plist"


def write_plist_atomic(path: Path, content: bytes) -> None:
    """Write plist bytes atomically so launchd never observes a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("wb") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def install(
    *,
    program: str | Path | None = None,
    plist_path: Path | None = None,
    timeout_seconds: float = 10.0,
) -> InstallResult:
    """Install and start the per-user ScreenCap LaunchAgent."""
    resolved_plist_path = (plist_path or default_plist_path()).expanduser()
    resolved_program, args = _resolve_program_arguments(program)
    # Per-user CLI install: bake the absolute log dir into the plist (launchd
    # in macOS 26+ does NOT expand `~` or `$HOME` in path keys, so the
    # bundled SMAppService plist omits these — but here we know the user
    # invoking the install, so we resolve their home and create the log
    # directory before launchctl bootstraps the agent and tries to open()
    # the path keys (a missing parent directory yields EX_CONFIG on spawn).
    log_dir = Path.home() / "Library" / "Logs" / "ScreenCap"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return InstallResult(
            state="install_failed_plist_write_failed",
            plist_path=resolved_plist_path,
            detail=f"could not create log dir {log_dir}: {exc}",
        )
    content = render_plist(program=resolved_program, args=args, log_dir=str(log_dir))

    existing_content = _read_existing(resolved_plist_path)
    content_changed = existing_content != content
    try:
        write_plist_atomic(resolved_plist_path, content)
    except OSError as exc:
        reason = "disk_full" if exc.errno == errno.ENOSPC else "plist_write_failed"
        return InstallResult(
            state=f"install_failed_{reason}",
            plist_path=resolved_plist_path,
            detail=str(exc),
        )

    domain = _launchctl_domain()
    service = _launchctl_service()
    bootstrap = subprocess.run(
        ["launchctl", "bootstrap", domain, str(resolved_plist_path)],
        capture_output=True,
        text=True,
    )
    bootstrap_stderr = bootstrap.stderr or ""
    already_loaded = _is_already_loaded(bootstrap_stderr)

    if bootstrap.returncode != 0 and not already_loaded:
        lower = bootstrap_stderr.lower()
        _unlink_best_effort(resolved_plist_path)
        if _looks_like_signing_error(lower):
            return InstallResult(
                state="install_failed_daemon_signing_invalid",
                plist_path=resolved_plist_path,
                detail=bootstrap_stderr,
            )
        if "operation not permitted" in lower:
            return InstallResult(
                state="permission_required",
                plist_path=resolved_plist_path,
                detail=bootstrap_stderr,
            )
        return InstallResult(
            state="install_failed_launchctl_bootstrap_failed",
            plist_path=resolved_plist_path,
            detail=bootstrap_stderr or f"launchctl exited {bootstrap.returncode}",
        )

    if already_loaded or content_changed:
        # `-kp` (not `-p`): kill and restart the running service so plist
        # content changes take effect immediately. Without `-k`, a running
        # service ignores the new plist until next crash.
        kickstart = subprocess.run(
            ["launchctl", "kickstart", "-kp", service],
            capture_output=True,
            text=True,
        )
        if kickstart.returncode != 0:
            return InstallResult(
                state="install_failed_launchctl_bootstrap_failed",
                plist_path=resolved_plist_path,
                detail=kickstart.stderr or f"launchctl kickstart exited {kickstart.returncode}",
            )

    if _wait_for_daemon(timeout_seconds=timeout_seconds):
        return InstallResult(
            state="installed_and_running",
            plist_path=resolved_plist_path,
            detail="daemon.info responded",
        )

    # Daemon never reached running state — distinguish "another daemon already
    # holds the socket and the spawned process exited with EX_TEMPFAIL=75" from
    # the generic did-not-start case. The 75-classified branch unblocks the
    # operator-visible CLI install verifier so they see "another daemon is
    # already bound to the socket" instead of a vague timeout.
    last_exit = _last_launchd_exit_code()
    if last_exit == _EX_TEMPFAIL:
        return InstallResult(
            state="install_failed_already_running",
            plist_path=resolved_plist_path,
            detail=(
                "launchctl reported last exit code = 75 (EX_TEMPFAIL); "
                "another daemon is already bound to the socket"
            ),
        )

    return InstallResult(
        state="install_failed_daemon_did_not_start",
        plist_path=resolved_plist_path,
        detail=f"daemon.info did not respond within {timeout_seconds:g}s",
    )


def uninstall(*, plist_path: Path | None = None) -> UninstallResult:
    """Stop the per-user LaunchAgent and remove its plist."""
    resolved_plist_path = (plist_path or default_plist_path()).expanduser()
    bootout = subprocess.run(
        ["launchctl", "bootout", _launchctl_service()],
        capture_output=True,
        text=True,
    )
    stderr = bootout.stderr or ""
    bootout_failed = bootout.returncode != 0 and not _is_not_loaded(stderr)

    # Clean the UDS socket regardless of bootout outcome: a stale
    # `~/.screencap/run/api.sock` after a crashed daemon would otherwise
    # block the next install's bind. `cleanup_socket` is filesystem-only
    # and idempotent, so running it on the failure path is safe.
    from screencap.daemon.socket import cleanup_socket, default_socket_path

    try:
        cleanup_socket(default_socket_path())
    except OSError:
        logger.debug("cleanup_socket failed during uninstall", exc_info=True)

    if bootout_failed:
        return UninstallResult(
            state="uninstall_failed_launchctl_bootout_failed",
            plist_path=resolved_plist_path,
            detail=stderr or f"launchctl exited {bootout.returncode}",
        )

    try:
        resolved_plist_path.unlink(missing_ok=True)
    except OSError as exc:
        return UninstallResult(
            state="uninstall_failed_plist_remove_failed",
            plist_path=resolved_plist_path,
            detail=str(exc),
        )

    return UninstallResult(
        state="uninstalled",
        plist_path=resolved_plist_path,
        detail="LaunchAgent removed",
    )


def status() -> StatusResult:
    """Return LaunchAgent load state from launchd."""
    printed = subprocess.run(
        ["launchctl", "print", _launchctl_service()],
        capture_output=True,
        text=True,
    )
    stderr = printed.stderr or ""
    if printed.returncode != 0:
        if _is_not_loaded(stderr):
            return StatusResult(state="not_loaded", launchd_state=None, detail=stderr)
        return StatusResult(
            state="status_failed",
            launchd_state=None,
            detail=stderr or f"launchctl exited {printed.returncode}",
        )

    launchd_state = _parse_launchd_state(printed.stdout or "")
    return StatusResult(
        state="loaded",
        launchd_state=launchd_state,
        detail=printed.stdout or "",
    )


def _resolve_program_arguments(program: str | Path | None) -> tuple[str | Path, tuple[str, ...]]:
    if program is not None:
        return program, ("serve",)
    if getattr(sys, "frozen", False):
        return sys.executable, ("serve",)
    found = shutil.which(DEFAULT_BINARY_NAME)
    if found:
        return found, ("serve",)
    return sys.executable, ("-m", "screencap", "serve")


def _read_existing(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        logger.debug("Could not read existing LaunchAgent plist", exc_info=True)
        return None


def _unlink_best_effort(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove LaunchAgent plist after failure", exc_info=True)


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl_service() -> str:
    return f"{_launchctl_domain()}/{DAEMON_LABEL}"


def _is_already_loaded(message: str) -> bool:
    lower = message.lower()
    return (
        "already loaded" in lower
        or "already bootstrapped" in lower
        or "service already" in lower
    )


def _is_not_loaded(message: str) -> bool:
    lower = message.lower()
    return (
        "not found" in lower
        or "no such service" in lower
        or "service is not loaded" in lower
        or "could not find service" in lower
    )


def _looks_like_signing_error(message: str) -> bool:
    signing_markers = ("code signature", "codesign", "signing", "signature invalid")
    return "operation not permitted" in message and any(
        marker in message for marker in signing_markers
    )


def _parse_launchd_state(output: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("state ="):
            return stripped.split("=", 1)[1].strip()
    return None


def _parse_last_exit_code(output: str) -> int | None:
    """Extract the integer value of the ``last exit code = N`` line in
    ``launchctl print`` output. Returns None when the field is absent or
    not parseable as a signed int."""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("last exit code"):
            continue
        _, _, value = stripped.partition("=")
        candidate = value.strip()
        try:
            return int(candidate)
        except ValueError:
            return None
    return None


def _last_launchd_exit_code() -> int | None:
    """Return the daemon's last exit code as reported by ``launchctl print``.

    Returns None when launchctl is unavailable, exits non-zero, or its
    output omits the ``last exit code`` field. Best-effort; never raises.
    """
    try:
        printed = subprocess.run(
            ["launchctl", "print", _launchctl_service()],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if printed.returncode != 0:
        return None
    return _parse_last_exit_code(printed.stdout or "")


def _wait_for_daemon(*, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        if _daemon_info_responds(timeout_seconds=min(0.5, max(timeout_seconds, 0.1))):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _daemon_info_responds(*, timeout_seconds: float = 0.5) -> bool:
    from screencap.daemon.socket import default_socket_path

    request = (
        b"GET /v0/daemon.info HTTP/1.1\r\n"
        b"Host: screencap\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout_seconds)
        sock.connect(str(default_socket_path()))
        sock.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        response = b"".join(chunks)
    except OSError:
        return False
    finally:
        sock.close()

    header, _sep, body = response.partition(b"\r\n\r\n")
    status_line = header.splitlines()[0] if header else b""
    if b" 200 " not in status_line:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(payload.get("ok"))
