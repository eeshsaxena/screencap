"""Append-only audit log for daemon privileged verbs.

Records each invocation of ``/v0/recording.start`` and
``/v0/recording.stop`` as a single JSON line at
``~/.screencap/run/audit.log`` (mode 0o600, parent dir already 0o700
via ``socket.bind_unix_socket``). This is the forensic surface paired
with the same-EUID trust boundary documented in ``SECURITY.md``.

Best-effort by contract: a failure to open or write the audit log MUST
NOT propagate to the caller of the underlying verb. Audit-write errors
log a warning so operators can notice the regression.

Append-only contract: writes use ``O_APPEND | O_CREAT | O_WRONLY`` to
prevent truncation races. Rotation is intentionally deferred to a
follow-up ticket.

Read-only verbs (``recording.list``, ``session.snapshot``,
``daemon.info``, ``events``) are intentionally not audited — they leak
no capability and auditing them would 10x log volume.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_AUDIT_FILE_MODE = 0o600


def audit_log_path() -> Path:
    """Resolve the audit-log path on the live filesystem.

    Re-resolved on each call so tests can override via
    ``monkeypatch.setattr`` without leaking state between runs.
    """
    return Path.home() / ".screencap" / "run" / "audit.log"


def record_verb(
    verb: str,
    *,
    peer_pid: int | None,
    peer_path: str | None,
    classification: str,
    outcome: str,
    **extra: Any,
) -> None:
    """Append a single JSON-lines audit record for ``verb``.

    Fire-and-forget: I/O failures degrade to a logger.warning and never
    propagate. Callers must not depend on the write succeeding.

    Parameters
    ----------
    verb:
        Daemon route identifier (``recording.start`` / ``recording.stop``).
    peer_pid:
        Peer's effective PID from ``LOCAL_PEEREPID``, or ``None`` when
        the peer was unreachable.
    peer_path:
        Peer's executable path from ``proc_pidpath``, or ``None``.
    classification:
        One of ``swiftui`` / ``cli`` / ``mcp`` / ``unknown`` (see
        ``screencap.daemon.provenance``).
    outcome:
        ``ok`` on success; lowercase error code (e.g., ``lock_contended``)
        on a typed ``DaemonAPIError``; ``internal_error`` on unhandled
        exceptions.
    extra:
        Optional verb-specific fields (e.g., ``recording_name``). Stored
        flat in the record alongside the canonical fields.
    """
    record: dict[str, Any] = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "verb": verb,
        "peer_pid": peer_pid,
        "peer_path": peer_path,
        "classification": classification,
        "outcome": outcome,
    }
    if extra:
        record.update(extra)
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")

    path = audit_log_path()
    try:
        fd = os.open(
            path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            _AUDIT_FILE_MODE,
        )
    except OSError:
        logger.warning("audit_log: could not open %s", path, exc_info=True)
        return

    try:
        # Tighten the file mode on every write so a pre-existing file at a
        # relaxed mode (the only way to land there is same-UID, but worth
        # the belt-and-braces) becomes 0o600 on first audited verb.
        try:
            os.fchmod(fd, _AUDIT_FILE_MODE)
        except OSError:
            logger.debug("audit_log: fchmod failed", exc_info=True)
        try:
            os.write(fd, line)
        except OSError:
            logger.warning("audit_log: write failed for %s", path, exc_info=True)
    finally:
        try:
            os.close(fd)
        except OSError:
            logger.debug("audit_log: close failed", exc_info=True)


__all__ = ["audit_log_path", "record_verb"]
