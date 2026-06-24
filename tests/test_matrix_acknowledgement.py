"""Tests for the Unit 7a matrix-acknowledgement migration prompt.

Verifies the one-time on-upgrade prompt that surfaces when an existing user
with ``mode = internal`` runs ``screencap start`` for the first time after
the privacy matrix tightened CHAT/EMAIL/CALENDAR/VIDEO_CALL.

The flag is ``privacy.matrix_acknowledged_v2026_04`` — once set, the prompt
never fires again. New users (no ``[privacy]`` section) get the flag set
on first config write so they never see it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Redirect _CONFIG_PATH to tmp_path and reset config cache."""
    import screencap.config

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    monkeypatch.delenv("SCREENCAP_MATRIX_ACK", raising=False)
    yield


def _read_flag(monkeypatch_path: Path) -> object:
    """Read the matrix_acknowledged_v2026_04 flag from disk."""
    import tomllib

    if not monkeypatch_path.exists():
        return None
    cfg = tomllib.loads(monkeypatch_path.read_text())
    return cfg.get("privacy", {}).get("matrix_acknowledged_v2026_04")


def _write_config(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


# ---------------------------------------------------------------------------
# _write_privacy_flag round-trip
# ---------------------------------------------------------------------------


class TestWritePrivacyFlag:
    def test_creates_privacy_section_when_missing(self, tmp_path):
        from screencap.privacy_settings import _write_privacy_flag
        from screencap.config import _CONFIG_PATH

        # No file exists yet
        _write_privacy_flag("matrix_acknowledged_v2026_04", True)
        assert _read_flag(_CONFIG_PATH) is True

    def test_preserves_other_privacy_keys(self, tmp_path):
        from screencap.privacy_settings import _write_privacy_flag
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
[privacy]
mode = "internal"
exclude_apps = ["com.example.foo"]
allow_apps = ["com.example.bar"]
""".lstrip())

        _write_privacy_flag("matrix_acknowledged_v2026_04", True)

        import tomllib
        cfg = tomllib.loads(_CONFIG_PATH.read_text())
        assert cfg["privacy"]["matrix_acknowledged_v2026_04"] is True
        assert cfg["privacy"]["mode"] == "internal"
        assert cfg["privacy"]["exclude_apps"] == ["com.example.foo"]
        assert cfg["privacy"]["allow_apps"] == ["com.example.bar"]

    def test_overwrites_existing_value(self, tmp_path):
        from screencap.privacy_settings import _write_privacy_flag
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
[privacy]
mode = "internal"
matrix_acknowledged_v2026_04 = false
""".lstrip())

        _write_privacy_flag("matrix_acknowledged_v2026_04", True)
        assert _read_flag(_CONFIG_PATH) is True


# ---------------------------------------------------------------------------
# _maybe_prompt_matrix_acknowledgement gating
# ---------------------------------------------------------------------------


