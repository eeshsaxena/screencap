"""Configuration: ~/.screencap/config.toml + env vars."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_DEFAULT_BASE = Path.home() / ".screencap"
_DEFAULT_RECORDINGS = _DEFAULT_BASE / "recordings"
_DEFAULT_DOWNLOADS = _DEFAULT_BASE / "downloads"
_CONFIG_PATH = _DEFAULT_BASE / "config.toml"

_config_cache: dict | None = None


def _load_toml() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if _CONFIG_PATH.exists():
        _config_cache = tomllib.loads(_CONFIG_PATH.read_text())
    else:
        _config_cache = {}
    return _config_cache


def get_recordings_dir() -> Path:
    """Return recordings directory, creating it if needed."""
    env = os.environ.get("SCREENCAP_RECORDINGS_DIR")
    if env:
        p = Path(env)
    else:
        cfg = _load_toml()
        p = Path(cfg.get("recordings_dir", str(_DEFAULT_RECORDINGS)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_audio_default() -> bool:
    """Return default audio setting (True = on)."""
    env = os.environ.get("SCREENCAP_AUDIO_DEFAULT")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    return cfg.get("audio_default", True)


def get_wifi_metrics() -> bool:
    """Return whether WiFi metrics collection is enabled (True = on)."""
    env = os.environ.get("SCREENCAP_WIFI_METRICS")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    return cfg.get("wifi_metrics", True)


def get_app_versions() -> bool:
    """Return whether running-application version capture is enabled (True = on)."""
    env = os.environ.get("SCREENCAP_APP_VERSIONS")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    return cfg.get("app_versions", True)


def get_auto_name() -> bool:
    """Return whether LLM auto-naming is enabled after recording (True = on)."""
    env = os.environ.get("SCREENCAP_AUTO_NAME")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    return cfg.get("auto_name", True)


def get_auto_update() -> bool:
    """Return whether auto-update checking is enabled (True = on)."""
    env = os.environ.get("SCREENCAP_AUTO_UPDATE")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    return cfg.get("auto_update", True)


def get_auto_name_local_only() -> bool:
    """Return whether LLM auto-naming is restricted to local providers only."""
    env = os.environ.get("SCREENCAP_AUTO_NAME_LOCAL_ONLY")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    return cfg.get("auto_name_local_only", False)


def get_downloads_dir() -> Path:
    """Return downloads directory, creating it if needed."""
    env = os.environ.get("SCREENCAP_DOWNLOADS_DIR")
    if env:
        p = Path(env)
    else:
        cfg = _load_toml()
        p = Path(cfg.get("downloads_dir", str(_DEFAULT_DOWNLOADS)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_base_dir() -> Path:
    """Return ~/.screencap/, creating it if needed."""
    _DEFAULT_BASE.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_BASE


def get_disk_warn_mb() -> int:
    """Minimum free MB to start recording / show warning. Default 2000."""
    env = os.environ.get("SCREENCAP_DISK_WARN_MB")
    if env is not None:
        env = env.strip()
        try:
            val = int(env)
        except ValueError:
            raise SystemExit(
                f"Error: SCREENCAP_DISK_WARN_MB must be an integer, got: {env!r}"
            )
        if val < 0:
            raise SystemExit(
                f"Error: SCREENCAP_DISK_WARN_MB cannot be negative, got: {val}"
            )
        return val
    cfg = _load_toml()
    val = cfg.get("disk_warn_mb", 2000)
    if not isinstance(val, int):
        raise SystemExit(
            f"Error: disk_warn_mb in config.toml must be an integer, got: {val!r}"
        )
    return val


def get_disk_stop_mb() -> int:
    """Free MB threshold to auto-stop recording. Default 500."""
    env = os.environ.get("SCREENCAP_DISK_STOP_MB")
    if env is not None:
        env = env.strip()
        try:
            val = int(env)
        except ValueError:
            raise SystemExit(
                f"Error: SCREENCAP_DISK_STOP_MB must be an integer, got: {env!r}"
            )
        if val < 0:
            raise SystemExit(
                f"Error: SCREENCAP_DISK_STOP_MB cannot be negative, got: {val}"
            )
        return val
    cfg = _load_toml()
    val = cfg.get("disk_stop_mb", 500)
    if not isinstance(val, int):
        raise SystemExit(
            f"Error: disk_stop_mb in config.toml must be an integer, got: {val!r}"
        )
    return val


def get_chunk_duration() -> float:
    """Return auto-cut chunk duration in seconds. Default 3600 (1 hour). 0 = legacy."""
    env = os.environ.get("SCREENCAP_CHUNK_DURATION")
    if env is not None:
        return float(env)
    cfg = _load_toml()
    return float(cfg.get("chunk_duration", 3600.0))


def get_auto_delete_after_upload() -> bool:
    """Return whether to auto-delete chunks after confirmed upload. Default True."""
    env = os.environ.get("SCREENCAP_AUTO_DELETE")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    return cfg.get("auto_delete_after_upload", True)


def get_rest_threshold() -> float:
    """Return rest threshold in seconds for task segmentation. Default 120."""
    env = os.environ.get("SCREENCAP_REST_THRESHOLD")
    if env is not None:
        return float(env)
    cfg = _load_toml()
    return float(cfg.get("rest_threshold", 120.0))


def resolve_recording_dir(name: str) -> Path:
    """Resolve a recording name to a directory path, with traversal protection.

    Raises ValueError if the resolved path escapes the recordings directory.
    """
    recordings_dir = get_recordings_dir()
    recording_dir = (recordings_dir / name).resolve()
    if not recording_dir.is_relative_to(recordings_dir.resolve()):
        raise ValueError(f"Invalid recording name: {name}")
    return recording_dir
