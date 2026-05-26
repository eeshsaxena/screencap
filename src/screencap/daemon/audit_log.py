"""Append-only audit log for daemon privileged verbs.

Records each invocation of ``/v0/recording.start`` and
``/v0/recording.stop`` as a single JSON line at
``~/.screencap/run/audit.log`` (mode 0o600, parent dir already 0o700
via ``socket.bind_unix_socket``). This is the forensic surface paired
with the same-EUID trust boundary documented in ``SECURITY.md``.

Best-effort by contract: a failure to open or write the audit log MUST
NOT propagate to the caller of the underlying verb. Audit-write errors
log a warning so operators can notice the regression.

Append-only contract: writes use ``O_APPEND | O_CREAT | O_WRONLY |
O_NOFOLLOW`` to prevent truncation races. ``O_NOFOLLOW`` blocks symlinks
at the leaf (``audit.log`` itself) only; it does NOT defend against
hardlinks or against a symlinked parent directory. Both of those —
together with any same-UID write to the log — remain inside the
accepted-out-of-scope same-UID trust boundary documented in
``SECURITY.md``. Rotation is intentionally deferred to a follow-up
ticket.

Single-writer invariant: the daemon serializes all audit writes through
its asyncio event loop, and the LaunchAgent + auto-spawn coordination
via ``pidfile`` enforces at most one daemon process per user. The
``O_APPEND`` flag still guarantees per-write atomicity for any
hypothetical multi-writer case.

Read-only verbs (``recording.list``, ``session.snapshot``,
``daemon.info``, ``events``) are intentionally not audited — they leak
no capability and auditing them would 10x log volume.

Audit fields preserve caller-supplied bytes inside JSON strings; analysts
using ``jq -r`` or ``cat`` on ``audit.log`` should account for raw
control characters in fields like ``recording_name``.
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


def _audit_log_path() -> Path:
    """Resolve the audit log path lazily so tests / runtime can monkeypatch
    ``HOME`` after this module is imported. Mirrors the lazy shape used by
    ``default_socket_path`` in ``screencap.daemon.socket``.
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
        Peer's executable path from ``proc_pidpath``, or ``None``. The
        ``provenance`` module documents this signal as racy by design;
        readers should treat ``peer_path`` and ``classification`` as
        advisory, not authoritative.
    classification:
        One of ``swiftui`` / ``cli`` / ``mcp`` / ``unknown`` (see
        ``screencap.daemon.provenance``).
    outcome:
        ``ok`` on success; lowercase error code (e.g., ``lock_contended``)
        on a typed ``DaemonAPIError``; ``internal_error`` on unhandled
        exceptions.
    extra:
        Optional verb-specific fields (e.g., ``recording_name``). Stored
        flat in the record. Canonical fields above always win on key
        collision — extra cannot overwrite ``verb``, ``outcome``, ``ts``,
        etc.

    Note
    ----
    The file I/O is synchronous and runs on the asyncio event loop.
    Recording start/stop are infrequent so the per-call cost is
    negligible on local disks. The plan flags network-mounted home dirs
    as a deferred risk; profile before wrapping in
    ``asyncio.to_thread``.
    """
    try:
        canonical: dict[str, Any] = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "verb": verb,
            "peer_pid": peer_pid,
            "peer_path": peer_path,
            "classification": classification,
            "outcome": outcome,
        }
        # Build extras first so canonical fields override on key collision —
        # callers cannot forge a verb or outcome via a kwarg name clash.
        record = {**extra, **canonical}
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    except Exception:
        # A non-serializable value in ``extra`` (e.g., a ``Path``) raises
        # ``TypeError`` from ``json.dumps``. Best-effort contract: never
        # propagate; never open the file; log so operators can spot it.
        logger.warning("audit_log: failed to serialize record", exc_info=True)
        return

    # Wrap the create-then-chmod in a tight umask to close the brief
    # window where a freshly-created file would otherwise pick up the
    # process umask before fchmod tightens it (matches the pattern used
    # by ``socket.bind_unix_socket``).
    path = _audit_log_path()
    old_umask = os.umask(0o077)
    try:
        fd = os.open(
            path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,
            _AUDIT_FILE_MODE,
        )
    except OSError:
        logger.warning("audit_log: could not open %s", path, exc_info=True)
        return
    finally:
        os.umask(old_umask)

    try:
        # Tighten the file mode on every write so a pre-existing file at a
        # relaxed mode (only same-UID can land it there, but worth the
        # belt-and-braces) becomes 0o600 on first audited verb.
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


__all__ = ["record_verb"]