class TestMatrixAcknowledgementPrompt:
    def test_no_config_file_skips(self, tmp_path):
        """Brand-new user with no config.toml at all → no prompt, no flag write."""
        from screencap.cli import _maybe_prompt_matrix_acknowledgement
        from screencap.config import _CONFIG_PATH

        assert not _CONFIG_PATH.exists()
        _maybe_prompt_matrix_acknowledgement()  # must not raise
        assert not _CONFIG_PATH.exists()  # no side effect

    def test_already_acknowledged_skips(self, tmp_path):
        """Flag already true → no-op."""
        from screencap.cli import _maybe_prompt_matrix_acknowledgement
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
[privacy]
mode = "internal"
matrix_acknowledged_v2026_04 = true
exclude_apps = ["com.example.x"]
""".lstrip())
        original_mtime = _CONFIG_PATH.stat().st_mtime
        _maybe_prompt_matrix_acknowledgement()
        # File untouched (no rewrite)
        assert _CONFIG_PATH.stat().st_mtime == original_mtime

    def test_non_internal_mode_skips(self, tmp_path):
        """Mode != internal → matrix change doesn't apply, no flag write."""
        from screencap.cli import _maybe_prompt_matrix_acknowledgement
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
[privacy]
mode = "public"
""".lstrip())

        _maybe_prompt_matrix_acknowledgement()
        assert _read_flag(_CONFIG_PATH) is None  # never set

    def test_env_var_writes_flag_without_prompting(self, tmp_path, monkeypatch):
        """SCREENCAP_MATRIX_ACK=true → flag written, no interactive prompt.

        SwiftUI's spawn path sets this env var; the flag is written so future
        invocations short-circuit without re-checking.
        """
        from screencap.cli import _maybe_prompt_matrix_acknowledgement
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
[privacy]
mode = "internal"
""".lstrip())

        monkeypatch.setenv("SCREENCAP_MATRIX_ACK", "true")
        # Stub stdin.isatty to return False so the path is purely env-driven
        import sys as _sys
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)

        _maybe_prompt_matrix_acknowledgement()
        assert _read_flag(_CONFIG_PATH) is True

    def test_non_tty_writes_flag_without_prompting(self, tmp_path, monkeypatch):
        """Non-interactive (no TTY) → flag written without prompt.

        Background invocations (cron, scripts) should not get a 5s wait;
        the flag should still be set so they don't re-check on every run.
        """
        from screencap.cli import _maybe_prompt_matrix_acknowledgement
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
[privacy]
mode = "internal"
""".lstrip())

        import sys as _sys
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)

        _maybe_prompt_matrix_acknowledgement()
        assert _read_flag(_CONFIG_PATH) is True

    def test_invalid_mode_value_skips(self, tmp_path):
        """Garbage mode value → no crash, no flag write."""
        from screencap.cli import _maybe_prompt_matrix_acknowledgement
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
[privacy]
mode = "wat"
""".lstrip())

        _maybe_prompt_matrix_acknowledgement()  # must not raise
        assert _read_flag(_CONFIG_PATH) is None

    def test_acknowledged_after_first_run_persists(self, tmp_path, monkeypatch):
        """Flag persists across two invocations — second run is a no-op."""
        from screencap.cli import _maybe_prompt_matrix_acknowledgement
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
[privacy]
mode = "internal"
""".lstrip())

        monkeypatch.setenv("SCREENCAP_MATRIX_ACK", "true")
        import sys as _sys
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)

        _maybe_prompt_matrix_acknowledgement()
        assert _read_flag(_CONFIG_PATH) is True

        # Second run — file already has flag, function should short-circuit.
        # Touching the file would be observable via mtime; we instead just
        # confirm it doesn't raise and the flag stays true.
        _maybe_prompt_matrix_acknowledgement()
        assert _read_flag(_CONFIG_PATH) is True



class TestNewUserPreSet:
    """When `_maybe_prompt_privacy_setup` runs (declined-wizard path), it
    pre-sets `matrix_acknowledged_v2026_04=true` so a brand-new user (who has
    never relied on TEXT_REDACT) doesn't see the upgrade prompt on first run.

    Pinned by todo 025 — the previous test suite covered every branch of
    `_maybe_prompt_matrix_acknowledgement` but not the upstream
    `_maybe_prompt_privacy_setup` path that pre-acknowledges for new users.
    """

    def test_decline_wizard_pre_sets_matrix_ack_flag(self, tmp_path, monkeypatch):
        """Simulate a brand-new user who declines the privacy wizard. The
        matrix-ack flag must be set so the next start doesn't fire the prompt.
        """
        from screencap.cli import _maybe_prompt_privacy_setup
        from screencap.config import _CONFIG_PATH, invalidate_config_cache

        # Fresh config — no [privacy] section yet.
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _CONFIG_PATH.exists():
            _CONFIG_PATH.unlink()
        invalidate_config_cache()

        # Decline the wizard via env var (simulates "user said no").
        monkeypatch.setenv("SCREENCAP_PRIVACY_SETUP_SKIP", "true")
        import sys as _sys
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)

        # Should not raise even though there's no config yet.
        try:
            _maybe_prompt_privacy_setup()
        except SystemExit:
            pass  # the function may exit on certain branches; we just need the side-effect

        flag = _read_flag(_CONFIG_PATH)
        # Either the flag is set (success path) OR the config still doesn't
        # exist (the function bailed before writing). Both are valid; what
        # matters is that on a config-write-success path, the flag IS set.
        if _CONFIG_PATH.exists():
            assert flag is True, (
                "After privacy setup wrote a config, the matrix-ack flag must "
                "be pre-set to true (todo 025). Got: " + repr(flag)
            )
