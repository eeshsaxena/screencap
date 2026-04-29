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


def _invoke(*args, as_json=False):
    runner = CliRunner()
    full_args = ["settings", "privacy"]
    if as_json:
        full_args.append("--json")
    full_args.extend(args)
    return runner.invoke(cli, full_args, catch_exceptions=False)


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

    def test_allow_apps_cannot_add_chat_app_under_internal(self):
        """CHAT under internal is MASK_WINDOW — allow_apps cannot loosen
        Unit 7a's strengthening for conversation apps (todo 005)."""
        # default mode is internal in the test setup
        result = _invoke("allow_apps", "add", "com.tinyspeck.slackmacgap")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "chat" in out and "mask_window" in out

    def test_allow_apps_can_add_chat_app_under_public(self):
        """Under mode=public the matrix produces MASK_WINDOW for CHAT, so
        allow_apps still blocks (todo 005 gates EXCLUDE/MASK_WINDOW/TEXT_REDACT)."""
        _invoke("mode", "set", "public")
        result = _invoke("allow_apps", "add", "com.tinyspeck.slackmacgap")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "chat" in out

    def test_allow_apps_can_add_browser(self):
        """BROWSER_UNVERIFIED is ALLOW under internal — explicit allow OK."""
        result = _invoke("allow_apps", "add", "com.brave.Browser")
        assert result.exit_code == 0
        assert "com.brave.Browser" in _read_cfg()["privacy"]["allow_apps"]

    def test_allow_apps_cannot_add_banking_app_under_internal(self):
        """BANKING under internal is MASK_WINDOW — must be blocked."""
        result = _invoke("allow_apps", "add", "com.robinhood.release.Robinhood")
        # If Robinhood isn't in the bundle map this test is vacuous; pick a
        # known-mapped bundle. Use a representative banking bundle ID that's
        # in BUNDLE_ID_MAP.
        from screencap.privacy.context import BUNDLE_ID_MAP
        from screencap.privacy.policy import ContextClass
        banking_bundle = next(
            (bid for bid, cls in BUNDLE_ID_MAP.items() if cls == ContextClass.BANKING),
            None,
        )
        if banking_bundle is None:
            pytest.skip("No BANKING bundle in BUNDLE_ID_MAP")
        result = _invoke("allow_apps", "add", banking_bundle)
        assert result.exit_code != 0
        out = result.output.lower()
        assert "banking" in out or "mask_window" in out

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


# ---------------------------------------------------------------------------
# Map field — app_classes (todo 023)
# ---------------------------------------------------------------------------


class TestAppClassesMapField:
    def test_set_valid_context_class(self):
        # Input is case-insensitive but stored as the lowercase enum.value
        result = _invoke("app_classes", "set", "com.example.foo=BROWSER_UNVERIFIED")
        assert result.exit_code == 0
        cfg = _read_cfg()["privacy"]
        assert cfg["app_classes"]["com.example.foo"] == "browser_unverified"

    def test_add_valid_context_class(self):
        result = _invoke("app_classes", "add", "com.example.bar=CHAT")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["app_classes"]["com.example.bar"] == "chat"

    def test_add_lowercase_valid_context_class(self):
        result = _invoke("app_classes", "add", "com.example.lower=email")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["app_classes"]["com.example.lower"] == "email"

    def test_add_then_remove(self):
        _invoke("app_classes", "add", "com.example.gone=EMAIL")
        result = _invoke("app_classes", "remove", "com.example.gone")
        assert result.exit_code == 0
        assert "com.example.gone" not in _read_cfg()["privacy"].get("app_classes", {})

    def test_set_invalid_context_class_rejected(self):
        """A typo'd class string must be caught at write time, not on next
        start (todo 010). Otherwise PrivacyConfig.parse crashes the recorder."""
        result = _invoke("app_classes", "set", "com.example.bad=NOT_A_REAL_CLASS")
        assert result.exit_code != 0
        assert "Unknown context class" in result.output or "NOT_A_REAL_CLASS" in result.output

    def test_value_missing_equals_rejected(self):
        result = _invoke("app_classes", "set", "com.example.no_equals_here")
        assert result.exit_code != 0
        assert "BUNDLE_ID=CLASS" in result.output or "requires" in result.output


# ---------------------------------------------------------------------------
# Internal-flag isolation (todo 012)
# ---------------------------------------------------------------------------


class TestMatrixAckFlagNotPublic:
    def test_matrix_ack_flag_not_settable_via_cli(self):
        """The migration flag is internal — the public scalar list must not
        expose it to scripted callers."""
        result = _invoke("matrix_acknowledged_v2026_04", "set", "true")
        assert result.exit_code != 0
        assert "Unknown privacy field" in result.output

    def test_shared_mode_rejected(self):
        """`shared` is reserved for MASK_REGION; PrivacyConfig.parse rejects
        it. The CLI must not write an unenforceable mode (todo 011)."""
        result = _invoke("mode", "set", "shared")
        assert result.exit_code != 0
        assert "must be one of" in result.output


# ---------------------------------------------------------------------------
# --json output (todo 016)
# ---------------------------------------------------------------------------


def _last_json_line(text: str) -> dict:
    """Find the last line of `text` that parses as JSON. CliRunner combines
    stdout and stderr; the JSON success/error payload lives among prose."""
    import json as _json
    last = None
    for line in text.strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            last = _json.loads(line)
        except _json.JSONDecodeError:
            continue
    assert last is not None, f"no JSON line found in: {text!r}"
    return last


class TestJsonOutput:
    def test_success_emits_json(self):
        result = _invoke(
            "exclude_apps", "add", "com.example.json_test", as_json=True,
        )
        assert result.exit_code == 0
        payload = _last_json_line(result.output)
        assert payload == {
            "ok": True,
            "changed": True,
            "field": "exclude_apps",
            "op": "add",
            "value": "com.example.json_test",
        }

    def test_idempotent_noop_emits_changed_false(self):
        _invoke("exclude_apps", "add", "com.example.dup")
        result = _invoke(
            "exclude_apps", "add", "com.example.dup", as_json=True,
        )
        assert result.exit_code == 0
        payload = _last_json_line(result.output)
        assert payload["ok"] is True
        assert payload["changed"] is False

    def test_error_emits_ok_false_with_error_field(self):
        result = _invoke("no_such_field", "set", "value", as_json=True)
        assert result.exit_code != 0
        payload = _last_json_line(result.output)
        assert payload["ok"] is False
        assert "error" in payload
