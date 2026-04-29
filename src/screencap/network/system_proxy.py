"""System proxy snapshot/set/restore via macOS `networksetup` + `osascript`.

The engine layer is the SINGLE OWNER of system-proxy lifecycle for a
``--network`` recording. Top-level ``recorder.py`` never touches system
proxy state. See plan F1-v6/F1-v7.

All mutating commands run through a SINGLE ``osascript with administrator
privileges`` invocation so macOS's ~5-minute auth cache covers all
services in one prompt. Snapshots are atomic-write (`.tmp` + `os.replace`)
so restore-on-abnormal-termination always sees a coherent state.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console(stderr=True)

_NETWORKSETUP = "/usr/sbin/networksetup"
_OSASCRIPT = "/usr/bin/osascript"

# Hardcoded; never user-configurable. Validated as IP literal in set_proxy_all.
_PROXY_HOST = "127.0.0.1"


class SystemProxyError(Exception):
    """Raised when a `networksetup` or `osascript` command fails."""


@dataclass(frozen=True)
class ServiceProxyState:
    """Per-service proxy snapshot taken before a recording starts."""

    service_name: str
    web_proxy: dict[str, Any] | None = None
    secure_web_proxy: dict[str, Any] | None = None
    bypass_domains: tuple[str, ...] = ()


def list_active_services() -> list[str]:
    """Return enabled service names; skip ``*``-prefixed disabled lines.

    The first line of `networksetup -listallnetworkservices` is a header
    ("An asterisk (*) denotes that a network service is disabled.") which
    we skip. Disabled services start with ``*`` and are excluded.
    """
    result = subprocess.run(
        [_NETWORKSETUP, "-listallnetworkservices"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemProxyError(
            f"networksetup -listallnetworkservices failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    services: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip the leading header line.
        if stripped.startswith("An asterisk"):
            continue
        if stripped.startswith("*"):
            continue
        services.append(stripped)
    return services


def _parse_proxy_block(stdout: str) -> dict[str, Any]:
    """Parse `networksetup -getwebproxy` / `-getsecurewebproxy` output.

    Output shape:
        Enabled: Yes|No
        Server: <host>
        Port: <int>
        Authenticated Proxy Enabled: 0|1
    """
    result: dict[str, Any] = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # Normalise booleans + ints
        if key == "Port":
            try:
                result[key] = int(value) if value else 0
            except ValueError:
                result[key] = 0
        elif key in {"Enabled", "Authenticated Proxy Enabled"}:
            result[key] = value
        else:
            result[key] = value
    return result


def _parse_bypass_domains(stdout: str) -> tuple[str, ...]:
    domains: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # macOS prints "There aren't any bypass domains set on <Service>." when empty.
        if line.startswith("There aren't any") or line.startswith("There are no"):
            continue
        domains.append(line)
    return tuple(domains)


def snapshot_service(service_name: str) -> ServiceProxyState:
    """Snapshot one service's web/secure-web proxy + bypass domains."""
    web = subprocess.run(
        [_NETWORKSETUP, "-getwebproxy", service_name],
        capture_output=True,
        text=True,
        check=False,
    )
    secure = subprocess.run(
        [_NETWORKSETUP, "-getsecurewebproxy", service_name],
        capture_output=True,
        text=True,
        check=False,
    )
    bypass = subprocess.run(
        [_NETWORKSETUP, "-getproxybypassdomains", service_name],
        capture_output=True,
        text=True,
        check=False,
    )
    return ServiceProxyState(
        service_name=service_name,
        web_proxy=_parse_proxy_block(web.stdout) if web.returncode == 0 else None,
        secure_web_proxy=(
            _parse_proxy_block(secure.stdout) if secure.returncode == 0 else None
        ),
        bypass_domains=_parse_bypass_domains(bypass.stdout) if bypass.returncode == 0 else (),
    )


def snapshot_all() -> dict[str, ServiceProxyState]:
    """Return ``{service_name: ServiceProxyState}`` for all enabled services."""
    return {svc: snapshot_service(svc) for svc in list_active_services()}


