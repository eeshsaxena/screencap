"""Bounded last-known-good entitlement lease (KTD-4, U14).

The local recording + recall gates (U8/U9 — the **consumers** of this module) must
NOT read ``auth.whoami()`` live on every check. ``whoami``'s ``tier`` claim is baked
into the daemon's cached ID token, which only re-materializes on a refresh (~1h
buffer), and a naive "grace-allow whenever the token is stale/offline" rule is a
durable, scriptable bypass: a non-payer who blocks the daemon's network path stays
perpetually stale and records free forever.

Instead, on every **successful** token refresh the daemon writes a **lease** here —
the last-known-good ``tier`` plus a hard expiry (default 72h). The gate reads the
lease:

* within its window it grace-allows the cached ``tier`` (an offline paying user is
  never locked out — R8);
* once it **expires** with no successful refresh the gate blocks (closing the
  perpetual-offline hole);
* a fresh, network-confirmed *not-entitled* result clears the lease immediately
  (:func:`clear_lease`).

The just-converted case is re-opened promptly by the U14 daemon re-mint
(``/v0/entitlement.refresh`` → force ``get_id_token(force_refresh=True)`` →
:func:`write_lease`), not by waiting out the ~1h token buffer.

**Consumers (do not implement here):** U8 (``recording.start`` gate) and U9
(recall-verb gate) import :func:`lease_entitled_tier` / :func:`is_entitled` to decide
whether to allow. This module owns only the store + read/write/clear + the
unexpired-tier helper; the allow/deny policy and the ``whoami``-ambiguity handling
(stale / ``signed_in:false``-with-no-``stale`` → *preserve* the current lease, never a
definitive block) live in the gates. The daemon/CLI write side calls
:func:`reconcile_from_whoami`, the single write policy.

Storage: a small JSON file ``~/.screencap/run/entitlement.lease`` (mode ``0o600``,
parent dir ``0o700``), mirroring the run-dir secret-file hardening used by the engine
token (``O_NOFOLLOW`` tmp write + atomic rename + a re-asserted ``chmod``). The lease
is not a secret per se, but it is a *same-EUID* entitlement fact and shares the run
dir's trust boundary (see SECURITY.md); the perms match the neighbours.

Time is injected (``now`` parameter) so tests can control expiry without sleeping;
production callers pass nothing and get wall-clock ``time.time()``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Default lease lifetime: 72h. Long enough that an offline paying user is never
# locked out across a normal disconnection, short enough that a lapsed non-payer
# who stays offline is eventually gated (the perpetual-offline hole closes). The
# ~1h token-refresh tail plus this window is the accepted local revocation bound
# (KTD-4, consistent with the cloud gate's KTD-2 bound).
DEFAULT_LEASE_TTL_SECONDS = 72 * 60 * 60

_LEASE_FILENAME = "entitlement.lease"
_LEASE_FILE_MODE = 0o600
_LEASE_DIR_MODE = 0o700


@dataclass(frozen=True)
class Lease:
    """A persisted last-known-good entitlement fact.

    ``tier`` is the ``whoami`` tier claim at the moment of the last successful
    refresh — ``None`` means "signed in but no positively-resolved paid tier"
    (a network-confirmed not-entitled state, distinct from *unknown*). ``expires_at``
    is epoch seconds; past it the lease no longer entitles.
    """

    tier: str | None
    expires_at: float

    def is_valid(self, now: float) -> bool:
        """True if this lease has not yet expired at wall-clock ``now``."""
        return now < self.expires_at


def _lease_path() -> Path:
    from screencap.config import get_base_dir

    run_dir = get_base_dir() / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    # Re-assert 0o700 on every access, mirroring socket._ensure_socket_directory:
    # the run dir may pre-exist at a relaxed mode. Best-effort — a chmod failure
    # must not break entitlement reads/writes.
    try:
        os.chmod(run_dir, _LEASE_DIR_MODE)
    except OSError:
        logger.debug("entitlement_lease: could not chmod run dir", exc_info=True)
    return run_dir / _LEASE_FILENAME


def _atomic_write_lease_file(path: Path, payload: bytes) -> None:
    """Atomically write ``payload`` to ``path`` at mode 0o600 (same-EUID only).

    ``O_NOFOLLOW`` rejects a pre-planted symlink at the tmp path so a same-EUID
    actor cannot redirect the write; a failed replace unlinks the tmp so no residue
    is left in the run dir. Mirrors ``supervisor.Supervisor._atomic_write_secret_file``
    (the engine-token/cloud-key writer) so the run-dir hardening is uniform.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(
        str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, _LEASE_FILE_MODE
    )
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    try:
        os.replace(str(tmp), str(path))
        os.chmod(path, _LEASE_FILE_MODE)  # re-assert in case the file pre-existed wider
    except OSError:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def write_lease(
    tier: str | None,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    *,
    now: float | None = None,
) -> Lease:
    """Persist a fresh last-known-good lease and return it.

    Called by the daemon on every **successful** token refresh (the regular
    ``whoami`` cadence) and on the post-checkout re-mint signal. ``tier`` is the
    ``whoami`` tier claim; ``ttl_seconds`` sets the hard expiry from ``now``
    (wall-clock ``time.time()`` when ``now`` is omitted — injected in tests).

    A negative/zero ``ttl_seconds`` yields an already-expired lease (harmless; the
    reader treats it as not-entitled). The write is atomic + 0o600; an ``OSError``
    propagates so the caller can decide (the daemon wiring logs + swallows, so a
    lease-write hiccup never aborts a refresh).
    """
    resolved_now = time.time() if now is None else now
    lease = Lease(tier=tier, expires_at=resolved_now + ttl_seconds)
    payload = json.dumps(
        {"tier": lease.tier, "expires_at": lease.expires_at},
        separators=(",", ":"),
    ).encode("utf-8")
    _atomic_write_lease_file(_lease_path(), payload)
    return lease


