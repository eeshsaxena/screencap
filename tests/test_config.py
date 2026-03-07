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
