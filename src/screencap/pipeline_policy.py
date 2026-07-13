"""Destination & retention policy resolver — the monetization seam (U3).

This module separates **data** from **resolution**:

* :class:`ResolvedPolicy` is the *value object* — ``{destination,
  retention_policy, params}`` — that is **resolved once at routing time and
  frozen into per-recording state** (persisted into ``.recording_intent`` by
  ``engine/lock_policy.py``, read back by ``catalog.read_intent_policy``).
  Freezing per recording is load-bearing: a later config edit or plan-tier
  change CANNOT retroactively re-route an in-flight recording whose chunks
  may already have been scrubbed/uploaded under the frozen policy.

* :func:`resolve_policy` is the *resolver function* — **the single R13
  attachment point** where a future plan-tier override hooks in. It reads
  config defaults and returns a :class:`ResolvedPolicy`. A future tier gate
  slots into ONE place (the ``override`` hook) without re-forking the
  pipeline. **No metering / quota / billing is built here** — only the
  override slot exists.

The account gate (R16) lives in the resolver's cloud branch and is exposed
as :attr:`ResolvedPolicy.requires_account`: ``cloud`` / ``both`` require an
account; ``local`` never does. Auth itself is NOT rebuilt here — see
``screencap.auth``; the resolver only carries the boolean requirement so a
caller (U7) can enforce it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# --------------------------------------------------------------------------
# Closed-set value types
# --------------------------------------------------------------------------


class Destination(str, Enum):
    """Where a recording's processed artifacts converge at the terminal stage.

    A ``str`` enum so it round-trips cleanly through the ``.recording_intent``
    JSON (the existing ``destination`` field already carries these literals).
    """

    LOCAL = "local"
    CLOUD = "cloud"
    BOTH = "both"


class RetentionPolicy(str, Enum):
    """How long local artifacts are kept. ``KEEP_FOREVER`` is the default so
    existing local behavior is unchanged unless a cap is set (R11)."""

    KEEP_FOREVER = "keep_forever"
    DELETE_AFTER_UPLOAD = "delete_after_upload"
    DELETE_AFTER_DAYS = "delete_after_days"
    SIZE_CAP = "size_cap"


# Destinations whose routing branch uploads to the cloud and therefore
# requires an account (R16). ``local`` is deliberately absent.
_CLOUD_DESTINATIONS = frozenset({Destination.CLOUD, Destination.BOTH})


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    """The frozen, per-recording policy value object.

    Resolved once at routing time and persisted into per-recording state.
    Immutable (``frozen``) so nothing can mutate a recording's policy after
    it is frozen — a later config/tier change produces a *different*
    ``ResolvedPolicy`` for *new* recordings only.

    Attributes:
        destination: ``local`` / ``cloud`` / ``both``.
        retention_policy: the resolved retention policy.
        params: policy params, e.g. ``{"days": 30}`` for
            ``DELETE_AFTER_DAYS`` or ``{"size_cap_mb": 5000}`` for
            ``SIZE_CAP``; ``{}`` otherwise. U8 reads these to evict.
    """

    destination: Destination
    retention_policy: RetentionPolicy
    params: dict = field(default_factory=dict)

    @property
    def requires_account(self) -> bool:
        """R16 account gate: cloud/both require auth; local never does.

        The terminal stage (U7) enforces this; the resolver only declares it.
        """
        return self.destination in _CLOUD_DESTINATIONS

    # --- (de)serialization for the frozen-into-.recording_intent round-trip ---

    def to_dict(self) -> dict:
        """Serialize for persistence into ``.recording_intent``."""
        return {
            "destination": self.destination.value,
            "retention_policy": self.retention_policy.value,
            "retention_params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Optional["ResolvedPolicy"]:
        """Reconstruct a frozen policy from persisted ``.recording_intent`` data.

        Returns ``None`` when the policy fields are absent (a legacy
        ``.recording_intent`` written before U3) or malformed, so callers can
        treat sparse-but-valid recordings as first-class.
        """
        try:
            dest = data.get("destination")
            retention = data.get("retention_policy")
            if dest is None or retention is None:
                return None
            params = data.get("retention_params") or {}
            if not isinstance(params, dict):
                return None
            return cls(
                destination=Destination(dest),
                retention_policy=RetentionPolicy(retention),
                params=dict(params),
            )
        except (ValueError, AttributeError, TypeError):
            return None


# --------------------------------------------------------------------------
# The resolver — the SINGLE R13 attachment point
# --------------------------------------------------------------------------

# A plan-tier override hook. ``None`` = no override (the only production state
# today — no metering/billing is built). A future plan-tier installs ONE
# callable here that may rewrite the to-be-frozen policy (e.g. force
# ``local`` for a free tier, or grant a longer retention cap for a paid tier).
# It receives the config-default policy and returns the policy to freeze.
PolicyOverride = Callable[[ResolvedPolicy], ResolvedPolicy]


def resolve_policy(
    *,
    destination: str | Destination,
    ambient: bool = False,
    override: PolicyOverride | None = None,
) -> ResolvedPolicy:
    """Resolve the ``ResolvedPolicy`` to FREEZE for a recording (the R13 seam).

    This is the ONE place destination + retention are decided. A future
    plan-tier gates cloud routing / retention here by passing an ``override``
    (or by a registered default override — see :func:`set_default_override`)
    — without touching any pipeline code. In-flight recordings are unaffected
    because their policy was already frozen.

    Args:
        destination: ``local`` / ``cloud`` / ``both`` (the user/config intent
            for this recording). Accepts the enum or its string value.
        ambient: True for the SCR-214 always-on ambient stream. An ambient
            recording overrides the config retention default with
            ``DELETE_AFTER_DAYS`` at a user-configurable window
            (``config.get_ambient_retention_days``, default 30) so raw footage
            rolls off (R13/KTD5); non-ambient recordings keep the config default
            (``keep_forever``). This resolved value is FROZEN per recording, so a
            later window-default change applies to FUTURE ambient days only —
            each existing day keeps the window it started with.
        override: optional per-call plan-tier hook (the seam). Receives the
            config-default policy and returns the policy to freeze. Defaults
            to the process-wide default override if one is registered.

    Returns:
        The frozen :class:`ResolvedPolicy`. Its retention defaults come from
        config (:func:`screencap.config.get_retention_policy`); ``params``
        carries any ``days`` / ``size_cap_mb``.

    Note:
        Retention is read from config and applies to the *local* copy. For a
        ``cloud``-only recording with no surviving local copy the retention
        policy still describes the local copy's lifetime up to eviction; for
        ``both`` the masked cloud copy is evicted immediately post-upload by
        a fixed rule (not this configurable retention) — see the plan's
        ``both`` semantics. U7/U8 interpret these fields.
    """
    dest = destination if isinstance(destination, Destination) else Destination(destination)

    # Config provides the retention default. Importing here (not at module
    # load) keeps ``screencap --help`` fast and avoids a config import cycle.
    from screencap.config import get_retention_policy

    policy_name, params = get_retention_policy()

    if ambient:
        # SCR-214 U8/KTD5: an always-on, full-fidelity ambient stream CANNOT
        # inherit the keep_forever default (R11) — raw footage must roll off, so
        # ambient resolves to DELETE_AFTER_DAYS with a user-configurable window
        # (default 30). Non-ambient recordings are untouched. Because this
        # resolved policy is FROZEN into ``.recording_intent`` at start, a later
        # change to the window default applies to FUTURE ambient days only; each
        # existing day keeps the window it started with (surface this in the
        # ambient-retention setting copy).
        from screencap.config import get_ambient_retention_days

        policy_name = RetentionPolicy.DELETE_AFTER_DAYS.value
        params = {"days": get_ambient_retention_days()}

    base = ResolvedPolicy(
        destination=dest,
        retention_policy=RetentionPolicy(policy_name),
        params=dict(params),
    )

    # THE single attachment point. A future plan-tier rewrites routing here;
    # today no override is installed, so `base` is returned unchanged.
    hook = override if override is not None else _DEFAULT_OVERRIDE
    if hook is not None:
        resolved = hook(base)
        if not isinstance(resolved, ResolvedPolicy):
            raise TypeError(
                "policy override must return a ResolvedPolicy, got "
                f"{type(resolved).__name__}"
            )
        return resolved
    return base


# Process-wide default override slot. Stays ``None`` in production until a
# plan-tier is built (R13: the slot exists; metering does not). Tests register
# a stub here to prove the seam changes routing for NEW recordings only.
_DEFAULT_OVERRIDE: PolicyOverride | None = None


def set_default_override(override: PolicyOverride | None) -> None:
    """Install (or clear with ``None``) the process-wide plan-tier override.

    The future-plan-tier attachment point. Registering an override changes
    :func:`resolve_policy` for *subsequent* calls only; already-frozen
    per-recording policies are untouched (they live in ``.recording_intent``,
    not re-resolved). Pass ``None`` to clear.
    """
    global _DEFAULT_OVERRIDE
    _DEFAULT_OVERRIDE = override
