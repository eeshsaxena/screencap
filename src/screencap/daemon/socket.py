"""AF_UNIX socket lifecycle and peer-origin checks for the daemon."""

from __future__ import annotations

import atexit
import ctypes
import errno
import logging
import os
import socket
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

_BOUND_PATHS: set[Path] = set()
_ATEXIT_REGISTERED = False


class DaemonSocketError(RuntimeError):
    """Base class for daemon socket setup failures."""


class DaemonAlreadyRunning(DaemonSocketError):
    """Raised when an existing socket accepts connections."""


class RogueFileAtSocketPath(DaemonSocketError):
    """Raised when a non-socket occupies the configured socket path."""


def default_socket_path() -> Path:
    return Path.home() / ".screencap" / "run" / "api.sock"


def _register_atexit_once() -> None:
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(_cleanup_bound_paths)
        _ATEXIT_REGISTERED = True


def _cleanup_bound_paths() -> None:
    for path in list(_BOUND_PATHS):
        cleanup_socket(path)


def cleanup_socket(path: str | Path) -> None:
    socket_path = Path(path).expanduser()
    try:
        mode = socket_path.stat().st_mode
    except FileNotFoundError:
        _BOUND_PATHS.discard(socket_path)
        return
    if stat.S_ISSOCK(mode):
        socket_path.unlink()
    _BOUND_PATHS.discard(socket_path)


def _ensure_socket_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def _probe_existing_socket(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return

    if not stat.S_ISSOCK(mode):
        raise RogueFileAtSocketPath(f"rogue file at socket path: {path}")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
    except OSError:
        path.unlink()
        return
    finally:
        probe.close()

    raise DaemonAlreadyRunning(f"another daemon is running at {path}")


def _getpeereid(fd: int) -> tuple[int, int]:
    libc = ctypes.CDLL(None, use_errno=True)
    func = libc.getpeereid
    func.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    func.restype = ctypes.c_int

    euid = ctypes.c_uint()
    egid = ctypes.c_uint()
    result = func(fd, ctypes.byref(euid), ctypes.byref(egid))
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(euid.value), int(egid.value)


def peer_matches_current_euid(fd: int) -> bool:
    peer_euid, _peer_egid = _getpeereid(fd)
    return peer_euid == os.geteuid()


class PeerCheckingUnixSocket(socket.socket):
    """Unix listener that rejects accepted connections from other EUIDs."""

    def accept(self) -> tuple[socket.socket, object]:  # type: ignore[override]
        while True:
            conn, address = super().accept()
            try:
                if peer_matches_current_euid(conn.fileno()):
                    return conn, address
                logger.warning("Rejected daemon client with different peer EUID")
            except OSError:
                logger.warning("Rejected daemon client after getpeereid failure", exc_info=True)
            conn.close()


def bind_unix_socket(path: str | Path | None = None) -> PeerCheckingUnixSocket:
    socket_path = Path(path).expanduser() if path is not None else default_socket_path()
    _ensure_socket_directory(socket_path)
    _probe_existing_socket(socket_path)

    listener = PeerCheckingUnixSocket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o077)
    try:
        try:
            listener.bind(str(socket_path))
        except OSError as exc:
            listener.close()
            if exc.errno in {errno.EADDRINUSE, errno.EEXIST}:
                _probe_existing_socket(socket_path)
            raise
    finally:
        os.umask(old_umask)

    os.chmod(socket_path, 0o600)
    listener.listen(socket.SOMAXCONN)
    _BOUND_PATHS.add(socket_path)
    _register_atexit_once()
    return listener
