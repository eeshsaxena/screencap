"""Regression: the daemon test harness must isolate ``config.toml`` reads.

Dogfood 2026-07-11: ``config._CONFIG_PATH`` is bound at import time from the
original ``_DEFAULT_BASE``, so the ``_isolate_recordings_and_run_dir`` autouse
fixture patching ``_DEFAULT_BASE`` alone did NOT redirect ``config.toml`` reads.
A developer with ``local_paywall_enforce = true`` in their real
``~/.screencap/config.toml`` (SCR-237 paywall testing) leaked that flag into the
daemon app under test, gating the read-only recall/search verbs with HTTP 402 and
failing six ``test_read_only_verbs`` cases that had nothing to do with billing.
These pin the isolation so the leak can't return.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import screencap.config as cfg


@pytest.mark.privacy
def test_config_path_is_redirected_off_real_home() -> None:
    """The autouse isolation fixture must redirect ``_CONFIG_PATH`` off the
    developer's real ``~/.screencap`` so ``config.toml`` reads can't leak in."""
    assert cfg._CONFIG_PATH.parent != (Path.home() / ".screencap")


@pytest.mark.privacy
def test_config_reads_resolve_the_isolated_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value written to the *isolated* config is what reads see — proving the
    read resolves the isolated path, not the developer's real config. Without the
    ``_CONFIG_PATH`` redirect this would read the real ``~/.screencap`` file."""
    # The env var short-circuits the config read; clear it so we exercise the file.
    monkeypatch.delenv("SCREENCAP_LOCAL_PAYWALL_ENFORCE", raising=False)

    # No isolated config file → the flag falls through to its default (ON, since
    # the paid-only launch; SCR). Prove the isolated PATH is what's read by then
    # writing the NON-default value: a False result can only come from the
    # isolated file being honored, not the default.
    cfg.invalidate_config_cache()
    assert cfg.get_local_paywall_enforced() is True

    cfg._CONFIG_PATH.write_text("local_paywall_enforce = false\n")
    cfg.invalidate_config_cache()
    assert cfg.get_local_paywall_enforced() is False
