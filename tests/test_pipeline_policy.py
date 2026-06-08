"""Tests for screencap.pipeline_policy — the U3 destination/retention resolver."""

import json
import os
from unittest import mock

import pytest

from screencap.pipeline_policy import (
    Destination,
    ResolvedPolicy,
    RetentionPolicy,
    resolve_policy,
    set_default_override,
)


@pytest.fixture(autouse=True)
def _reset_config_and_override():
    """Reset the config cache and the process-wide override between tests."""
    import screencap.config as cfg

    cfg._config_cache = None
    set_default_override(None)
    # Clear retention/auto-delete env so config defaults are deterministic.
    drop = {
        "SCREENCAP_RETENTION_POLICY",
        "SCREENCAP_RETENTION_DAYS",
        "SCREENCAP_RETENTION_SIZE_CAP_MB",
        "SCREENCAP_AUTO_DELETE",
    }
    env = {k: v for k, v in os.environ.items() if k not in drop}
    with mock.patch.dict(os.environ, env, clear=True):
        yield
    cfg._config_cache = None
    set_default_override(None)


# --- happy path: defaults + per-destination ---


def test_default_retention_is_keep_forever():
    """Unset config → keep_forever default for every destination (R11)."""
    import screencap.config as cfg

    cfg._config_cache = {}
    for dest in ("local", "cloud", "both"):
        p = resolve_policy(destination=dest)
        assert p.retention_policy is RetentionPolicy.KEEP_FOREVER
        assert p.params == {}
        assert p.destination is Destination(dest)


def test_resolver_returns_configured_retention():
    """Configured retention flows through for local/cloud/both."""
    import screencap.config as cfg

    cfg._config_cache = {"retention": {"policy": "delete_after_days", "days": 30}}
    for dest in ("local", "cloud", "both"):
        p = resolve_policy(destination=dest)
        assert p.retention_policy is RetentionPolicy.DELETE_AFTER_DAYS
        assert p.params == {"days": 30}


def test_size_cap_params_flow_through():
    import screencap.config as cfg

    cfg._config_cache = {"retention": {"policy": "size_cap", "size_cap_mb": 5000}}
    p = resolve_policy(destination="local")
    assert p.retention_policy is RetentionPolicy.SIZE_CAP
    assert p.params == {"size_cap_mb": 5000}


def test_resolved_policy_is_frozen_value_object():
    """ResolvedPolicy is immutable (a frozen value object to freeze on disk)."""
    p = resolve_policy(destination="local")
    with pytest.raises((AttributeError, TypeError)):
        p.destination = Destination.CLOUD  # type: ignore[misc]


# --- Covers AE6: the account gate (R16) ---


def test_account_gate_cloud_and_both_require_account():
    assert resolve_policy(destination="cloud").requires_account is True
    assert resolve_policy(destination="both").requires_account is True


def test_account_gate_local_requires_no_account():
    assert resolve_policy(destination="local").requires_account is False


# --- freeze semantics ---


def test_freeze_config_change_does_not_change_frozen_policy():
    """A config change AFTER a policy is frozen does not change it.

    The frozen ResolvedPolicy is a value object; once serialized into
    per-recording state it is read back as-is, never re-resolved.
    """
    import screencap.config as cfg

    cfg._config_cache = {"retention": {"policy": "keep_forever"}}
    frozen = resolve_policy(destination="cloud")
    persisted = json.loads(json.dumps(frozen.to_dict()))  # round-trip on disk

    # Config changes underneath the recording.
    cfg._config_cache = {"retention": {"policy": "delete_after_days", "days": 7}}

    # The recording's frozen policy is unchanged — read it back, don't re-resolve.
    restored = ResolvedPolicy.from_dict(persisted)
    assert restored == frozen
    assert restored.retention_policy is RetentionPolicy.KEEP_FOREVER
    # A NEW recording, by contrast, picks up the new config.
    new = resolve_policy(destination="cloud")
    assert new.retention_policy is RetentionPolicy.DELETE_AFTER_DAYS


# --- seam: the single attachment point ---


def test_override_changes_routing_for_new_recordings_only():
    """A stubbed plan-tier override re-routes NEW recordings via the seam.

    Proves the single attachment point works without re-forking the pipeline:
    a recording frozen BEFORE the override keeps its policy; one resolved
    AFTER picks up the override.
    """
    import screencap.config as cfg

    cfg._config_cache = {"retention": {"policy": "keep_forever"}}

    # Recording A is routed and frozen under no override.
    a = resolve_policy(destination="cloud")
    a_persisted = a.to_dict()
    assert a.destination is Destination.CLOUD

    # A plan-tier ships: free tier forced to local, no cloud routing.
    def free_tier(base: ResolvedPolicy) -> ResolvedPolicy:
        return ResolvedPolicy(
            destination=Destination.LOCAL,
            retention_policy=base.retention_policy,
            params=base.params,
        )

    set_default_override(free_tier)

    # Recording B (new) is re-routed by the seam — single attachment point.
    b = resolve_policy(destination="cloud")
    assert b.destination is Destination.LOCAL
    assert b.requires_account is False

    # Recording A's frozen policy is untouched (read back from disk).
    assert ResolvedPolicy.from_dict(a_persisted).destination is Destination.CLOUD


def test_per_call_override_takes_precedence():
    """A per-call override beats the default-registered one and config."""
    import screencap.config as cfg

    cfg._config_cache = {}

    def force_size_cap(base: ResolvedPolicy) -> ResolvedPolicy:
        return ResolvedPolicy(
            destination=base.destination,
            retention_policy=RetentionPolicy.SIZE_CAP,
            params={"size_cap_mb": 1000},
        )

    p = resolve_policy(destination="both", override=force_size_cap)
    assert p.retention_policy is RetentionPolicy.SIZE_CAP
    assert p.params == {"size_cap_mb": 1000}


def test_override_must_return_resolved_policy():
    set_default_override(lambda base: "not a policy")  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError):
        resolve_policy(destination="local")


# --- serialization round-trip ---


def test_to_dict_from_dict_round_trip():
    p = ResolvedPolicy(
        destination=Destination.BOTH,
        retention_policy=RetentionPolicy.DELETE_AFTER_DAYS,
        params={"days": 14},
    )
    assert ResolvedPolicy.from_dict(p.to_dict()) == p


def test_from_dict_legacy_intent_returns_none():
    """A legacy v1 .recording_intent (no policy fields) reads back as None."""
    legacy = {"version": 1, "destination": "cloud", "privacy_mode": "public"}
    assert ResolvedPolicy.from_dict(legacy) is None


def test_from_dict_malformed_returns_none():
    assert ResolvedPolicy.from_dict({"destination": "bogus", "retention_policy": "x"}) is None
    assert ResolvedPolicy.from_dict({}) is None
