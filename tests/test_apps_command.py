"""Tests for Unit 5: ``screencap apps --json``.

Wraps app_discovery.discover_installed_apps() + classify + matrix evaluation.
Designed for the SwiftUI Privacy pane to render per-app toggles + state badges.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest import mock

import pytest
from click.testing import CliRunner

from screencap.app_discovery import AppMetadata
from screencap.cli import cli
from screencap.privacy.policy import ContextClass

pytestmark = pytest.mark.privacy


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    import screencap.config

    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    yield


def _fake_apps() -> list[AppMetadata]:
    """Return a fixed list of installed apps for stable tests."""
    return [
        AppMetadata(path="/Applications/1Password 7.app",
                    bundle_id="com.1password.1password",
                    display_name="1Password 7"),
        AppMetadata(path="/Applications/Slack.app",
                    bundle_id="com.tinyspeck.slackmacgap",
                    display_name="Slack"),
        AppMetadata(path="/Applications/ChatGPT.app",
                    bundle_id="com.openai.chat",
                    display_name="ChatGPT"),
        AppMetadata(path="/Applications/Visual Studio Code.app",
                    bundle_id="com.microsoft.VSCode",
                    display_name="Visual Studio Code"),
        AppMetadata(path="/Applications/Robinhood.app",
                    bundle_id="com.robinhood.Robinhood",
                    display_name="Robinhood"),
        AppMetadata(path="/Applications/Mail.app",
                    bundle_id="com.apple.mail",
                    display_name="Mail"),
    ]


def _invoke_apps_json(*extra_args):
    runner = CliRunner()
    with mock.patch(
        "screencap.app_discovery.discover_installed_apps",
        return_value=_fake_apps(),
    ):
        return runner.invoke(cli, ["apps", "--json", *extra_args], catch_exceptions=False)


def _by_bundle(payload: dict, bundle_id: str) -> dict:
    return next(a for a in payload["apps"] if a["bundle_id"] == bundle_id)


class TestAppsJsonOutput:
    def test_returns_apps_list(self):
        result = _invoke_apps_json()
        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip())
        assert "apps" in payload
        assert len(payload["apps"]) == 6

    def test_envelope_carries_ok_and_schema_version(self):
        """Uniform JSON envelope (todo 020) — `ok` + `schema_version` lead."""
        payload = json.loads(_invoke_apps_json().stdout.strip())
        assert payload["ok"] is True
        assert isinstance(payload["schema_version"], int)
        assert payload["schema_version"] >= 1

    def test_each_app_has_required_fields(self):
        payload = json.loads(_invoke_apps_json().stdout.strip())
        for app in payload["apps"]:
            assert set(app.keys()) >= {
                "bundle_id", "display_name", "path", "icon_path",
                "context_class", "classification_source", "resolved_action",
                "in_exclude_apps", "in_allow_apps", "is_matrix_exclude",
            }


class TestMatrixResolution:
    def test_password_manager_is_matrix_exclude(self):
        payload = json.loads(_invoke_apps_json().stdout.strip())
        op = _by_bundle(payload, "com.1password.1password")
        assert op["context_class"] == "password_manager"
        assert op["resolved_action"] == "exclude"
        assert op["is_matrix_exclude"] is True

    def test_chat_under_default_internal_is_mask_window(self):
        """Slack under default internal mode → MASK_WINDOW (post-Unit-7a)."""
        payload = json.loads(_invoke_apps_json().stdout.strip())
        slack = _by_bundle(payload, "com.tinyspeck.slackmacgap")
        assert slack["context_class"] == "chat"
        assert slack["resolved_action"] == "mask_window"
        assert slack["is_matrix_exclude"] is False

    def test_ai_assistant_resolves_to_allow_under_internal(self):
        """ChatGPT under default internal → BROWSER_UNVERIFIED → ALLOW."""
        payload = json.loads(_invoke_apps_json().stdout.strip())
        chatgpt = _by_bundle(payload, "com.openai.chat")
        assert chatgpt["context_class"] == "browser_unverified"
        assert chatgpt["resolved_action"] == "allow"

    def test_code_editor_resolves_to_allow_under_internal(self):
        payload = json.loads(_invoke_apps_json().stdout.strip())
        vscode = _by_bundle(payload, "com.microsoft.VSCode")
        assert vscode["context_class"] == "code_editor_terminal"
        assert vscode["resolved_action"] == "allow"

    def test_banking_under_internal_is_mask_window(self):
        payload = json.loads(_invoke_apps_json().stdout.strip())
        rh = _by_bundle(payload, "com.robinhood.Robinhood")
        assert rh["context_class"] == "banking"
        assert rh["resolved_action"] == "mask_window"


class TestUserOverridesReflected:
    def test_in_exclude_apps_flag(self, tmp_path):
        from screencap.config import _CONFIG_PATH
        _CONFIG_PATH.write_text(
            '[privacy]\nmode = "internal"\nexclude_apps = ["com.tinyspeck.slackmacgap"]\n'
        )
        import screencap.config
        screencap.config._config_cache = None
        payload = json.loads(_invoke_apps_json().stdout.strip())
        slack = _by_bundle(payload, "com.tinyspeck.slackmacgap")
        assert slack["in_exclude_apps"] is True
        assert slack["resolved_action"] == "exclude"

    def test_in_allow_apps_respects_matrix_floor(self, tmp_path):
        """allow_apps for CHAT under internal does NOT override the matrix
        MASK_WINDOW floor (Finding 3 — runtime evaluator was missing the
        same guard the CLI add-time path enforces). The Privacy pane shows
        the bundle as in_allow_apps=true but resolved_action=mask_window
        so the user sees the actual capture posture."""
        from screencap.config import _CONFIG_PATH
        _CONFIG_PATH.write_text(
            '[privacy]\nmode = "internal"\nallow_apps = ["com.tinyspeck.slackmacgap"]\n'
        )
        import screencap.config
        screencap.config._config_cache = None
        payload = json.loads(_invoke_apps_json().stdout.strip())
        slack = _by_bundle(payload, "com.tinyspeck.slackmacgap")
        assert slack["in_allow_apps"] is True
        # Floor wins: matrix decides, not allow_apps.
        assert slack["resolved_action"] == "mask_window"

    def test_password_manager_stays_excluded_even_when_allow_listed(self, tmp_path):
        """Matrix EXCLUDE (PASSWORD_MANAGER) cannot be loosened by allow_apps."""
        from screencap.config import _CONFIG_PATH
        _CONFIG_PATH.write_text(
            '[privacy]\nmode = "internal"\nallow_apps = ["com.1password.1password"]\n'
        )
        import screencap.config
        screencap.config._config_cache = None
        payload = json.loads(_invoke_apps_json().stdout.strip())
        op = _by_bundle(payload, "com.1password.1password")
        assert op["in_allow_apps"] is True
        assert op["resolved_action"] == "exclude"  # matrix wins
        assert op["is_matrix_exclude"] is True


class TestConfirmationFields:
    """SCR-235 schema v3: allow_confirmed + confirmation_required."""

    def _write_privacy(self, body: str) -> None:
        import screencap.config
        from screencap.config import _CONFIG_PATH
        _CONFIG_PATH.write_text(body)
        screencap.config._config_cache = None

    def test_schema_v3_fields_present(self):
        payload = json.loads(_invoke_apps_json().stdout.strip())
        assert payload["schema_version"] >= 3
        for app in payload["apps"]:
            assert "allow_confirmed" in app
            assert "confirmation_required" in app

    def test_confirmation_required_per_class(self):
        payload = json.loads(_invoke_apps_json().stdout.strip())
        assert _by_bundle(payload, "com.1password.1password")["confirmation_required"] is True
        assert _by_bundle(payload, "com.robinhood.Robinhood")["confirmation_required"] is True
        assert _by_bundle(payload, "com.tinyspeck.slackmacgap")["confirmation_required"] is False
        assert _by_bundle(payload, "com.openai.chat")["confirmation_required"] is False

    def test_confirmed_allow_resolves_to_allow(self):
        """An entry in both lists is authoritative: resolved_action=allow
        and allow_confirmed=true, even for a mask-class app under internal."""
        self._write_privacy(
            '[privacy]\nmode = "internal"\n'
            'allow_apps = ["com.tinyspeck.slackmacgap"]\n'
            'confirmed_allow_apps = ["com.tinyspeck.slackmacgap"]\n'
        )
        payload = json.loads(_invoke_apps_json().stdout.strip())
        slack = _by_bundle(payload, "com.tinyspeck.slackmacgap")
        assert slack["allow_confirmed"] is True
        assert slack["resolved_action"] == "allow"

    def test_legacy_allow_is_not_confirmed(self):
        self._write_privacy(
            '[privacy]\nmode = "internal"\nallow_apps = ["com.tinyspeck.slackmacgap"]\n'
        )
        payload = json.loads(_invoke_apps_json().stdout.strip())
        slack = _by_bundle(payload, "com.tinyspeck.slackmacgap")
        assert slack["in_allow_apps"] is True
        assert slack["allow_confirmed"] is False
        assert slack["resolved_action"] == "mask_window"

    def test_mixed_case_membership_matches(self):
        """Config sets are case-normalized (SCR-235): mixed-case entries and
        mixed-case OS bundle IDs still match."""
        self._write_privacy(
            '[privacy]\nmode = "internal"\nexclude_apps = ["COM.MICROSOFT.VSCODE"]\n'
        )
        payload = json.loads(_invoke_apps_json().stdout.strip())
        vscode = _by_bundle(payload, "com.microsoft.VSCode")
        assert vscode["in_exclude_apps"] is True
        assert vscode["resolved_action"] == "exclude"


class TestErrorPath:
    def test_discovery_error_exits_nonzero_and_emits_envelope(self):
        """Error path exits 1 (todo 019) so agents that check exit code first
        don't silently skip a discovery failure as if it were an empty result."""
        runner = CliRunner()
        with mock.patch(
            "screencap.app_discovery.discover_installed_apps",
            side_effect=FileNotFoundError("/Applications missing"),
        ):
            result = runner.invoke(cli, ["apps", "--json"], catch_exceptions=False)
        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip())
        assert payload["ok"] is False
        assert payload["apps"] == []
        assert payload["error"] == "/Applications missing"
        assert payload["schema_version"] >= 1


