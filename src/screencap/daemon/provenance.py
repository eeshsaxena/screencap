"""Server-derived ``started_by`` provenance for daemon recordings.

Phase 2 U2 makes the daemon classify every accepted peer connection
into one of ``swiftui`` / ``cli`` / ``mcp`` / ``unknown`` and use that
as the authoritative ``started_by`` on persisted recording metadata.

The peer's effective PID comes from ``getsockopt(SOL_LOCAL, LOCAL_PEEREPID)``
on the accepted socket; the executable path comes from
``proc_pidpath(pid)``; the argv (used to distinguish ``screencap mcp``
from ``screencap start`` — both run from the same binary under
single-binary mode dispatch) comes from
``sysctl(KERN_PROCARGS2)``.

Caveats:

- This is advisory provenance only. The EUID match from Phase 1 stays
  the authentication gate; ``started_by`` does NOT gate any behavior.
- TOCTOU window between accept() and the syscall chain: a peer could
  ``exec()`` into a different image. We classify the post-exec image.
  Acceptable for advisory provenance.
- PIDs are reusable. Same caveat — we resolve the live image at probe
  time, accepting the inherent race.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import c_int, c_size_t, c_uint, c_void_p
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Classification labels — kept in sync with screencap.pidfile's
# CLAIMANT_* constants so downstream readers see the same vocabulary.
STARTED_BY_SWIFTUI = "swiftui"
STARTED_BY_CLI = "cli"
STARTED_BY_MCP = "mcp"
STARTED_BY_UNKNOWN = "unknown"

# macOS socket-options constants. Mirrored from <sys/un.h>.
SOL_LOCAL = 0
LOCAL_PEERPID = 0x002
LOCAL_PEEREPID = 0x003

# macOS sysctl constants. Mirrored from <sys/sysctl.h>.
CTL_KERN = 1
KERN_PROCARGS2 = 49

# proc_pidpath buffer size cap. macOS' libproc.h documents 4 * MAXPATHLEN
# as the safe ceiling.
_PROC_PIDPATHINFO_MAXSIZE = 4 * 1024


def _libc() -> ctypes.CDLL | None:
    """Return libc on macOS; ``None`` on Linux (this module is no-op there).

    The daemon ships macOS-only, but importing this module on a Linux
    CI runner should not crash — every callable below short-circuits to
    ``unknown`` when libc isn't the right shape.
    """
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None


def _libproc() -> ctypes.CDLL | None:
    try:
        # Empty string opens the executable's own symbol space; macOS
        # exposes libproc.dylib here. Linux has no equivalent and will
        # raise.
        return ctypes.CDLL("libproc.dylib", use_errno=True)
    except OSError:
        return None


def _get_peer_pid(sock_fd: int) -> int | None:
    """Return the peer's effective PID for the accepted UNIX socket.

    Tries ``LOCAL_PEEREPID`` first (responsible / harder-to-spoof PID),
    falls back to ``LOCAL_PEERPID`` on the rare older kernel where
    ``LOCAL_PEEREPID`` returns ``ENOPROTOOPT``.
    """
    libc = _libc()
    if libc is None:
        return None

    libc.getsockopt.argtypes = [c_int, c_int, c_int, c_void_p, ctypes.POINTER(c_uint)]
    libc.getsockopt.restype = c_int

    for option in (LOCAL_PEEREPID, LOCAL_PEERPID):
        pid_buf = c_uint(0)
        size = c_uint(ctypes.sizeof(pid_buf))
        rc = libc.getsockopt(
            c_int(sock_fd),
            c_int(SOL_LOCAL),
            c_int(option),
            ctypes.byref(pid_buf),
            ctypes.byref(size),
        )
        if rc == 0 and pid_buf.value > 0:
            return int(pid_buf.value)
    return None


def _get_proc_path(pid: int) -> str | None:
    libproc = _libproc()
    if libproc is None:
        return None
    libproc.proc_pidpath.argtypes = [c_int, c_void_p, c_uint]
    libproc.proc_pidpath.restype = c_int

    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    rc = libproc.proc_pidpath(c_int(pid), buf, c_uint(_PROC_PIDPATHINFO_MAXSIZE))
    if rc <= 0:
        return None
    return buf.raw[:rc].decode("utf-8", errors="replace")


def _get_proc_argv(pid: int) -> list[str]:
    """Return argv for ``pid`` via ``sysctl(KERN_PROCARGS2)``.

    Returns an empty list on any failure — callers must treat missing
    argv as "could not classify".

    KERN_PROCARGS2 wire format starts with a 4-byte ``argc``, followed
    by the NUL-terminated executable path, followed by ``argc``
    NUL-terminated argv strings, followed by environ. We only care
    about argv, so we parse up to ``argc + 1`` (path + argv).
    """
    libc = _libc()
    if libc is None:
        return []
    libc.sysctl.argtypes = [
        ctypes.POINTER(c_int),
        c_uint,
        c_void_p,
        ctypes.POINTER(c_size_t),
        c_void_p,
        c_size_t,
    ]
    libc.sysctl.restype = c_int

    # First query the argmax kernel parameter so we size the buffer
    # right. ``CTL_KERN, KERN_ARGMAX`` returns the maximum argv+env
    # payload size for any process; sized buffers smaller than this
    # would risk a partial read.
    KERN_ARGMAX = 8
    name_argmax = (c_int * 2)(CTL_KERN, KERN_ARGMAX)
    argmax = c_int(0)
    size = c_size_t(ctypes.sizeof(argmax))
    rc = libc.sysctl(
        name_argmax,
        c_uint(2),
        ctypes.byref(argmax),
        ctypes.byref(size),
        None,
        c_size_t(0),
    )
    if rc != 0 or argmax.value <= 0:
        return []

    buf = ctypes.create_string_buffer(argmax.value)
    bufsize = c_size_t(argmax.value)
    name_args = (c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    rc = libc.sysctl(
        name_args,
        c_uint(3),
        buf,
        ctypes.byref(bufsize),
        None,
        c_size_t(0),
    )
    if rc != 0 or bufsize.value < ctypes.sizeof(c_int):
        return []

    raw = buf.raw[: bufsize.value]
    argc = int.from_bytes(raw[: ctypes.sizeof(c_int)], "little")
    if argc < 0 or argc > 1024:
        return []

    cursor = ctypes.sizeof(c_int)
    # Skip the executable path (NUL-terminated, possibly with extra
    # NUL padding before argv[0]).
    end_path = raw.find(b"\x00", cursor)
    if end_path < 0:
        return []
    cursor = end_path + 1
    while cursor < len(raw) and raw[cursor : cursor + 1] == b"\x00":
        cursor += 1

    args: list[str] = []
    for _ in range(argc):
        end = raw.find(b"\x00", cursor)
        if end < 0:
            break
        args.append(raw[cursor:end].decode("utf-8", errors="replace"))
        cursor = end + 1
    return args


def classify_path_and_argv(path: str | None, argv: list[str]) -> str:
    """Pure classifier (testable without sockets).

    Decision rules:
    - Path ends with ``/ScreenCap.app/Contents/MacOS/screencap`` → ``swiftui``.
    - Otherwise inspect argv:
      - argv[1] == "mcp" → ``mcp``
      - argv[1] in {"start", "stop", "status"} (the live-state CLI verbs) → ``cli``
    - Else ``unknown``.

    The SwiftUI bundle check wins over argv inspection because the
    SwiftUI app may invoke the bundled CLI binary with arbitrary args
    (``view --`` for catalog open, etc.).
    """
    if path:
        normalized = path.lower()
        if normalized.endswith("/screencap.app/contents/macos/screencap"):
            return STARTED_BY_SWIFTUI
        if "/screencap.app/contents/macos/" in normalized:
            return STARTED_BY_SWIFTUI

    if len(argv) >= 2:
        verb = argv[1]
        if verb == "mcp":
            return STARTED_BY_MCP
        if verb in {"start", "stop", "status"}:
            return STARTED_BY_CLI
    elif len(argv) == 1 and path:
        # No subcommand (e.g., ``screencap`` bare invocation) — treat
        # as CLI but log so an operator can spot weird call shapes.
        return STARTED_BY_CLI

    return STARTED_BY_UNKNOWN


@dataclass(frozen=True)
class PeerDescriptor:
    """Snapshot of a peer's identity at request time.

    All three fields collapse to ``None`` / ``unknown`` when probing
    fails — callers must treat partial population as the norm, not the
    exception. The descriptor stays advisory: the EUID match in
    ``socket.PeerCheckingUnixSocket`` remains the authentication gate.
    """

    pid: int | None
    path: str | None
    classification: str


_UNKNOWN_PEER = PeerDescriptor(pid=None, path=None, classification=STARTED_BY_UNKNOWN)


def derive_peer_descriptor(sock_fd: int) -> PeerDescriptor:
    """Resolve a full peer descriptor (pid, path, classification) from a
    socket fd. Never raises — diagnostic failures collapse to the
    ``unknown`` descriptor.
    """
    try:
        pid = _get_peer_pid(sock_fd)
    except Exception:  # noqa: BLE001
        logger.debug("derive_peer_descriptor: getsockopt failed", exc_info=True)
        return _UNKNOWN_PEER

    if pid is None or pid <= 0:
        return _UNKNOWN_PEER

    try:
        path = _get_proc_path(pid)
    except Exception:  # noqa: BLE001
        path = None

    try:
        argv = _get_proc_argv(pid)
    except Exception:  # noqa: BLE001
        argv = []

    return PeerDescriptor(
        pid=pid,
        path=path,
        classification=classify_path_and_argv(path, argv),
    )


def derive_started_by(sock_fd: int) -> str:
    """Resolve ``started_by`` from the peer socket.

    Returns one of ``swiftui`` / ``cli`` / ``mcp`` / ``unknown``. Never
    raises — diagnostic failures collapse to ``unknown``.

    Caller passes the file descriptor of the accepted UNIX socket. The
    function does not assume any particular ASGI/Starlette shape; it
    just needs an ``int`` fd.
    """
    return derive_peer_descriptor(sock_fd).classification


def _peer_fd_from_asgi_scope(scope: dict) -> int | None:
    """Best-effort peer-fd extraction from an ASGI request scope.

    Uvicorn stashes the underlying ``asyncio.Transport`` (or its socket)
    on different scope paths across versions; we probe the documented
    ones and accept ``None`` if no path resolves. Callers must treat
    ``None`` as "could not classify" and fall through to
    ``STARTED_BY_UNKNOWN``.
    """
    transport = scope.get("transport")
    if transport is None:
        # Newer uvicorn uses ``scope["asgi"]`` extensions or carries
        # the socket inside ``scope["extensions"]``. Try a few known
        # paths without assuming any one wins; failing all of them is
        # not an error.
        extensions = scope.get("extensions") or {}
        transport = (
            extensions.get("transport")
            or extensions.get("socket")
            or extensions.get("asgi_socket")
        )
    if transport is None:
        return None
    socket = getattr(transport, "get_extra_info", lambda *_a, **_k: None)("socket")
    if socket is None:
        return None
    try:
        return int(socket.fileno())
    except (AttributeError, OSError, ValueError):
        return None


def derive_peer_descriptor_from_asgi_scope(scope: dict) -> PeerDescriptor:
    """Resolve the full peer descriptor from a Starlette/uvicorn ASGI
    request scope. Returns the ``unknown`` descriptor when the underlying
    socket is not reachable through the scope on this uvicorn version.
    """
    fd = _peer_fd_from_asgi_scope(scope)
    if fd is None:
        return _UNKNOWN_PEER
    return derive_peer_descriptor(fd)


def derive_started_by_from_asgi_scope(scope: dict) -> str:
    """Resolve ``started_by`` from a Starlette/uvicorn ASGI request scope.

    Returns ``unknown`` when the underlying socket is not reachable
    through the scope on this uvicorn version — the API surface is
    advisory and the EUID match from Phase 1 remains the auth gate.
    """
    return derive_peer_descriptor_from_asgi_scope(scope).classification


__all__ = [
    "STARTED_BY_SWIFTUI",
    "STARTED_BY_CLI",
    "STARTED_BY_MCP",
    "STARTED_BY_UNKNOWN",
    "PeerDescriptor",
    "classify_path_and_argv",
    "derive_peer_descriptor",
    "derive_peer_descriptor_from_asgi_scope",
    "derive_started_by",
    "derive_started_by_from_asgi_scope",
]
