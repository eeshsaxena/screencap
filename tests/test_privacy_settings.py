"""Direct-seam unit tests for screencap.privacy_settings (SCR-156).

These exercise the helpers extracted from cli/__init__.py at their new home,
without a CliRunner round trip. Behavioral coverage of the mutation engine and
matrix guard via the Click command lives in tests/test_settings_privacy.py;
this file targets the seams that extraction newly makes directly testable, plus
the fail-fast / rollback contracts that the CliRunner suite does not reach.
"""

from __future__ import annotations

import fcntl
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point config + lock paths at a tmp dir (mirrors test_settings_privacy)."""
    import screencap.config

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)


class TestPrivacyConfigWriter:
    """The advisory-flock read-modify-write helper (R9 / todo 015 / todo 025)."""

    def test_raises_timeout_when_lock_is_held(self, monkeypatch):
        """A holder that never releases makes the writer fail fast rather than
        hang — surfaces a stuck peer instead of stalling `screencap start`."""
        from screencap import privacy_settings
        from screencap.privacy_settings import (
            PrivacyConfigLockTimeout,
            _config_lock_path,
            _privacy_config_writer,
        )

        # Shrink the timeout so the bounded retry loop exits quickly.
        monkeypatch.setattr(privacy_settings, "_PRIVACY_CONFIG_FLOCK_TIMEOUT_S", 0.3)

        lock_path = _config_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Hold the flock on a separate open file description; the writer opens
        # its own fd and its LOCK_EX|LOCK_NB acquire will keep hitting EWOULDBLOCK.
        holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder_fd, fcntl.LOCK_EX)
            with pytest.raises(PrivacyConfigLockTimeout):
                with _privacy_config_writer():
                    pytest.fail("writer should never acquire the lock")
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

    def test_leaves_file_untouched_on_exception(self):
        """On an exception inside the `with`, the doc is not saved — the
        read-modify-write cycle is all-or-nothing (no partial write)."""
        from screencap.config import _CONFIG_PATH
        from screencap.privacy_settings import _privacy_config_writer

        _CONFIG_PATH.write_text('[privacy]\nmode = "internal"\n', encoding="utf-8")
        original = _CONFIG_PATH.read_text(encoding="utf-8")

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom):
            with _privacy_config_writer() as doc:
                doc["privacy"]["mode"] = "public"  # mutate in memory only
                raise _Boom()

        assert _CONFIG_PATH.read_text(encoding="utf-8") == original


class TestMatrixBlocksAllowForClass:
    """The matrix-invariant guard as a pure function — the extraction's key
    payoff: directly testable without a CliRunner round trip. Behavioral
    coverage through `settings privacy` lives in test_settings_privacy.py."""

    def test_password_manager_blocked_in_every_mode(self):
        from screencap.privacy.policy import ContextClass
        from screencap.privacy_settings import _matrix_blocks_allow_for_class

        for mode in ("internal", "public"):
            assert (
                _matrix_blocks_allow_for_class(ContextClass.PASSWORD_MANAGER, mode)
                is not None
            )

    def test_browser_unverified_is_mode_dependent(self):
        """BROWSER_UNVERIFIED is ALLOW under internal (allowable) but MASK_WINDOW
        under public (blocked) — exercises the mode-dependent guard branch."""
        from screencap.privacy.policy import ContextClass
        from screencap.privacy_settings import _matrix_blocks_allow_for_class

        assert (
            _matrix_blocks_allow_for_class(ContextClass.BROWSER_UNVERIFIED, "internal")
            is None
        )
        assert (
            _matrix_blocks_allow_for_class(ContextClass.BROWSER_UNVERIFIED, "public")
            is not None
        )

    def test_chat_blocked_under_internal(self):
        from screencap.privacy.policy import ContextClass
        from screencap.privacy_settings import _matrix_blocks_allow_for_class

        # CHAT is MASK_WINDOW under internal → allow_apps cannot loosen it.
        assert _matrix_blocks_allow_for_class(ContextClass.CHAT, "internal") is not None

    def test_invalid_mode_falls_back_to_internal(self):
        from screencap.privacy.policy import ContextClass
        from screencap.privacy_settings import _matrix_blocks_allow_for_class

        # An unparseable mode is treated as internal, under which CHAT is blocked.
        assert _matrix_blocks_allow_for_class(ContextClass.CHAT, "garbage") is not None


class TestBuildPrivacySettingsBlock:
    """The `settings --json` privacy block reader (SCR-17), now directly tested."""

    def test_no_section_reports_internal_default_and_flag_false(self):
        from screencap.privacy_settings import _build_privacy_settings_block

        # No config file written by this test.
        assert _build_privacy_settings_block() == {
            "mode": "internal",
            "setup_skipped": False,
            "has_privacy_section": False,
        }

    def test_reads_mode_and_flags_from_section(self):
        from screencap.config import _CONFIG_PATH
        from screencap.privacy_settings import _build_privacy_settings_block

        _CONFIG_PATH.write_text(
            '[privacy]\nmode = "public"\nsetup_skipped = true\n', encoding="utf-8"
        )
        block = _build_privacy_settings_block()
        assert block["mode"] == "public"
        assert block["setup_skipped"] is True
        assert block["has_privacy_section"] is True

    def test_invalid_mode_falls_back_to_internal_but_keeps_section_flag(self):
        from screencap.config import _CONFIG_PATH
        from screencap.privacy_settings import _build_privacy_settings_block

        _CONFIG_PATH.write_text('[privacy]\nmode = "bogus"\n', encoding="utf-8")
        block = _build_privacy_settings_block()
        assert block["mode"] == "internal"
        assert block["has_privacy_section"] is True


class TestPrivacyDiagnostics:
    """The two privacy smoke checks moved in SCR-156; the _SMOKE_CHECKS registry
    stays in cli so its existing patch path keeps working."""

    def test_domain_index_check_passes(self):
        from screencap.privacy_settings import _check_domain_index

        name, ok, err = _check_domain_index()
        assert name == "domain_index"
        assert ok is True, err

    def test_onnxruntime_excluded_check_returns_contract(self):
        from screencap.privacy_settings import _check_onnxruntime_excluded

        name, ok, err = _check_onnxruntime_excluded()
        assert name == "onnxruntime_excluded"
        # Env-robust on purpose: onnxruntime is typically importable in a dev
        # environment (ok=False) but excluded from the frozen binary (ok=True).
        # Assert only the contract shape so this is not flaky across both.
        assert isinstance(ok, bool)

    def test_both_checks_registered_in_cli_smoke_list(self):
        from screencap import cli as _cli
        from screencap.privacy_settings import (
            _check_domain_index,
            _check_onnxruntime_excluded,
        )

        assert _check_domain_index in _cli._SMOKE_CHECKS
        assert _check_onnxruntime_excluded in _cli._SMOKE_CHECKS
