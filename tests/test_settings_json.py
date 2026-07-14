"""Tests for `settings --json` privacy block (SCR-17 / Unit U1).

Verifies the read-side that the SwiftUI first-run banner consumes:
`mode`, `setup_skipped`, and `has_privacy_section` are exposed under a
nested `privacy` block. `has_privacy_section` must distinguish "absent"
from "present with default values" — the banner gates its first-launch
fail-closed mode write on this flag.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from screencap.cli import cli


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    import screencap.config

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    yield


def _write_config(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _invoke_settings_json() -> dict:
    runner = CliRunner()
    result = runner.invoke(cli, ["settings", "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _get_privacy(payload: dict) -> dict:
    assert "settings" in payload
    assert "privacy" in payload["settings"], (
        f"missing privacy block in {payload!r}"
    )
    return payload["settings"]["privacy"]


def test_privacy_block_absent_section_returns_defaults():
    """No `[privacy]` section: defaults emitted, has_privacy_section=False.

    Drives the SwiftUI first-launch fail-closed write: when this flag is
    False, the banner triggers `screencap settings privacy mode set internal`
    before showing the banner state.
    """
    payload = _invoke_settings_json()
    privacy = _get_privacy(payload)
    assert privacy == {
        "mode": "internal",
        "setup_skipped": False,
        "has_privacy_section": False,
    }


def test_privacy_block_mode_only(tmp_path):
    from screencap.config import _CONFIG_PATH

    _write_config(_CONFIG_PATH, '[privacy]\nmode = "public"\n')
    privacy = _get_privacy(_invoke_settings_json())
    assert privacy["mode"] == "public"
    assert privacy["setup_skipped"] is False
    assert privacy["has_privacy_section"] is True


def test_privacy_block_setup_skipped_only(tmp_path):
    from screencap.config import _CONFIG_PATH

    _write_config(_CONFIG_PATH, "[privacy]\nsetup_skipped = true\n")
    privacy = _get_privacy(_invoke_settings_json())
    assert privacy["setup_skipped"] is True
    assert privacy["mode"] == "internal"
    assert privacy["has_privacy_section"] is True


def test_privacy_block_empty_section_is_present():
    """Empty `[privacy]` table is still considered present — the user has
    interacted with privacy config. Distinguishes from "never written"."""
    from screencap.config import _CONFIG_PATH

    _write_config(_CONFIG_PATH, "[privacy]\n")
    privacy = _get_privacy(_invoke_settings_json())
    assert privacy["has_privacy_section"] is True
    assert privacy["mode"] == "internal"
    assert privacy["setup_skipped"] is False


def test_privacy_block_invalid_mode_falls_back_to_internal():
    """A malformed mode in config does not raise; it falls back to internal.

    Mirrors `PrivacyConfig`'s permissiveness — the pane surfaces state, it
    is not the place to reject malformed configs at read time.
    """
    from screencap.config import _CONFIG_PATH

    _write_config(_CONFIG_PATH, '[privacy]\nmode = "paranoid"\n')
    privacy = _get_privacy(_invoke_settings_json())
    assert privacy["mode"] == "internal"
    assert privacy["has_privacy_section"] is True


def test_settings_schema_version_bumped():
    """The schema version bump signals the additive `privacy` block to
    consumers that care. The bump is exposed at the top level and consumers
    must continue parsing the rest of the payload regardless of version."""
    payload = _invoke_settings_json()
    assert payload["ok"] is True
    assert payload["schema_version"] >= 2


def test_existing_settings_fields_unchanged():
    """The privacy block is additive — v1 consumers reading other fields
    must not be affected. Sanity that the bump did not drop fields."""
    settings = _invoke_settings_json()["settings"]
    for key in (
        "show_on_website",
        "upload_default",
        "audio_default",
        "chunk_duration",
        "auto_delete_after_upload",
        "rest_threshold_seconds",
        "recordings_dir",
    ):
        assert key in settings, f"missing {key} in settings payload"


def test_auto_name_removed_and_schema_bumped():
    """Legacy LLM auto-naming was removed (2026-07-11 plan): the `auto_name`
    key is gone from the payload and the field removal is signalled by a
    schema_version bump to v3."""
    payload = _invoke_settings_json()
    assert "auto_name" not in payload["settings"]
    assert payload["schema_version"] >= 3


@pytest.mark.parametrize("pair", ["auto_name=true", "auto_name_local_only=false"])
def test_auto_name_set_rejected_as_unknown_key(pair):
    """The removed keys are no longer writable: `settings --set auto_name=...`
    errors as an unknown setting instead of silently persisting dead config."""
    runner = CliRunner()
    result = runner.invoke(cli, ["settings", "--set", pair], catch_exceptions=False)
    assert result.exit_code != 0, result.output
    assert "Unknown setting" in result.output


def _invoke_set(pair: str) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["settings", "--set", pair], catch_exceptions=False)
    assert result.exit_code == 0, result.output


def test_content_index_enabled_defaults_false_in_json():
    """The OCR-indexing flag (SCR-174 U1) is exposed in the read payload and
    defaults to off — the search consent trigger reads it to decide whether
    on-screen-text indexing is active."""
    settings = _invoke_settings_json()["settings"]
    assert settings["content_index_enabled"] is False


def test_content_index_enabled_set_roundtrips():
    """`settings --set content_index_enabled=...` is writable through the
    _BOOL_KEYS allowlist and the new value is reflected in `settings --json`.
    This is the write/read surface the consent flow flips on consent."""
    _invoke_set("content_index_enabled=true")
    assert _invoke_settings_json()["settings"]["content_index_enabled"] is True

    _invoke_set("content_index_enabled=false")
    assert _invoke_settings_json()["settings"]["content_index_enabled"] is False


def test_content_index_enabled_rejects_non_bool():
    """A non-boolean value is rejected, matching the other _BOOL_KEYS."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["settings", "--set", "content_index_enabled=maybe"], catch_exceptions=False
    )
    assert result.exit_code == 1
    assert "true or false" in result.output


