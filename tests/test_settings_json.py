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
        "auto_name",
        "chunk_duration",
        "auto_delete_after_upload",
        "rest_threshold_seconds",
        "recordings_dir",
    ):
        assert key in settings, f"missing {key} in settings payload"
