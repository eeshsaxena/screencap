"""Configuration: ~/.screencap/config.toml + env vars."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

if TYPE_CHECKING:
    import tomlkit

_DEFAULT_BASE = Path.home() / ".screencap"
_DEFAULT_RECORDINGS = _DEFAULT_BASE / "recordings"
_DEFAULT_DOWNLOADS = _DEFAULT_BASE / "downloads"
_DEFAULT_SESSIONS = _DEFAULT_BASE / "sessions"
_CONFIG_PATH = _DEFAULT_BASE / "config.toml"

_config_cache: dict | None = None

_BOOL_TRUE = ("1", "true", "yes")


def _load_toml() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if _CONFIG_PATH.exists():
        _config_cache = tomllib.loads(_CONFIG_PATH.read_text())
    else:
        _config_cache = {}
    return _config_cache


def invalidate_config_cache() -> None:
    """Reset the config cache so the next read re-loads from disk."""
    global _config_cache
    _config_cache = None


def _parse_bool_env(env_name: str, cfg_key: str, default: bool) -> bool:
    """Env var (truthy → bool) > config.toml > default."""
    env = os.environ.get(env_name)
    if env is not None:
        return env.lower() in _BOOL_TRUE
    return _load_toml().get(cfg_key, default)


def _parse_nonneg_int_env(env_name: str, cfg_key: str, default: int) -> int:
    """Non-negative integer: env var > config.toml > default.

    Exits with a message if the env var is non-integer or negative, or if
    the config.toml value is not an int.
    """
    env = os.environ.get(env_name)
    if env is not None:
        env = env.strip()
        try:
            val = int(env)
        except ValueError:
            raise SystemExit(
                f"Error: {env_name} must be an integer, got: {env!r}"
            )
        if val < 0:
            raise SystemExit(
                f"Error: {env_name} cannot be negative, got: {val}"
            )
        return val
    val = _load_toml().get(cfg_key, default)
    if not isinstance(val, int):
        raise SystemExit(
            f"Error: {cfg_key} in config.toml must be an integer, got: {val!r}"
        )
    return val


def save_config_atomic(
    config_path: Path, doc: "tomlkit.TOMLDocument",
) -> None:
    """Write a tomlkit document atomically (tempfile + os.rename).

    Single source of truth for atomic config writes — used by both the
    setup wizard and the runtime privacy persistence layer. Preserves
    comments and formatting via tomlkit.
    """
    import tomlkit  # local import — keeps `screencap --help` fast

    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config_path.parent),
        suffix=".toml.tmp",
    )
    closed = False
    try:
        os.write(fd, tomlkit.dumps(doc).encode())
        os.close(fd)
        closed = True
        os.rename(tmp_path, str(config_path))
    except Exception:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


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
    return _parse_bool_env("SCREENCAP_AUDIO_DEFAULT", "audio_default", True)


def set_audio_default(value: bool) -> None:
    """Persist the default audio setting to ``config.toml``.

    Uses tomlkit via the setup-wizard loader/saver pair so comments and
    formatting are preserved. Mirrors the path taken by
    ``screencap settings --set audio_default=…`` (cli.py:1911).

    Invalidates the in-process config cache so subsequent reads in the
    same process observe the new value. Cross-process invalidation is
    not required — each ``screencap start`` is a fresh Python process
    with an empty cache.
    """
    from screencap.setup_wizard import _load_config_toml, _save_config_atomic

    doc = _load_config_toml(_CONFIG_PATH)
    doc["audio_default"] = value
    _save_config_atomic(_CONFIG_PATH, doc)
    invalidate_config_cache()


def get_wifi_metrics() -> bool:
    """Return whether WiFi metrics collection is enabled (True = on)."""
    return _parse_bool_env("SCREENCAP_WIFI_METRICS", "wifi_metrics", True)


def get_app_versions() -> bool:
    """Return whether running-application version capture is enabled (True = on)."""
    return _parse_bool_env("SCREENCAP_APP_VERSIONS", "app_versions", True)


def get_auto_name() -> bool:
    """Return whether LLM auto-naming is enabled after recording (True = on)."""
    return _parse_bool_env("SCREENCAP_AUTO_NAME", "auto_name", True)


def get_auto_update() -> bool:
    """Return whether auto-update checking is enabled (True = on)."""
    return _parse_bool_env("SCREENCAP_AUTO_UPDATE", "auto_update", True)


def get_auto_name_local_only() -> bool:
    """Return whether LLM auto-naming is restricted to local providers only."""
    return _parse_bool_env(
        "SCREENCAP_AUTO_NAME_LOCAL_ONLY", "auto_name_local_only", False,
    )


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


def get_sessions_dir() -> Path:
    """Return sessions directory, creating it if needed."""
    env = os.environ.get("SCREENCAP_SESSIONS_DIR")
    if env:
        p = Path(env)
    else:
        cfg = _load_toml()
        p = Path(cfg.get("sessions_dir", str(_DEFAULT_SESSIONS)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_base_dir() -> Path:
    """Return ~/.screencap/, creating it if needed."""
    _DEFAULT_BASE.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_BASE


def get_disk_warn_mb() -> int:
    """Minimum free MB to start recording / show warning. Default 2000."""
    return _parse_nonneg_int_env("SCREENCAP_DISK_WARN_MB", "disk_warn_mb", 2000)


def get_disk_stop_mb() -> int:
    """Free MB threshold to auto-stop recording. Default 500."""
    return _parse_nonneg_int_env("SCREENCAP_DISK_STOP_MB", "disk_stop_mb", 500)


def get_chunk_duration() -> float:
    """Return auto-cut chunk duration in seconds. Default 900 (15 min). 0 = legacy."""
    env = os.environ.get("SCREENCAP_CHUNK_DURATION")
    if env is not None:
        return float(env)
    cfg = _load_toml()
    return float(cfg.get("chunk_duration", 900.0))


def get_auto_delete_after_upload() -> bool:
    """Return whether to auto-delete chunks after confirmed upload. Default True."""
    return _parse_bool_env(
        "SCREENCAP_AUTO_DELETE", "auto_delete_after_upload", True,
    )


def get_rest_threshold() -> float:
    """Return rest threshold in seconds for task segmentation. Default 120."""
    env = os.environ.get("SCREENCAP_REST_THRESHOLD")
    if env is not None:
        return float(env)
    cfg = _load_toml()
    return float(cfg.get("rest_threshold", 120.0))


def get_segmentation_mode() -> str:
    """Return segmentation mode: 'idle' or 'llm'. Default 'llm'.

    Reads from config.toml key ``segmentation_mode``.
    CLI flag ``--segmentation-mode`` takes priority (passed directly, not via this function).

    Controls manifest format:
    - 'idle': old manifest with tasks (format_version absent)
    - 'llm': simplified manifest with chunk metadata only (format_version: 2)
    """
    valid = ("idle", "llm")
    cfg = _load_toml()
    val = cfg.get("segmentation_mode", "llm")
    if not isinstance(val, str) or val not in valid:
        raise SystemExit(
            f"Error: segmentation_mode must be one of {valid}, got: {val!r}"
        )
    return val


def get_show_on_website() -> bool:
    """Return whether recordings should be visible on the website. Default True."""
    return _parse_bool_env("SCREENCAP_SHOW_ON_WEBSITE", "show_on_website", True)


def get_upload_default() -> str:
    """Return default recording destination: 'local', 'cloud', 'both', or 'ask'.

    Priority: SCREENCAP_UPLOAD_DEFAULT env var > privacy.upload_default config > 'ask'.
    """
    valid = ("local", "cloud", "both", "ask")
    env = os.environ.get("SCREENCAP_UPLOAD_DEFAULT")
    if env is not None:
        val = env.strip().lower()
        if val not in valid:
            raise SystemExit(
                f"Error: SCREENCAP_UPLOAD_DEFAULT must be one of {valid}, got: {env!r}"
            )
        return val
    cfg = _load_toml()
    section = cfg.get("privacy", {})
    if isinstance(section, dict):
        val = section.get("upload_default", "ask")
        if not isinstance(val, str):
            raise SystemExit(
                f"Error: privacy.upload_default must be a string, got: {type(val).__name__}"
            )
        val = val.lower()
        if val not in valid:
            raise SystemExit(
                f"Error: privacy.upload_default must be one of {valid}, got: {val!r}"
            )
        return val
    return "ask"


def get_privacy_config():
    """Return a PrivacyConfig parsed from [privacy] in config.toml.

    Deferred import to avoid circular deps and keep CLI startup fast.
    """
    from screencap.privacy.policy import parse_privacy_config

    return parse_privacy_config(_load_toml())


def get_network_config():
    """Return a NetworkConfig parsed from [network] in config.toml.

    V1 fields only — see :class:`screencap.network.config.NetworkConfig`.
    Deferred import to avoid circular deps and keep CLI startup fast.
    """
    from screencap.network.config import parse_network_config

    return parse_network_config(_load_toml().get("network", {}))


def get_first_seen_prompt_enabled() -> bool:
    """Return whether the first-seen privacy prompt is enabled. Default True.

    Reads ``[menubar].first_seen_prompt`` from config.toml.
    Env var ``SCREENCAP_FIRST_SEEN_PROMPT`` overrides the config.
    """
    env = os.environ.get("SCREENCAP_FIRST_SEEN_PROMPT")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    section = cfg.get("menubar", {})
    if isinstance(section, dict):
        val = section.get("first_seen_prompt", True)
        if isinstance(val, bool):
            return val
    return True


def resolve_recording_dir(name: str) -> Path:
    """Resolve a recording name to a directory path, with traversal protection.

    Raises ValueError if the resolved path escapes the recordings directory.
    """
    recordings_dir = get_recordings_dir()
    recording_dir = (recordings_dir / name).resolve()
    if not recording_dir.is_relative_to(recordings_dir.resolve()):
        raise ValueError(f"Invalid recording name: {name}")
    return recording_dir