def read_lease() -> Lease | None:
    """Return the persisted lease, or ``None`` if absent/unreadable/malformed.

    Fail-safe by construction: a missing file, an unreadable file, or a corrupt /
    schema-drifted payload all resolve to ``None`` — the gate then treats the state
    as *no valid lease* and (per its own ambiguity policy) blocks only once nothing
    entitles. Never raises.
    """
    try:
        raw = _lease_path().read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
        expires_at = data["expires_at"]
        tier = data.get("tier")
    except (ValueError, KeyError, TypeError):
        logger.debug("entitlement_lease: malformed lease file", exc_info=True)
        return None
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return None
    if tier is not None and not isinstance(tier, str):
        return None
    return Lease(tier=tier, expires_at=float(expires_at))


def clear_lease() -> None:
    """Remove the lease immediately (a fresh, network-confirmed not-entitled).

    Called when ``whoami`` positively resolves *not-entitled* (signed in, no paid
    tier): the last-known-good is now known bad, so the gate must block without
    waiting out the window. Idempotent + best-effort — a missing file is fine, an
    unlink failure is logged and swallowed (the lease will still expire on its own).
    """
    try:
        _lease_path().unlink(missing_ok=True)
    except OSError:
        logger.warning("entitlement_lease: could not clear lease file", exc_info=True)


def is_entitled(now: float | None = None) -> bool:
    """True if an unexpired lease exists carrying a positively-resolved paid tier.

    ``now`` defaults to wall-clock ``time.time()`` (injected in tests). Returns
    ``False`` for a missing lease, an expired lease, OR an unexpired lease whose
    ``tier`` is ``None`` (network-confirmed not-entitled) — the gate treats all
    three as not-entitled. Note this is the *lease* verdict only; the gates layer
    their own ``whoami``-ambiguity handling (stale → preserve, never block) on top.
    """
    return lease_entitled_tier(now) is not None


def lease_entitled_tier(now: float | None = None) -> str | None:
    """Return the cached ``tier`` iff the lease is unexpired and carries one, else ``None``.

    The primary read helper for the U8/U9 gates: an unexpired lease with a non-None
    ``tier`` grace-allows that tier; a missing / expired / tier-``None`` lease yields
    ``None`` (not-entitled). ``now`` is injected in tests.
    """
    resolved_now = time.time() if now is None else now
    lease = read_lease()
    if lease is None or not lease.is_valid(resolved_now):
        return None
    return lease.tier


def reconcile_from_whoami(info: dict) -> None:
    """Update the lease from a ``whoami`` result — the SINGLE KTD-4 write policy.

    Called by every daemon-context whoami touchpoint (the ``/v0/auth.whoami`` verb,
    the ``/v0/entitlement.refresh`` re-mint verb) and the CLI ``whoami`` post-checkout
    path, so the write/clear rule can never drift between them:

    * **Definitive success** — ``signed_in`` and NOT ``stale`` (the refresh landed,
      so ``tier`` is authoritative):
      * a string ``tier`` (positively-resolved paid tier) → :func:`write_lease`
        (last-known-good refreshed, offline grace re-armed);
      * ``tier`` absent/``None`` (network-confirmed *not-entitled*) → :func:`clear_lease`
        immediately, so the gate blocks without waiting out the window.
    * **Ambiguous** — ``stale`` (offline, couldn't refresh) OR not ``signed_in`` (an
      unexpected sign-out with no ``stale`` flag): **preserve** the current lease.
      Never a definitive block — a ``whoami`` hiccup or an offline payer must not be
      locked out (KTD-4).

    Best-effort: a lease-store ``OSError`` is swallowed (logged) so a store hiccup
    never aborts the request/command that triggered the reconcile.
    """
    if not info.get("signed_in") or info.get("stale"):
        return  # ambiguous — keep the current lease
    tier = info.get("tier")
    try:
        if isinstance(tier, str):
            write_lease(tier)
        else:
            clear_lease()
    except OSError:
        logger.warning("entitlement_lease: reconcile write failed", exc_info=True)
