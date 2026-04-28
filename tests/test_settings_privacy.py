"""Tests for Unit 4b: ``screencap settings privacy <field> <op> <value>``.

Verifies the privacy-list mutation surface — add/remove for list fields,
set for scalar fields, idempotency, R16 round-trip preservation of mode,
and the matrix-EXCLUDE bypass guard.
"""

from __future__ import annotations

import tomllib
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


def _read_cfg() -> dict:
    from screencap.config import _CONFIG_PATH

    if not _CONFIG_PATH.exists():
        return {}
    return tomllib.loads(_CONFIG_PATH.read_text())


def _invoke(*args):
    runner = CliRunner()
    return runner.invoke(cli, ["settings", "privacy", *args], catch_exceptions=False)


# ---------------------------------------------------------------------------
# List fields
# ---------------------------------------------------------------------------


class TestListFields:
    def test_add_to_exclude_apps(self):
        result = _invoke("exclude_apps", "add", "com.example.foo")
        assert result.exit_code == 0
        cfg = _read_cfg()
        assert "com.example.foo" in cfg["privacy"]["exclude_apps"]

    def test_add_then_remove(self):
        _invoke("exclude_apps", "add", "com.example.foo")
        _invoke("exclude_apps", "remove", "com.example.foo")
        cfg = _read_cfg()
        assert cfg.get("privacy", {}).get("exclude_apps", []) == []

    def test_add_idempotent(self):
        _invoke("exclude_apps", "add", "com.example.foo")
        result = _invoke("exclude_apps", "add", "com.example.foo")
        assert result.exit_code == 0
        cfg = _read_cfg()
        assert cfg["privacy"]["exclude_apps"].count("com.example.foo") == 1

    def test_remove_absent_value_is_noop(self):
        result = _invoke("exclude_apps", "remove", "com.example.never-added")
        assert result.exit_code == 0

    def test_set_rejected_for_list_field(self):
        result = _invoke("exclude_apps", "set", "com.example.foo")
        assert result.exit_code != 0
        assert "use add/remove" in result.output

    def test_unknown_field_rejected(self):
        result = _invoke("not_a_field", "add", "x")
        assert result.exit_code != 0
        assert "Unknown privacy field" in result.output


class TestScalarFields:
    def test_set_mode(self):
        result = _invoke("mode", "set", "internal")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["mode"] == "internal"

    def test_set_mode_normalizes_case(self):
        _invoke("mode", "set", "INTERNAL")
        assert _read_cfg()["privacy"]["mode"] == "internal"

    def test_invalid_mode_rejected(self):
        result = _invoke("mode", "set", "paranoid")
        assert result.exit_code != 0
        assert "must be one of" in result.output

    def test_set_setup_skipped_true(self):
        result = _invoke("setup_skipped", "set", "true")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["setup_skipped"] is True

    def test_set_setup_skipped_false(self):
        result = _invoke("setup_skipped", "set", "false")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["setup_skipped"] is False

    def test_invalid_bool_rejected(self):
        result = _invoke("setup_skipped", "set", "maybe")
        assert result.exit_code != 0

    def test_add_rejected_for_scalar_field(self):
        result = _invoke("mode", "add", "internal")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Matrix EXCLUDE invariant guard
# ---------------------------------------------------------------------------


class TestMatrixExcludeGuard:
    def test_allow_apps_cannot_add_password_manager(self):
        """1Password is in PASSWORD_MANAGER which the matrix unconditionally
        excludes; allow_apps cannot loosen this."""
        result = _invoke("allow_apps", "add", "com.1password.1password")
        assert result.exit_code != 0
        assert "PASSWORD_MANAGER" in result.output or "matrix" in result.output

    def test_allow_apps_can_add_chat_app(self):
        """CHAT is masked (MASK_WINDOW under internal) but not unconditionally
        EXCLUDED — allow_apps add succeeds."""
        result = _invoke("allow_apps", "add", "com.tinyspeck.slackmacgap")
        assert result.exit_code == 0
        assert "com.tinyspeck.slackmacgap" in _read_cfg()["privacy"]["allow_apps"]

    def test_exclude_apps_can_add_password_manager(self):
        """exclude_apps can always add anything — strictening is safe."""
        result = _invoke("exclude_apps", "add", "com.1password.1password")
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# R16 round-trip: mode preservation across mutations
# ---------------------------------------------------------------------------


class TestRoundTripModePreservation:
    def test_mode_preserved_across_list_mutations(self):
        # Start with mode = public (non-default, must not be substituted)
        _invoke("mode", "set", "public")
        # 10 alternating add/remove on exclude_apps
        for i in range(10):
            _invoke("exclude_apps", "add", f"com.example.app{i}")
            _invoke("exclude_apps", "remove", f"com.example.app{i}")
        # Mode must still be public
        assert _read_cfg()["privacy"]["mode"] == "public"

    def test_other_keys_preserved_when_setting_mode(self):
        _invoke("exclude_apps", "add", "com.example.x")
        _invoke("allow_apps", "add", "com.example.y")
        _invoke("mode", "set", "internal")
        cfg = _read_cfg()["privacy"]
        assert cfg["mode"] == "internal"
        assert "com.example.x" in cfg["exclude_apps"]
        assert "com.example.y" in cfg["allow_apps"]
