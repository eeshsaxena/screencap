"""AF_UNIX socket lifecycle and peer-origin checks for the daemon."""

from __future__ import annotations

import atexit
import ctypes
import errno
import logging
import os
import socket
import stat
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_BOUND_PATHS: set[Path] = set()
_ATEXIT_REGISTERED = False


class DaemonSocketError(RuntimeError):
    """Base class for daemon socket setup failures."""


class DaemonAlreadyRunning(DaemonSocketError):
    """Raised when an existing socket accepts connections.

    ``existing_pid`` carries the PID of the process holding the socket as
    reported by ``lsof -tU``, or ``None`` if lsof was unavailable or did
    not return a parseable PID within the probe timeout.
    """

    def __init__(self, message: str, *, existing_pid: int | None = None) -> None:
        super().__init__(message)
        self.existing_pid = existing_pid


class RogueFileAtSocketPath(DaemonSocketError):
    """Raised when a non-socket occupies the configured socket path."""


class SocketPermsDrift(DaemonSocketError):
    """Raised when the socket or its parent directory deviates from the
    expected mode at bind time. Defends against umask races, accidental
    ``chmod`` post-install, and tampered run-dirs by refusing to listen
    over a leaky socket.
    """


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


_TEST_DRIFT_PARENT_MODE_ENV = "SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE"


def _maybe_inject_test_parent_drift(socket_path: Path) -> None:
    """Test-only: drift the socket parent dir's mode so the subprocess-level
    perm-drift integration test (SCR-67) can exercise :func:`_verify_socket_perms`
    end-to-end through a real ``screencap serve`` process.

    No-op unless ``SCREENCAP_DAEMON_TEST_DRIFT_PARENT_MODE`` is set (octal, e.g.
    ``"0755"``). When set, it ``chmod``s the parent AFTER
    :func:`_ensure_socket_directory` re-established ``0o700`` and BEFORE
    :func:`_verify_socket_perms`, reproducing the exact ensure->drift->verify
    race the verifier defends against — the cross-process analogue of the
    in-process ``ensure_then_drift`` monkeypatch in the daemon socket tests.

    Fail-closed by construction: because the drift lands before the verify, any
    value other than ``0o700`` makes :func:`_verify_socket_perms` raise and the
    daemon abort. This hook can only ever refuse startup, never serve over a
    relaxed socket. The ``SCREENCAP_DAEMON_`` prefix means CLI auto-spawn strips
    it (``cli/_autospawn.py``) and launchd does not inherit the operator's shell
    env, so it cannot reach a production daemon.
    """
    raw = os.environ.get(_TEST_DRIFT_PARENT_MODE_ENV)
    if not raw:
        return
    try:
        mode = int(raw, 8)
    except ValueError as exc:
        # A mistyped octal value must keep the typed-exit contract: surface it
        # as SocketPermsDrift so serve() maps it to EX_TEMPFAIL rather than
        # letting a bare ValueError escape as an unclassified exit-1 traceback.
        raise SocketPermsDrift(
            f"{_TEST_DRIFT_PARENT_MODE_ENV}={raw!r} is not a valid octal mode"
        ) from exc
    os.chmod(socket_path.parent, mode)


_EXPECTED_PARENT_MODE = 0o700
_EXPECTED_SOCKET_MODE = 0o600


def _verify_socket_perms(socket_path: Path) -> None:
    """Re-stat the socket and its parent directory and refuse to listen if
    either deviates from the expected mode. Catches umask drift, accidental
    ``chmod`` between bind and listen, and parent-dir tampering.

    Raises :class:`SocketPermsDrift` (a :class:`DaemonSocketError`) on any
    mismatch or missing path. Mode bits only — UID/GID checks are deferred.
    """
    parent = socket_path.parent
    try:
        parent_mode = stat.S_IMODE(parent.stat().st_mode)
    except FileNotFoundError as exc:
        raise SocketPermsDrift(
            f"socket parent dir missing at verify time: {parent}"
        ) from exc
    if parent_mode != _EXPECTED_PARENT_MODE:
        raise SocketPermsDrift(
            f"socket parent dir perms drifted: {parent} is "
            f"0o{parent_mode:03o}, expected 0o{_EXPECTED_PARENT_MODE:03o}"
        )

    try:
        socket_mode = stat.S_IMODE(socket_path.stat().st_mode)
    except FileNotFoundError as exc:
        raise SocketPermsDrift(
            f"socket file missing at verify time: {socket_path}"
        ) from exc
    if socket_mode != _EXPECTED_SOCKET_MODE:
        raise SocketPermsDrift(
            f"socket file perms drifted: {socket_path} is "
            f"0o{socket_mode:03o}, expected 0o{_EXPECTED_SOCKET_MODE:03o}"
        )


def _capture_socket_pid(path: Path) -> int | None:
    """Return the PID of the process holding ``path`` via ``lsof -tU``.

    Best-effort: returns ``None`` on missing binary, timeout, non-zero exit,
    or unparseable output. Never raises. The probe must not block daemon
    startup further than ~1s — this is a diagnostic, not a control flow.
    """
    try:
        result = subprocess.run(
            ["lsof", "-tU", str(path)],
            timeout=1.0,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    for line in (result.stdout or "").splitlines():
        candidate = line.strip()
        try:
            value = int(candidate)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


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

    existing_pid = _capture_socket_pid(path)
    raise DaemonAlreadyRunning(
        f"another daemon is running at {path}",
        existing_pid=existing_pid,
    )


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
    _maybe_inject_test_parent_drift(socket_path)
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
    try:
        _verify_socket_perms(socket_path)
    except SocketPermsDrift:
        listener.close()
        cleanup_socket(socket_path)
        raise

    listener.listen(socket.SOMAXCONN)
    _BOUND_PATHS.add(socket_path)
    _register_atexit_once()
    return listener
