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
