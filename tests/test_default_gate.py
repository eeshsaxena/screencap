"""Search-by-default capture gate (search guardrails U8 / KTD5 / R6 / R8).

The pure truth table (every readiness input toggled) plus the content-index
effective-default. Vision-free, config-isolated via env.
"""

from __future__ import annotations

import pytest

from screencap.capture_gate import resolve_capture_gate

pytestmark = pytest.mark.privacy


def _gate(**overrides):
    base = dict(
        explicit_capture_images=None,
        scrub_enabled=True,
        key_present=True,
        scrub_importable=True,
        retention_bound_set=True,
        disclosure_acknowledged=True,
        consent_declined=False,
        corpus_encrypted=True,
    )
    base.update(overrides)
    return resolve_capture_gate(**base)


# ---------------------------------------------------------------------------
# Explicit requests
# ---------------------------------------------------------------------------


def test_explicit_false_always_wins():
    # Even fully ready, an explicit opt-out is OFF.
    r = _gate(explicit_capture_images=False)
    assert r.capture_images is False
    assert r.capture_images_encrypted is False
    assert r.reason == "explicit_opt_out"


def test_explicit_true_clamped_when_protection_not_ready():
    # corpus encrypted but key missing → clamp OFF (R8 extends to explicit true).
    r = _gate(explicit_capture_images=True, key_present=False)
    assert r.capture_images is False
    assert r.reason == "explicit_true_clamped_protection_not_ready"


def test_explicit_true_on_when_ready_encrypted():
    r = _gate(explicit_capture_images=True)
    assert r.capture_images is True
    assert r.capture_images_encrypted is True
    assert r.reason == "explicit_true"


def test_explicit_true_preflight_plaintext_no_key_needed():
    # Pre-flip (corpus not encrypted) → explicit true keeps today's plaintext
    # behavior with no key/scrub requirement.
    r = _gate(explicit_capture_images=True, corpus_encrypted=False, key_present=False, scrub_importable=False)
    assert r.capture_images is True
    assert r.capture_images_encrypted is False
    assert r.reason == "explicit_true"


# ---------------------------------------------------------------------------
# Default-on gate (unset)
# ---------------------------------------------------------------------------


def test_default_on_when_all_ready():
    r = _gate()
    assert r.capture_images is True
    assert r.capture_images_encrypted is True
    assert r.reason == "default_on"


def test_consent_declined_forces_off():
    r = _gate(consent_declined=True)
    assert r.capture_images is False
    assert r.reason == "consent_declined"


def test_disclosure_not_acknowledged_forces_off():
    r = _gate(disclosure_acknowledged=False)
    assert r.capture_images is False
    assert r.reason == "disclosure_not_acknowledged"


@pytest.mark.parametrize(
    "override",
    [
        {"key_present": False},
        {"scrub_importable": False},
        {"scrub_enabled": False},
        {"retention_bound_set": False},
        {"corpus_encrypted": False},
    ],
)
def test_default_off_when_any_conjunct_missing(override):
    r = _gate(**override)
    assert r.capture_images is False
    assert r.reason == "default_off_not_ready"


def test_preflip_default_off():
    # The pre-U8 world: nothing flipped → unset default is OFF (today's behavior).
    r = _gate(corpus_encrypted=False, disclosure_acknowledged=False, retention_bound_set=False)
    assert r.capture_images is False


# ---------------------------------------------------------------------------
# content_index_enabled effective default
# ---------------------------------------------------------------------------


def test_content_index_effective_default(monkeypatch):
    from screencap import config

    monkeypatch.delenv("SCREENCAP_CONTENT_INDEX", raising=False)
    monkeypatch.setattr(config, "_load_toml", lambda: {})  # nothing set in config.toml

    # Pre-flip: corpus not encrypted → default OFF.
    monkeypatch.setattr(config, "get_corpus_encrypted", lambda: False)
    monkeypatch.setattr(config, "get_search_disclosure_acknowledged", lambda: True)
    monkeypatch.setattr(config, "get_content_index_consent_declined", lambda: False)
    assert config.get_content_index_enabled() is False

    # Guardrails-on: encrypted + disclosure + not declined → default ON.
    monkeypatch.setattr(config, "get_corpus_encrypted", lambda: True)
    assert config.get_content_index_enabled() is True

    # Consent declined always wins.
    monkeypatch.setattr(config, "get_content_index_consent_declined", lambda: True)
    assert config.get_content_index_enabled() is False


def test_content_index_explicit_config_wins(monkeypatch):
    from screencap import config

    monkeypatch.delenv("SCREENCAP_CONTENT_INDEX", raising=False)
    monkeypatch.setattr(config, "_load_toml", lambda: {"content_index_enabled": False})
    monkeypatch.setattr(config, "get_corpus_encrypted", lambda: True)
    monkeypatch.setattr(config, "get_search_disclosure_acknowledged", lambda: True)
    # Explicit config value overrides the readiness default.
    assert config.get_content_index_enabled() is False
