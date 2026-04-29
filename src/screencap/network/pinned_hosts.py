"""Persistent pinned-hosts cache (``~/.screencap/known_pinned_hosts.json``).

V1 detects cert-pinned hosts at runtime: the first connection to a
pinned host fails (TLS handshake error), the addon adds the host to
``_runtime_tunnel_hosts``, and subsequent connections within the same
recording are tunneled (``ignore_connection=True``).

V1.5 makes that detection persistent across recordings. The addon
seeds its in-memory set from this file at construction time, so a
host detected in recording N never fails again in recording N+1.

The CLI command ``screencap network preload-pin <host>`` lets the
user manually add a known-pinned host (e.g. an internal banking app
they don't want to MITM-attempt) without having to record once and
fail.

File format: a JSON object ``{"hosts": ["host1", "host2", ...]}``
sorted lowercase. Plain JSON; not encrypted (host lists are not
sensitive — the user already chose to install the screencap CA).
"""
from __future__ import annotations

import json
from pathlib import Path

_DEFAULT_PATH = Path("~/.screencap/known_pinned_hosts.json").expanduser()


def _path(custom: Path | None = None) -> Path:
    return custom or _DEFAULT_PATH


def load_known_pinned_hosts(path: Path | None = None) -> set[str]:
    """Read the JSON file and return its ``hosts`` set.

    Returns an empty set when the file is missing, unreadable, or
    malformed — the failure mode for this cache is "behave like V1
    (one-recording-failure-per-host)", never "crash the recorder".
    """
    p = _path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(data, dict):
        return set()
    hosts = data.get("hosts", [])
    if not isinstance(hosts, list):
        return set()
    return {h.lower() for h in hosts if isinstance(h, str) and h}


def add_known_pinned_host(host: str, path: Path | None = None) -> bool:
    """Add ``host`` to the persistent set; create the file if needed.

    Returns ``True`` if a write happened, ``False`` if the host was
    already present (idempotent — used by the addon's ``error()`` hook
    on every cert-pin failure, which fires once per host per recording
    but may fire repeatedly across recordings).
    """
    if not host:
        return False
    p = _path(path)
    current = load_known_pinned_hosts(p)
    host_lc = host.lower()
    if host_lc in current:
        return False
    current.add(host_lc)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hosts": sorted(current)}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(p)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return True


def remove_known_pinned_host(host: str, path: Path | None = None) -> bool:
    """Remove ``host`` from the persistent set. Returns True iff removed."""
    if not host:
        return False
    p = _path(path)
    current = load_known_pinned_hosts(p)
    host_lc = host.lower()
    if host_lc not in current:
        return False
    current.remove(host_lc)
    payload = {"hosts": sorted(current)}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(p)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return True
