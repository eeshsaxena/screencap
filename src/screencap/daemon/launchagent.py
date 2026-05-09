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
from typing import Iterable

DAEMON_LABEL = "com.screencap.daemon"
DEFAULT_BINARY_NAME = "screencap"
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"

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
    """Build deterministic LaunchAgent plist XML bytes."""
    program_path = str(Path(program).expanduser())

    if log_dir is None:
        log_dir = Path.home() / "Library" / "Logs" / "ScreenCap"
    resolved_log_dir = Path(log_dir).expanduser()

    if env_vars is None:
        env_vars = {
            "PATH": DEFAULT_PATH,
            "SCREENCAP_RUN_DIR": str(Path.home() / ".screencap" / "run"),
        }

    plist = {
        "Label": label,
        "ProgramArguments": [program_path, *list(args)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ProcessType": "Adaptive",
        "ExitTimeOut": 30,
        "StandardErrorPath": str(resolved_log_dir / "daemon.err.log"),
        "StandardOutPath": str(resolved_log_dir / "daemon.out.log"),
        "EnvironmentVariables": dict(env_vars),
    }
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
    content = render_plist(program=resolved_program, args=args)

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
    if bootout.returncode != 0 and not _is_not_loaded(stderr):
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