def test_content_index_consent_declined_defaults_false_in_json():
    """The one-time-consent decision (SCR-174 U7) is exposed and defaults off.
    Persisted separately from `content_index_enabled` so 'declined' never
    re-prompts and isn't conflated with 'feature off'."""
    settings = _invoke_settings_json()["settings"]
    assert settings["content_index_consent_declined"] is False


def test_content_index_consent_declined_set_roundtrips():
    """The Search consent flow persists a decline via this writable bool."""
    _invoke_set("content_index_consent_declined=true")
    assert _invoke_settings_json()["settings"]["content_index_consent_declined"] is True


def test_container_enabled_defaults_true_in_json():
    """The on-disk vault flag (SCR-258) is exposed in the read payload and
    **defaults on** — the product ships with no installed base, so the container
    is the shipped posture from the first recording (KTD-8/KTD-19)."""
    settings = _invoke_settings_json()["settings"]
    assert settings["container_enabled"] is True


def test_container_enabled_set_roundtrips():
    """`settings --set container_enabled=true|false` is writable through the
    _BOOL_KEYS allowlist and reflected in `settings --json`. NOTE: this only
    round-trips the config *flag* — on a bundle-present install the daemon still
    refuses a plaintext downgrade (see the settings docstring / SECURITY.md);
    here there is no bundle, so the flag simply toggles."""
    _invoke_set("container_enabled=true")
    assert _invoke_settings_json()["settings"]["container_enabled"] is True

    _invoke_set("container_enabled=false")
    assert _invoke_settings_json()["settings"]["container_enabled"] is False


def test_container_enabled_rejects_non_bool():
    """A non-boolean value is rejected, matching the other _BOOL_KEYS."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["settings", "--set", "container_enabled=maybe"], catch_exceptions=False
    )
    assert result.exit_code == 1
    assert "true or false" in result.output