class TestHumanOutputMarkupSafety:
    def test_human_output_escapes_os_app_name_markup(self, monkeypatch):
        """SCR-169: the human-path ``apps`` listing interpolates the OS-derived
        ``display_name`` / ``bundle_id`` into a Rich-markup string. A name carrying
        an unbalanced ``[/]`` makes Rich raise ``MarkupError`` and crash the
        command; a balanced ``[x]`` run is silently stripped. The dynamic segments
        must be escaped so brackets render verbatim and never reach the parser.

        Force the human path — CliRunner stdout is non-TTY, which would otherwise
        auto-select the unaffected --json path."""
        from rich.errors import MarkupError

        import screencap.cli as cli_mod

        evil = [
            AppMetadata(path="/Applications/Evil.app",
                        bundle_id="com.evil[/]app",
                        display_name="Evil[/] App"),
        ]
        monkeypatch.setattr(cli_mod, "_should_default_to_json", lambda: False)
        with mock.patch(
            "screencap.app_discovery.discover_installed_apps",
            return_value=evil,
        ):
            result = CliRunner().invoke(cli, ["apps"], catch_exceptions=False)

        assert not isinstance(result.exception, MarkupError), result.exception
        assert result.exit_code == 0
        # Escaped, so the literal brackets survive instead of crashing/stripping.
        assert "Evil[/] App" in result.output
        assert "com.evil[/]app" in result.output


class TestSpotlightFlag:
    def test_default_skips_spotlight(self):
        runner = CliRunner()
        with mock.patch(
            "screencap.app_discovery.discover_installed_apps",
            return_value=_fake_apps(),
        ) as m:
            runner.invoke(cli, ["apps", "--json"], catch_exceptions=False)
            m.assert_called_once_with(use_spotlight=False)

    def test_include_spotlight_flag(self):
        runner = CliRunner()
        with mock.patch(
            "screencap.app_discovery.discover_installed_apps",
            return_value=_fake_apps(),
        ) as m:
            runner.invoke(cli, ["apps", "--json", "--include-spotlight"], catch_exceptions=False)
            m.assert_called_once_with(use_spotlight=True)
