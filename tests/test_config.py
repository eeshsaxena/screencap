"""Tests for screencap.config."""

import os
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset config cache between tests."""
    import screencap.config as cfg

    cfg._config_cache = None
    yield
    cfg._config_cache = None


def test_default_recordings_dir(tmp_path):
    with mock.patch("screencap.config._DEFAULT_RECORDINGS", tmp_path / "recordings"):
        from screencap.config import get_recordings_dir

        # Clear env
        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_RECORDINGS_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = get_recordings_dir()
            assert result == tmp_path / "recordings"
            assert result.exists()


def test_env_var_override(tmp_path):
    custom = tmp_path / "custom_recs"
    with mock.patch.dict(os.environ, {"SCREENCAP_RECORDINGS_DIR": str(custom)}):
        from screencap.config import get_recordings_dir

        result = get_recordings_dir()
        assert result == custom
        assert result.exists()


def test_audio_default_true():
    from screencap.config import get_audio_default

    env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_AUDIO_DEFAULT"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert get_audio_default() is True


def test_audio_env_override():
    from screencap.config import get_audio_default

    with mock.patch.dict(os.environ, {"SCREENCAP_AUDIO_DEFAULT": "false"}):
        assert get_audio_default() is False

    with mock.patch.dict(os.environ, {"SCREENCAP_AUDIO_DEFAULT": "1"}):
        assert get_audio_default() is True


def test_missing_config_file():
    """Config gracefully handles missing config.toml."""
    import screencap.config as cfg

    with mock.patch.object(cfg, "_CONFIG_PATH", Path("/nonexistent/config.toml")):
        cfg._config_cache = None
        result = cfg._load_toml()
        assert result == {}


def test_set_audio_default_round_trip(tmp_path):
    """``set_audio_default`` → ``get_audio_default`` round-trips via tomlkit."""
    import screencap.config as cfg
    from screencap.config import get_audio_default, set_audio_default

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("# header comment\naudio_default = true\n")

    env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_AUDIO_DEFAULT"}
    with (
        mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
        mock.patch.dict(os.environ, env, clear=True),
    ):
        cfg._config_cache = None
        assert get_audio_default() is True

        set_audio_default(False)
        # set_audio_default should invalidate the cache, so a fresh
        # get_audio_default() within the same process observes the write.
        assert get_audio_default() is False

        # tomlkit round-trip must preserve the leading comment.
        text = cfg_path.read_text()
        assert "# header comment" in text
        assert "audio_default = false" in text

        # Flip back to true to exercise the other direction.
        set_audio_default(True)
        assert get_audio_default() is True


def test_set_audio_default_creates_file(tmp_path):
    """``set_audio_default`` works even when config.toml does not exist yet."""
    import screencap.config as cfg
    from screencap.config import get_audio_default, set_audio_default

    cfg_path = tmp_path / "config.toml"
    assert not cfg_path.exists()

    env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_AUDIO_DEFAULT"}
    with (
        mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
        mock.patch.dict(os.environ, env, clear=True),
    ):
        cfg._config_cache = None
        set_audio_default(False)
        assert cfg_path.exists()
        assert get_audio_default() is False


_PREFER_BUILTIN_ENV = "SCREENCAP_PREFER_BUILTIN_MIC_OVER_BLUETOOTH"


def test_prefer_builtin_mic_default_true(tmp_path):
    """SCR-288: the Bluetooth mic-redirect defaults on (protect playback).

    Isolated from the real ``~/.screencap/config.toml`` (nonexistent tmp path +
    cache reset) so the default is asserted against an empty config, not whatever
    the developer's machine happens to set.
    """
    import screencap.config as cfg
    from screencap.config import get_prefer_builtin_mic_over_bluetooth

    env = {k: v for k, v in os.environ.items() if k != _PREFER_BUILTIN_ENV}
    with (
        mock.patch.object(cfg, "_CONFIG_PATH", tmp_path / "config.toml"),
        mock.patch.dict(os.environ, env, clear=True),
    ):
        cfg._config_cache = None
        assert get_prefer_builtin_mic_over_bluetooth() is True


def test_prefer_builtin_mic_env_override():
    from screencap.config import get_prefer_builtin_mic_over_bluetooth

    with mock.patch.dict(os.environ, {_PREFER_BUILTIN_ENV: "false"}):
        assert get_prefer_builtin_mic_over_bluetooth() is False
    with mock.patch.dict(os.environ, {_PREFER_BUILTIN_ENV: "1"}):
        assert get_prefer_builtin_mic_over_bluetooth() is True


def test_prefer_builtin_mic_toml_round_trip(tmp_path):
    """R11: a False persisted to config.toml disables the redirect (escape hatch)."""
    import screencap.config as cfg
    from screencap.config import (
        get_prefer_builtin_mic_over_bluetooth,
        set_prefer_builtin_mic_over_bluetooth,
    )

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("# header comment\n")

    env = {k: v for k, v in os.environ.items() if k != _PREFER_BUILTIN_ENV}
    with (
        mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
        mock.patch.dict(os.environ, env, clear=True),
    ):
        cfg._config_cache = None
        assert get_prefer_builtin_mic_over_bluetooth() is True  # default

        set_prefer_builtin_mic_over_bluetooth(False)
        # Cache invalidated → the fresh read observes the write.
        assert get_prefer_builtin_mic_over_bluetooth() is False
        text = cfg_path.read_text()
        assert "# header comment" in text
        assert "prefer_builtin_mic_over_bluetooth = false" in text


# --- recordings_dir setter tests (SCR-228 / U1) ---


class TestSetRecordingsDir:
    """Tests for set_recordings_dir() — the SCR-228 config write."""

    @staticmethod
    def _env_without_override():
        return {k: v for k, v in os.environ.items() if k != "SCREENCAP_RECORDINGS_DIR"}

    def test_round_trip(self, tmp_path):
        """set_recordings_dir → get_recordings_dir round-trips; cache invalidated."""
        import screencap.config as cfg
        from screencap.config import get_recordings_dir, set_recordings_dir

        base = tmp_path / "base"
        base.mkdir()
        cfg_path = base / "config.toml"
        cfg_path.write_text("# header comment\naudio_default = true\n")
        target = tmp_path / "new_recordings"
        target.mkdir()

        with (
            mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
            mock.patch.object(cfg, "_DEFAULT_BASE", base),
            mock.patch.dict(os.environ, self._env_without_override(), clear=True),
        ):
            cfg._config_cache = None
            set_recordings_dir(target)
            # Cache invalidated on writer exit → fresh read sees the new path.
            assert get_recordings_dir() == target.resolve()

    def test_preserves_other_keys_and_comments(self, tmp_path):
        import screencap.config as cfg
        from screencap.config import set_recordings_dir

        base = tmp_path / "base"
        base.mkdir()
        cfg_path = base / "config.toml"
        cfg_path.write_text("# header comment\naudio_default = true\n")
        target = tmp_path / "recs2"
        target.mkdir()

        with (
            mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
            mock.patch.object(cfg, "_DEFAULT_BASE", base),
            mock.patch.dict(os.environ, self._env_without_override(), clear=True),
        ):
            cfg._config_cache = None
            set_recordings_dir(target)
            text = cfg_path.read_text()
            assert "# header comment" in text
            assert "audio_default = true" in text
            assert str(target.resolve()) in text

    def test_creates_file_when_absent(self, tmp_path):
        import screencap.config as cfg
        from screencap.config import get_recordings_dir, set_recordings_dir

        base = tmp_path / "base"
        base.mkdir()
        cfg_path = base / "config.toml"
        assert not cfg_path.exists()
        target = tmp_path / "recs3"
        target.mkdir()

        with (
            mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
            mock.patch.object(cfg, "_DEFAULT_BASE", base),
            mock.patch.dict(os.environ, self._env_without_override(), clear=True),
        ):
            cfg._config_cache = None
            set_recordings_dir(target)
            assert cfg_path.exists()
            assert get_recordings_dir() == target.resolve()


# --- disk threshold config tests ---


class TestDiskWarnMb:
    """Tests for get_disk_warn_mb()."""

    def test_default_value(self):
        from screencap.config import get_disk_warn_mb

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_DISK_WARN_MB"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert get_disk_warn_mb() == 2000

    def test_env_var_override(self):
        from screencap.config import get_disk_warn_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_WARN_MB": "1000"}):
            assert get_disk_warn_mb() == 1000

    def test_env_var_zero_disables(self):
        from screencap.config import get_disk_warn_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_WARN_MB": "0"}):
            assert get_disk_warn_mb() == 0

    def test_env_var_whitespace(self):
        from screencap.config import get_disk_warn_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_WARN_MB": "  500  "}):
            assert get_disk_warn_mb() == 500

    def test_env_var_non_numeric(self):
        from screencap.config import get_disk_warn_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_WARN_MB": "abc"}):
            with pytest.raises(SystemExit):
                get_disk_warn_mb()

    def test_env_var_negative(self):
        from screencap.config import get_disk_warn_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_WARN_MB": "-1"}):
            with pytest.raises(SystemExit):
                get_disk_warn_mb()

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_disk_warn_mb

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_DISK_WARN_MB"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"disk_warn_mb": 3000}
            assert get_disk_warn_mb() == 3000

    def test_toml_rejects_float(self):
        import screencap.config as cfg
        from screencap.config import get_disk_warn_mb

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_DISK_WARN_MB"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"disk_warn_mb": 500.0}
            with pytest.raises(SystemExit):
                get_disk_warn_mb()

    def test_toml_rejects_string(self):
        import screencap.config as cfg
        from screencap.config import get_disk_warn_mb

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_DISK_WARN_MB"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"disk_warn_mb": "500"}
            with pytest.raises(SystemExit):
                get_disk_warn_mb()


class TestDiskStopMb:
    """Tests for get_disk_stop_mb()."""

    def test_default_value(self):
        from screencap.config import get_disk_stop_mb

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_DISK_STOP_MB"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert get_disk_stop_mb() == 500

    def test_env_var_override(self):
        from screencap.config import get_disk_stop_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_STOP_MB": "200"}):
            assert get_disk_stop_mb() == 200

    def test_env_var_zero_disables(self):
        from screencap.config import get_disk_stop_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_STOP_MB": "0"}):
            assert get_disk_stop_mb() == 0

    def test_env_var_non_numeric(self):
        from screencap.config import get_disk_stop_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_STOP_MB": "xyz"}):
            with pytest.raises(SystemExit):
                get_disk_stop_mb()

    def test_env_var_negative(self):
        from screencap.config import get_disk_stop_mb

        with mock.patch.dict(os.environ, {"SCREENCAP_DISK_STOP_MB": "-100"}):
            with pytest.raises(SystemExit):
                get_disk_stop_mb()


# --- show_on_website config tests ---


class TestShowOnWebsite:
    """Tests for get_show_on_website()."""

    def test_default_value(self):
        import screencap.config as cfg
        from screencap.config import get_show_on_website

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_SHOW_ON_WEBSITE"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {}  # empty config — no show_on_website key
            assert get_show_on_website() is True

    def test_env_var_true(self):
        from screencap.config import get_show_on_website

        with mock.patch.dict(os.environ, {"SCREENCAP_SHOW_ON_WEBSITE": "1"}):
            assert get_show_on_website() is True

    def test_env_var_false(self):
        from screencap.config import get_show_on_website

        with mock.patch.dict(os.environ, {"SCREENCAP_SHOW_ON_WEBSITE": "false"}):
            assert get_show_on_website() is False

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_show_on_website

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_SHOW_ON_WEBSITE"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"show_on_website": False}
            assert get_show_on_website() is False

    def test_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_show_on_website

        cfg._config_cache = {"show_on_website": False}
        with mock.patch.dict(os.environ, {"SCREENCAP_SHOW_ON_WEBSITE": "yes"}):
            assert get_show_on_website() is True

    def test_env_var_no(self):
        from screencap.config import get_show_on_website

        with mock.patch.dict(os.environ, {"SCREENCAP_SHOW_ON_WEBSITE": "no"}):
            assert get_show_on_website() is False


# --- sentinel show_on_website tests ---


class TestSentinelShowOnWebsite:
    """Tests for show_on_website in _build_sentinel_data()."""

    def test_default_true(self):
        from screencap.chunk_processor import _build_sentinel_data

        data = _build_sentinel_data("rec", "graceful", 1)
        assert data["show_on_website"] is True

    def test_explicit_false(self):
        from screencap.chunk_processor import _build_sentinel_data

        data = _build_sentinel_data("rec", "graceful", 1, show_on_website=False)
        assert data["show_on_website"] is False


# --- stripe paywall config tests ---


class TestStripePaywall:
    """Tests for get_stripe_paywall_enabled() (default OFF)."""

    def test_default_value(self):
        import screencap.config as cfg
        from screencap.config import get_stripe_paywall_enabled

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_STRIPE_PAYWALL"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {}  # empty config — no stripe_paywall key
            assert get_stripe_paywall_enabled() is False

    def test_env_var_true(self):
        from screencap.config import get_stripe_paywall_enabled

        with mock.patch.dict(os.environ, {"SCREENCAP_STRIPE_PAYWALL": "1"}):
            assert get_stripe_paywall_enabled() is True

    def test_env_var_false(self):
        from screencap.config import get_stripe_paywall_enabled

        with mock.patch.dict(os.environ, {"SCREENCAP_STRIPE_PAYWALL": "false"}):
            assert get_stripe_paywall_enabled() is False

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_stripe_paywall_enabled

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_STRIPE_PAYWALL"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"stripe_paywall": True}
            assert get_stripe_paywall_enabled() is True

    def test_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_stripe_paywall_enabled

        cfg._config_cache = {"stripe_paywall": True}
        with mock.patch.dict(os.environ, {"SCREENCAP_STRIPE_PAYWALL": "no"}):
            assert get_stripe_paywall_enabled() is False


class TestLocalPaywallEnforce:
    """Tests for get_local_paywall_enforced() (default OFF)."""

    def test_default_value(self):
        import screencap.config as cfg
        from screencap.config import get_local_paywall_enforced

        env = {
            k: v
            for k, v in os.environ.items()
            if k != "SCREENCAP_LOCAL_PAYWALL_ENFORCE"
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {}  # empty config — no local_paywall_enforce key
            assert get_local_paywall_enforced() is False

    def test_env_var_true(self):
        from screencap.config import get_local_paywall_enforced

        with mock.patch.dict(os.environ, {"SCREENCAP_LOCAL_PAYWALL_ENFORCE": "1"}):
            assert get_local_paywall_enforced() is True

    def test_env_var_false(self):
        from screencap.config import get_local_paywall_enforced

        with mock.patch.dict(os.environ, {"SCREENCAP_LOCAL_PAYWALL_ENFORCE": "false"}):
            assert get_local_paywall_enforced() is False

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_local_paywall_enforced

        env = {
            k: v
            for k, v in os.environ.items()
            if k != "SCREENCAP_LOCAL_PAYWALL_ENFORCE"
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"local_paywall_enforce": True}
            assert get_local_paywall_enforced() is True

    def test_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_local_paywall_enforced

        cfg._config_cache = {"local_paywall_enforce": True}
        with mock.patch.dict(os.environ, {"SCREENCAP_LOCAL_PAYWALL_ENFORCE": "no"}):
            assert get_local_paywall_enforced() is False


# --- upload default config tests ---


class TestUploadDefault:
    """Tests for get_upload_default()."""

    def test_default_value(self):
        from screencap.config import get_upload_default

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_UPLOAD_DEFAULT"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert get_upload_default() == "ask"

    @pytest.mark.parametrize("value,expected", [("cloud", "cloud"), ("local", "local"), ("ask", "ask")])
    def test_env_var_valid_values(self, value, expected):
        from screencap.config import get_upload_default

        with mock.patch.dict(os.environ, {"SCREENCAP_UPLOAD_DEFAULT": value}):
            assert get_upload_default() == expected

    def test_env_var_invalid(self):
        from screencap.config import get_upload_default

        with mock.patch.dict(os.environ, {"SCREENCAP_UPLOAD_DEFAULT": "bogus"}):
            with pytest.raises(SystemExit):
                get_upload_default()

    def test_env_var_case_insensitive(self):
        from screencap.config import get_upload_default

        with mock.patch.dict(os.environ, {"SCREENCAP_UPLOAD_DEFAULT": "CLOUD"}):
            assert get_upload_default() == "cloud"

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_upload_default

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_UPLOAD_DEFAULT"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"privacy": {"upload_default": "local"}}
            assert get_upload_default() == "local"

    def test_toml_invalid_value(self):
        import screencap.config as cfg
        from screencap.config import get_upload_default

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_UPLOAD_DEFAULT"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"privacy": {"upload_default": "bogus"}}
            with pytest.raises(SystemExit):
                get_upload_default()

    def test_toml_non_string(self):
        import screencap.config as cfg
        from screencap.config import get_upload_default

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_UPLOAD_DEFAULT"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"privacy": {"upload_default": 42}}
            with pytest.raises(SystemExit):
                get_upload_default()

    def test_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_upload_default

        cfg._config_cache = {"privacy": {"upload_default": "local"}}
        with mock.patch.dict(os.environ, {"SCREENCAP_UPLOAD_DEFAULT": "cloud"}):
            assert get_upload_default() == "cloud"


# --- segmentation mode config tests ---


class TestSegmentationMode:
    """Tests for get_segmentation_mode()."""

    def test_default_value(self):
        from screencap.config import get_segmentation_mode

        assert get_segmentation_mode() == "llm"

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_segmentation_mode

        cfg._config_cache = {"segmentation_mode": "idle"}
        assert get_segmentation_mode() == "idle"

    def test_toml_invalid(self):
        import screencap.config as cfg
        from screencap.config import get_segmentation_mode

        cfg._config_cache = {"segmentation_mode": "bogus"}
        with pytest.raises(SystemExit):
            get_segmentation_mode()


# --- retention policy config tests (U3) ---


def _clear_retention_env():
    drop = {
        "SCREENCAP_RETENTION_POLICY",
        "SCREENCAP_RETENTION_DAYS",
        "SCREENCAP_RETENTION_SIZE_CAP_MB",
        "SCREENCAP_AUTO_DELETE",
    }
    return {k: v for k, v in os.environ.items() if k not in drop}


class TestRetentionPolicy:
    """Tests for get_retention_policy() and the legacy auto_delete shim."""

    def test_default_is_keep_forever(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {}
            assert get_retention_policy() == ("keep_forever", {})

    def test_toml_retention_block(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"retention": {"policy": "delete_after_days", "days": 30}}
            assert get_retention_policy() == ("delete_after_days", {"days": 30})

    def test_toml_size_cap(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"retention": {"policy": "size_cap", "size_cap_mb": 4096}}
            assert get_retention_policy() == ("size_cap", {"size_cap_mb": 4096})

    def test_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        cfg._config_cache = {"retention": {"policy": "keep_forever"}}
        with mock.patch.dict(os.environ, {"SCREENCAP_RETENTION_POLICY": "delete_after_upload"}):
            assert get_retention_policy() == ("delete_after_upload", {})

    def test_auto_delete_shim_defaults_false(self):
        """The deprecated bool shim returns False with no config (keep_forever default)."""
        import screencap.config as cfg
        from screencap.config import get_auto_delete_after_upload

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {}
            assert get_auto_delete_after_upload() is False

    def test_auto_delete_shim_true_on_legacy_flag(self):
        """Legacy top-level auto_delete_after_upload=true still maps to True."""
        import screencap.config as cfg
        from screencap.config import get_auto_delete_after_upload

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"auto_delete_after_upload": True}
            assert get_auto_delete_after_upload() is True

    def test_env_invalid_policy(self):
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, {"SCREENCAP_RETENTION_POLICY": "bogus"}):
            with pytest.raises(SystemExit):
                get_retention_policy()

    def test_toml_invalid_policy(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"retention": {"policy": "bogus"}}
            with pytest.raises(SystemExit):
                get_retention_policy()

    def test_days_must_be_positive_int(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"retention": {"policy": "delete_after_days", "days": 0}}
            with pytest.raises(SystemExit):
                get_retention_policy()

    # --- backward compatibility with the legacy auto_delete_after_upload bool ---

    def test_legacy_auto_delete_true_maps_to_delete_after_upload(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"auto_delete_after_upload": True}
            assert get_retention_policy() == ("delete_after_upload", {})

    def test_legacy_auto_delete_false_maps_to_keep_forever(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"auto_delete_after_upload": False}
            assert get_retention_policy() == ("keep_forever", {})

    def test_legacy_auto_delete_absent_maps_to_keep_forever(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {}
            assert get_retention_policy() == ("keep_forever", {})

    def test_retention_block_wins_over_legacy_bool(self):
        import screencap.config as cfg
        from screencap.config import get_retention_policy

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {
                "auto_delete_after_upload": True,
                "retention": {"policy": "keep_forever"},
            }
            assert get_retention_policy() == ("keep_forever", {})


class TestGetAutoDeleteAfterUploadShim:
    """The legacy bool shim must keep existing callers working."""

    def test_legacy_true_reads_true(self):
        import screencap.config as cfg
        from screencap.config import get_auto_delete_after_upload

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"auto_delete_after_upload": True}
            assert get_auto_delete_after_upload() is True

    def test_default_false(self):
        import screencap.config as cfg
        from screencap.config import get_auto_delete_after_upload

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {}
            assert get_auto_delete_after_upload() is False

    def test_new_retention_block_drives_shim(self):
        import screencap.config as cfg
        from screencap.config import get_auto_delete_after_upload

        with mock.patch.dict(os.environ, _clear_retention_env(), clear=True):
            cfg._config_cache = {"retention": {"policy": "delete_after_upload"}}
            assert get_auto_delete_after_upload() is True
            cfg._config_cache = {"retention": {"policy": "keep_forever"}}
            assert get_auto_delete_after_upload() is False

    def test_env_auto_delete_true(self):
        import screencap.config as cfg
        from screencap.config import get_auto_delete_after_upload

        cfg._config_cache = {}
        with mock.patch.dict(os.environ, {"SCREENCAP_AUTO_DELETE": "1"}):
            assert get_auto_delete_after_upload() is True


# --- masked-video-upload feature flag tests (Part B) ---


class TestMaskedVideoUploadFlag:
    """Tests for get_masked_video_upload_enabled() — default OFF carrier."""

    def test_default_false(self):
        import screencap.config as cfg
        from screencap.config import get_masked_video_upload_enabled

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_MASKED_VIDEO_UPLOAD"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {}
            assert get_masked_video_upload_enabled() is False

    def test_env_var_true(self):
        from screencap.config import get_masked_video_upload_enabled

        with mock.patch.dict(os.environ, {"SCREENCAP_MASKED_VIDEO_UPLOAD": "1"}):
            assert get_masked_video_upload_enabled() is True
        with mock.patch.dict(os.environ, {"SCREENCAP_MASKED_VIDEO_UPLOAD": "true"}):
            assert get_masked_video_upload_enabled() is True

    def test_env_var_false(self):
        from screencap.config import get_masked_video_upload_enabled

        with mock.patch.dict(os.environ, {"SCREENCAP_MASKED_VIDEO_UPLOAD": "false"}):
            assert get_masked_video_upload_enabled() is False

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_masked_video_upload_enabled

        env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_MASKED_VIDEO_UPLOAD"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"masked_video_upload": True}
            assert get_masked_video_upload_enabled() is True

    def test_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_masked_video_upload_enabled

        cfg._config_cache = {"masked_video_upload": False}
        with mock.patch.dict(os.environ, {"SCREENCAP_MASKED_VIDEO_UPLOAD": "yes"}):
            assert get_masked_video_upload_enabled() is True


# --- ambient (always-on) capture config tests (SCR-214 U1) ---


def _env_without_ambient():
    drop = {"SCREENCAP_AMBIENT_ENABLED", "SCREENCAP_AMBIENT_AUTOSTART"}
    return {k: v for k, v in os.environ.items() if k not in drop}


class TestAmbientEnabled:
    """Tests for get_ambient_enabled() — opt-in, default OFF (R1)."""

    def test_default_false(self):
        import screencap.config as cfg
        from screencap.config import get_ambient_enabled

        with mock.patch.dict(os.environ, _env_without_ambient(), clear=True):
            cfg._config_cache = {}
            assert get_ambient_enabled() is False

    def test_env_var_true(self):
        from screencap.config import get_ambient_enabled

        with mock.patch.dict(os.environ, {"SCREENCAP_AMBIENT_ENABLED": "1"}):
            assert get_ambient_enabled() is True
        with mock.patch.dict(os.environ, {"SCREENCAP_AMBIENT_ENABLED": "true"}):
            assert get_ambient_enabled() is True

    def test_env_var_false(self):
        from screencap.config import get_ambient_enabled

        with mock.patch.dict(os.environ, {"SCREENCAP_AMBIENT_ENABLED": "false"}):
            assert get_ambient_enabled() is False

    def test_toml_value(self):
        import screencap.config as cfg
        from screencap.config import get_ambient_enabled

        with mock.patch.dict(os.environ, _env_without_ambient(), clear=True):
            cfg._config_cache = {"ambient": {"enabled": True}}
            assert get_ambient_enabled() is True

    def test_env_beats_toml(self):
        import screencap.config as cfg
        from screencap.config import get_ambient_enabled

        # env override wins over the config.toml [ambient] block.
        cfg._config_cache = {"ambient": {"enabled": True}}
        with mock.patch.dict(os.environ, {"SCREENCAP_AMBIENT_ENABLED": "no"}):
            assert get_ambient_enabled() is False

        cfg._config_cache = {"ambient": {"enabled": False}}
        with mock.patch.dict(os.environ, {"SCREENCAP_AMBIENT_ENABLED": "yes"}):
            assert get_ambient_enabled() is True


class TestAmbientAutostart:
    """Tests for get_ambient_autostart() — default ON when ambient is enabled (R2)."""

    def test_default_true(self):
        import screencap.config as cfg
        from screencap.config import get_ambient_autostart

        with mock.patch.dict(os.environ, _env_without_ambient(), clear=True):
            cfg._config_cache = {}
            assert get_ambient_autostart() is True

    def test_toml_can_disable(self):
        import screencap.config as cfg
        from screencap.config import get_ambient_autostart

        with mock.patch.dict(os.environ, _env_without_ambient(), clear=True):
            cfg._config_cache = {"ambient": {"autostart": False}}
            assert get_ambient_autostart() is False

    def test_env_beats_toml(self):
        import screencap.config as cfg
        from screencap.config import get_ambient_autostart

        cfg._config_cache = {"ambient": {"autostart": True}}
        with mock.patch.dict(os.environ, {"SCREENCAP_AMBIENT_AUTOSTART": "0"}):
            assert get_ambient_autostart() is False

    def test_autostart_independent_of_enabled(self):
        """Autostart defaults ON even when enabled is off in the same block."""
        import screencap.config as cfg
        from screencap.config import get_ambient_autostart, get_ambient_enabled

        with mock.patch.dict(os.environ, _env_without_ambient(), clear=True):
            cfg._config_cache = {"ambient": {"enabled": False}}
            assert get_ambient_enabled() is False
            assert get_ambient_autostart() is True


class TestAmbientSetters:
    """Round-trip tests for set_ambient_enabled / set_ambient_autostart."""

    def test_set_enabled_round_trip(self, tmp_path):
        import screencap.config as cfg
        from screencap.config import get_ambient_enabled, set_ambient_enabled

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("# header comment\naudio_default = true\n")

        with (
            mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
            mock.patch.dict(os.environ, _env_without_ambient(), clear=True),
        ):
            cfg._config_cache = None
            assert get_ambient_enabled() is False  # default before any write

            set_ambient_enabled(True)
            # Cache invalidated on write → fresh read observes it.
            assert get_ambient_enabled() is True

            text = cfg_path.read_text()
            assert "# header comment" in text  # tomlkit preserved the comment
            assert "audio_default = true" in text  # unrelated key untouched
            assert "[ambient]" in text

            set_ambient_enabled(False)
            assert get_ambient_enabled() is False

    def test_set_enabled_creates_file(self, tmp_path):
        import screencap.config as cfg
        from screencap.config import get_ambient_enabled, set_ambient_enabled

        cfg_path = tmp_path / "config.toml"
        assert not cfg_path.exists()

        with (
            mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
            mock.patch.dict(os.environ, _env_without_ambient(), clear=True),
        ):
            cfg._config_cache = None
            set_ambient_enabled(True)
            assert cfg_path.exists()
            assert get_ambient_enabled() is True

    def test_set_autostart_round_trip(self, tmp_path):
        import screencap.config as cfg
        from screencap.config import get_ambient_autostart, set_ambient_autostart

        cfg_path = tmp_path / "config.toml"

        with (
            mock.patch.object(cfg, "_CONFIG_PATH", cfg_path),
            mock.patch.dict(os.environ, _env_without_ambient(), clear=True),
        ):
            cfg._config_cache = None
            assert get_ambient_autostart() is True  # default

            set_ambient_autostart(False)
            assert get_ambient_autostart() is False

            set_ambient_autostart(True)
            assert get_ambient_autostart() is True
