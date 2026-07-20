"""F3 auto-spawn fallback for CLI live-state commands.

Phase 2 U1 collapses ``screencap start``/``stop``/``status`` to thin
clients of the daemon API. On a machine without the SwiftUI app
installed (and therefore without a LaunchAgent), the CLI still needs to
work — origin F3 / AE3.

Decision flow:
1. Try connecting to the daemon socket. If reachable, no spawn needed.
2. Probe whether a LaunchAgent is installed via
   ``launchctl print gui/$UID/com.screencap.daemon``. If installed but
   not running, surface a kickstart hint and exit non-zero — auto-
   spawning here would race launchd's supervision.
3. Otherwise (F3 case), spawn ``screencap serve --idle-shutdown=600``
   detached via ``posix_spawn`` with an absolute path to the daemon
   binary (never a bare command name — a hostile PATH could otherwise
   execute an arbitrary binary inheriting the user's Screen Recording
   TCC grants). Redirect stdout/stderr to ``~/.screencap/run/auto-serve.log``
   (mode 0o600). Poll the socket up to ``readiness_timeout_s`` seconds.

Open questions resolved at implementation time (see plan §Open
Questions → Deferred to Implementation):
- Stale-socket detection: trust Phase 1 U1's daemon-side cleanup. We
  do NOT unlink stale sockets here.
- Spawn race: two parallel auto-spawns both detect "no daemon", both
  spawn. The second spawn exits non-zero via Phase 1 U1's already-
  running detection; we bound the connection retry to 2 attempts so a
  racing winner still ends up serving the loser.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Bundle identifier the LaunchAgent registers under. Mirrors the value
# Phase 1 U6 wrote into ``com.screencap.daemon.plist``.
LAUNCHAGENT_LABEL = "com.screencap.daemon"

# Default idle-shutdown seconds the CLI passes to an auto-spawned
# daemon. 10 minutes — chosen so cron-driven ``screencap status`` does
# not leave a permanent daemon, but interactive workflows don't recycle
# in the middle of a multi-command sequence.
DEFAULT_IDLE_SHUTDOWN_S = 600

# RUN-DIR boundary (SCR-236 R3): the auto-spawn diagnostic log stays OUTSIDE the
# at-rest container as plaintext. Do NOT route through config.get_data_root().
_AUTO_LOG_PATH = Path.home() / ".screencap" / "run" / "auto-serve.log"


class DaemonAutoSpawnError(RuntimeError):
    """Auto-spawn failed; carries the log tail (if any) for user surfacing."""

    def __init__(self, message: str, *, log_tail: str | None = None) -> None:
        super().__init__(message)
        self.log_tail = log_tail


class LaunchAgentNotRunningError(RuntimeError):
    """LaunchAgent is installed but daemon is not running; user must kickstart."""

    KICKSTART_HINT = (
        "Error: Screencap daemon is registered with launchd but not running.\n"
        "Start it with: launchctl kickstart -kp gui/$UID/com.screencap.daemon\n"
        "(Auto-start was suppressed because launchd is managing this daemon. "
        "Running it manually would conflict with launchd supervision.)"
    )


@dataclass
class _SpawnPlan:
    binary: Path
    args: list[str]


def _socket_reachable(socket_path: Path, connect_timeout_s: float = 0.5) -> bool:
    """Return True iff a probe ``connect()`` to ``socket_path`` succeeds."""
    if not socket_path.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout_s)
    try:
        sock.connect(str(socket_path))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _launchagent_installed() -> bool:
    """True iff ``launchctl print gui/$UID/com.screencap.daemon`` succeeds.

    The non-zero exit signals "service not loaded"; anything else
    (timeout, missing launchctl, ``launchctl`` reporting "could not find
    service") collapses to "not installed" since we can't be certain
    launchd is supervising.
    """
    uid = os.geteuid()
    target = f"gui/{uid}/{LAUNCHAGENT_LABEL}"
    try:
        result = subprocess.run(
            ["launchctl", "print", target],
            timeout=2.0,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _resolve_daemon_binary() -> Path:
    """Return the absolute path of the binary that should host the daemon.

    Frozen PyInstaller builds: ``sys.executable`` is the binary itself.
    Dev / pip installs: ``sys.argv[0]`` is the script entry. We always
    resolve to an absolute path so ``posix_spawn`` never executes a
    PATH-resolved sibling — a hostile PATH could otherwise inject an
    arbitrary binary with Screen Recording TCC grants attached.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        resolved = Path(argv0).resolve()
        if resolved.exists() and resolved.is_file():
            return resolved
    # Last-resort fallback: ``sys.executable`` plus ``-m screencap`` is
    # invoked by ``_build_spawn_plan`` when no entry script is on disk
    # (rare — ``python -c 'from screencap.cli import cli; cli()'``).
    return Path(sys.executable).resolve()


def _build_spawn_plan(idle_shutdown_s: int, socket_path: Path | None) -> _SpawnPlan:
    binary = _resolve_daemon_binary()
    if getattr(sys, "frozen", False):
        args = [str(binary), "serve", f"--idle-shutdown={idle_shutdown_s}"]
    elif binary.name == "screencap" or binary.suffix == "":
        # Entry script (pip install gives ``…/bin/screencap``).
        args = [str(binary), "serve", f"--idle-shutdown={idle_shutdown_s}"]
    else:
        # Dev fallback: ``python -m screencap serve``.
        args = [str(binary), "-m", "screencap", "serve", f"--idle-shutdown={idle_shutdown_s}"]
    if socket_path is not None:
        args.extend(["--socket", str(socket_path)])
    return _SpawnPlan(binary=binary, args=args)


def _open_auto_log() -> int:
    """Open ``~/.screencap/run/auto-serve.log`` with hardened perms.

    Defends against symlink-redirection of the log dir by verifying
    ``realpath == abspath`` after mkdir.
    """
    log_path = _AUTO_LOG_PATH
    log_dir = log_path.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_dir, 0o700)
    except OSError:
        pass
    if os.path.realpath(str(log_dir)) != os.path.abspath(str(log_dir)):
        # A pre-created symlink in the log dir's path resolves
        # elsewhere — drop log capture and let the spawn proceed with
        # DEVNULL output rather than write to a hostile target.
        return os.open(os.devnull, os.O_WRONLY)
    # O_NOFOLLOW prevents a symlink at the log path from redirecting
    # writes to an attacker-controlled target.
    try:
        return os.open(
            str(log_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        # ELOOP: log_path is a symlink. Fall through to /dev/null so the
        # spawn still proceeds without writing through the symlink.
        return os.open(os.devnull, os.O_WRONLY)


def _read_log_tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(log file not found)"
    return "\n".join(text.splitlines()[-lines:])


def _spawn_daemon(plan: _SpawnPlan) -> int:
    """``posix_spawn`` ``plan.args`` detached. Return the new daemon PID."""
    log_fd = _open_auto_log()
    try:
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            file_actions = [
                (os.POSIX_SPAWN_DUP2, devnull_fd, 0),
                (os.POSIX_SPAWN_DUP2, log_fd, 1),
                (os.POSIX_SPAWN_DUP2, log_fd, 2),
                (os.POSIX_SPAWN_CLOSE, log_fd),
                (os.POSIX_SPAWN_CLOSE, devnull_fd),
            ]
            # ``setsid=True`` detaches the spawned process group so the
            # parent CLI can exit without its session signals reaching
            # the daemon.
            # Strip SCREENCAP_DAEMON_* env vars from the spawned daemon's
            # environment.  These could carry a hostile
            # SCREENCAP_DAEMON_ENGINE_COMMAND that would execute an
            # attacker-controlled binary with the user's Screen Recording
            # TCC grants. The supported configuration channel for the auto-
            # spawn case is the explicit --idle-shutdown CLI flag.
            filtered_env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("SCREENCAP_DAEMON_")
            }
            pid = os.posix_spawn(
                str(plan.binary),
                plan.args,
                filtered_env,
                file_actions=file_actions,
                setsid=True,
            )
        finally:
            os.close(devnull_fd)
    finally:
        os.close(log_fd)
    return pid


def _poll_socket_ready(
    socket_path: Path,
    *,
    deadline: float,
    spawned_pid: int | None,
) -> bool:
    """Backoff-poll the socket until reachable or ``deadline`` (monotonic) elapses."""
    delay = 0.025
    while time.monotonic() < deadline:
        if _socket_reachable(socket_path):
            return True
        if spawned_pid is not None:
            try:
                # Reap fast-exit failures so the parent's wait doesn't
                # accumulate zombies.
                exited_pid, _status = os.waitpid(spawned_pid, os.WNOHANG)
            except ChildProcessError:
                exited_pid = 0
            if exited_pid == spawned_pid:
                return False
        time.sleep(delay)
        delay = min(delay * 2, 0.25)
    return False


def _kill_pid_if_alive(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        pass


def ensure_daemon_or_spawn(
    *,
    socket_path: Path | None = None,
    idle_shutdown_s: int = DEFAULT_IDLE_SHUTDOWN_S,
    readiness_timeout_s: float = 5.0,
    stderr_emitter=None,
    auto_spawn: bool = True,
) -> None:
    """Connect to the daemon or auto-spawn it for F3.

    Raises ``LaunchAgentNotRunningError`` when a LaunchAgent is
    installed but no daemon is reachable — the user must kickstart it.
    Raises ``DaemonAutoSpawnError`` when auto-spawn was attempted but
    the daemon never came up within ``readiness_timeout_s``.
    Returns silently when the daemon is reachable or auto-spawn
    succeeded.

    ``auto_spawn`` is the screencap stop / status hook to opt OUT of
    spawning when the daemon being missing means there's nothing to do
    (stopping a non-existent recording is a no-op; status of a missing
    daemon is "not recording").
    """
    from screencap.daemon.socket import default_socket_path

    target = socket_path if socket_path is not None else default_socket_path()

    if _socket_reachable(target):
        return

    if not auto_spawn:
        # Caller (``screencap stop`` / ``status``) decided a missing
        # daemon is itself the terminal state — let the subsequent HTTP
        # call surface ``DaemonUnreachableError`` so the command body
        # owns the exit-code-and-message UX. No launchctl probe; no
        # spawn.
        return

    if _launchagent_installed():
        raise LaunchAgentNotRunningError(LaunchAgentNotRunningError.KICKSTART_HINT)

    plan = _build_spawn_plan(idle_shutdown_s=idle_shutdown_s, socket_path=socket_path)

    if stderr_emitter is not None:
        stderr_emitter("Starting Screencap daemon...")

    # Bounded retry across the spawn-race window — two parallel CLI
    # invocations might both reach this branch; only one wins the
    # already-running detection on the daemon side.
    last_log_tail = ""
    for attempt in range(2):
        deadline = time.monotonic() + readiness_timeout_s
        try:
            pid = _spawn_daemon(plan)
        except OSError as exc:
            raise DaemonAutoSpawnError(
                f"posix_spawn failed: {exc}",
                log_tail=_read_log_tail(_AUTO_LOG_PATH),
            ) from exc

        if _poll_socket_ready(target, deadline=deadline, spawned_pid=pid):
            if stderr_emitter is not None:
                stderr_emitter("Daemon ready.")
            return

        _kill_pid_if_alive(pid)
        last_log_tail = _read_log_tail(_AUTO_LOG_PATH)

        # If a racing spawn won (we exited fast with already-running),
        # the socket may now be reachable thanks to the winning daemon.
        if _socket_reachable(target):
            if stderr_emitter is not None:
                stderr_emitter("Daemon ready.")
            return

        if attempt == 0:
            logger.debug("auto-spawn attempt 1 failed; retrying")

    raise DaemonAutoSpawnError(
        f"Screencap daemon failed to start within {readiness_timeout_s:.0f}s. "
        f"Last log lines from {_AUTO_LOG_PATH}:",
        log_tail=last_log_tail,
    )


__all__ = [
    "DEFAULT_IDLE_SHUTDOWN_S",
    "DaemonAutoSpawnError",
    "LaunchAgentNotRunningError",
    "ensure_daemon_or_spawn",
]