def write_snapshot(
    snapshot: dict[str, ServiceProxyState],
    path: Path,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    """Atomic-write the snapshot to ``path``.

    ``extra`` carries side-band fields (``worker_pid``, ``worker_create_time``,
    ``cmdline_tail``, ``recording_id``, ``snapshot_path``) used by the orphan
    detection path in ``lifecycle.restore_orphaned_proxy_state``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "services": {name: asdict(state) for name, state in snapshot.items()},
        "extra": extra or {},
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)
    # File mode 600 — snapshot reveals which services were enabled (interface
    # configuration leak) so restrict to the user.
    os.chmod(path, 0o600)


def read_snapshot(path: Path) -> tuple[dict[str, ServiceProxyState], dict[str, Any]]:
    """Read a snapshot back. Returns ``(services, extra)``.

    Raises ``FileNotFoundError`` if the path is missing; ``json.JSONDecodeError``
    if the file is corrupted (caller decides whether to treat as absent).
    """
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    raw_services = payload.get("services", {})
    services: dict[str, ServiceProxyState] = {}
    for name, raw in raw_services.items():
        services[name] = ServiceProxyState(
            service_name=raw.get("service_name", name),
            web_proxy=raw.get("web_proxy"),
            secure_web_proxy=raw.get("secure_web_proxy"),
            bypass_domains=tuple(raw.get("bypass_domains", []) or ()),
        )
    return services, payload.get("extra", {})


def _build_set_commands(host: str, port: int, services: list[str]) -> str:
    """Return the `;`-joined networksetup commands to enable proxy on each
    service. All values are shell-quoted to defeat injection via service names.
    """
    if not services:
        return ":"  # no-op shell command
    cmds: list[str] = []
    qhost = shlex.quote(host)
    qport = shlex.quote(str(port))
    for svc in services:
        qsvc = shlex.quote(svc)
        cmds.append(f"{shlex.quote(_NETWORKSETUP)} -setwebproxy {qsvc} {qhost} {qport}")
        cmds.append(f"{shlex.quote(_NETWORKSETUP)} -setsecurewebproxy {qsvc} {qhost} {qport}")
        cmds.append(f"{shlex.quote(_NETWORKSETUP)} -setwebproxystate {qsvc} on")
        cmds.append(f"{shlex.quote(_NETWORKSETUP)} -setsecurewebproxystate {qsvc} on")
    return "; ".join(cmds)


def _run_admin_osascript(shell_command: str, prompt: str) -> None:
    """Run ``shell_command`` via ``osascript with administrator privileges``."""
    # osascript -e arguments: the AppleScript runs `do shell script "..." with
    # administrator privileges with prompt "..."`. Shell-escape the *AppleScript*
    # string, which means escaping `"` and `\` in the inner shell command.
    escaped_shell = shell_command.replace("\\", "\\\\").replace('"', '\\"')
    escaped_prompt = prompt.replace("\\", "\\\\").replace('"', '\\"')
    apple_script = (
        f'do shell script "{escaped_shell}" '
        f'with administrator privileges '
        f'with prompt "{escaped_prompt}"'
    )
    result = subprocess.run(
        [_OSASCRIPT, "-e", apple_script],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,  # 5 min — covers AFK admin dialog with margin.
    )
    if result.returncode != 0:
        raise SystemProxyError(
            f"osascript admin command failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def set_proxy_all(host: str, port: int, services: list[str], *, prompt: str | None = None) -> None:
    """Enable web + secure-web proxy on every service via a single admin prompt."""
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"invalid port {port!r}")
    if host != _PROXY_HOST:
        # Defense-in-depth — the addon only ever listens on 127.0.0.1.
        raise ValueError(f"unexpected proxy host {host!r}; must be {_PROXY_HOST}")
    shell = _build_set_commands(host, port, services)
    _run_admin_osascript(
        shell,
        prompt
        or "screencap needs to configure your network proxy for capture (will be restored on stop)",
    )


def _build_restore_commands(snapshot: dict[str, ServiceProxyState]) -> str:
    cmds: list[str] = []
    for state in snapshot.values():
        qsvc = shlex.quote(state.service_name)
        web = state.web_proxy or {}
        sec = state.secure_web_proxy or {}
        # Restore web proxy
        if web.get("Server"):
            qhost = shlex.quote(str(web.get("Server", "")))
            qport = shlex.quote(str(web.get("Port", "0")))
            cmds.append(f"{shlex.quote(_NETWORKSETUP)} -setwebproxy {qsvc} {qhost} {qport}")
        was_web_enabled = (web.get("Enabled", "No") or "No").strip().lower() == "yes"
        cmds.append(
            f"{shlex.quote(_NETWORKSETUP)} -setwebproxystate {qsvc} "
            f"{'on' if was_web_enabled else 'off'}"
        )
        # Restore secure-web proxy
        if sec.get("Server"):
            qhost = shlex.quote(str(sec.get("Server", "")))
            qport = shlex.quote(str(sec.get("Port", "0")))
            cmds.append(f"{shlex.quote(_NETWORKSETUP)} -setsecurewebproxy {qsvc} {qhost} {qport}")
        was_sec_enabled = (sec.get("Enabled", "No") or "No").strip().lower() == "yes"
        cmds.append(
            f"{shlex.quote(_NETWORKSETUP)} -setsecurewebproxystate {qsvc} "
            f"{'on' if was_sec_enabled else 'off'}"
        )
    return "; ".join(cmds) if cmds else ":"


def restore_all(snapshot: dict[str, ServiceProxyState], *, prompt: str | None = None) -> None:
    """Restore each service's prior proxy state via a single admin prompt.

    Uses the macOS auth cache from a recent ``set_proxy_all`` call when within
    the ~5-minute window (no second prompt). Outside that window, prompts once.
    """
    if not snapshot:
        return
    shell = _build_restore_commands(snapshot)
    _run_admin_osascript(
        shell,
        prompt or "screencap is restoring your previous network proxy state",
    )


def services_changed_marker(
    start_services: list[str],
    stop_services: list[str],
    path: Path,
) -> bool:
    """Write a coverage-gap marker if the service list changed mid-recording.

    Returns True if a marker was written (services differed), False otherwise.
    """
    start_set = set(start_services)
    stop_set = set(stop_services)
    added = sorted(stop_set - start_set)
    removed = sorted(start_set - stop_set)
    if not added and not removed:
        return False
    payload = {
        "added": added,
        "removed": removed,
        "snapshot_at_start": sorted(start_services),
        "snapshot_at_stop": sorted(stop_services),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)
    return True
