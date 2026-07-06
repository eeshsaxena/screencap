"""SCR-236 U3 — data-plane root resolver + path unification.

These tests pin the container-aware path seam:

- With the flag OFF (the default), every resolved data-plane path
  (recordings, content index, backfill ledger) is **byte-identical** to the
  pre-SCR-236 behavior — the flag is a pure no-op until U8 flips it.
- With the flag ON, the recordings tree resolves at the mountpoint and the
  sidecar DBs relocate under ``<data_root>/.store/``; the run-dir / config
  paths (socket, audit log, pidfile, config.toml) stay OUTSIDE, unchanged.
- ``SCREENCAP_RECORDINGS_DIR`` bypasses the container flow entirely (the
  documented dev/test seam, KTD-8).
- ``catalog.list_recordings`` ignores ``.store/`` and other dot-entries.

Vision-free; marked ``privacy`` so they run on the CI privacy lane.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.privacy


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the config cache between tests (config reads config.toml lazily)."""
    import screencap.config as cfg

    cfg.invalidate_config_cache()
    yield
    cfg.invalidate_config_cache()


@pytest.fixture
def _clean_env(monkeypatch):
    """Start each flag-off test from a known baseline: no recordings override,
    container explicitly OFF. The shipped default is now ON (KTD-8, U8), so
    flag-off behavior must be requested explicitly rather than inherited from
    the default."""
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "0")


# --------------------------------------------------------------------------- #
# container_enabled default                                                    #
# --------------------------------------------------------------------------- #


def test_container_enabled_by_default(monkeypatch):
    """The shipped default is ON (KTD-8, flipped in U8) — the release that
    ships this feature encrypts recordings at rest out of the box."""
    from screencap.config import container_enabled

    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.delenv("SCREENCAP_CONTAINER_ENABLED", raising=False)
    monkeypatch.setattr("screencap.config._load_toml", lambda: {})
    assert container_enabled() is True


def test_container_enabled_via_env(_clean_env, monkeypatch):
    from screencap.config import container_enabled

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    assert container_enabled() is True


# --------------------------------------------------------------------------- #
# Flag OFF: byte-identity with pre-SCR-236 behavior                            #
# --------------------------------------------------------------------------- #


def test_data_root_flag_off_is_default_recordings(_clean_env, tmp_path):
    """With no override and the flag off, get_data_root() == the default
    recordings dir (the pre-SCR-236 behavior)."""
    from screencap import config

    fake_default = tmp_path / "screencap" / "recordings"
    with mock.patch.object(config, "_DEFAULT_RECORDINGS", fake_default):
        assert config.get_data_root() == fake_default


def test_data_root_flag_off_byte_identical_to_recordings_dir(_clean_env):
    """Snapshot the *current* resolved values and assert get_data_root() matches
    get_recordings_dir() exactly — the load-bearing byte-identity guarantee."""
    from screencap import config

    # get_recordings_dir() mkdirs; get_data_root() does not. Compare the paths.
    assert config.get_data_root() == config.get_recordings_dir()


def test_content_index_path_flag_off_is_base_dir(_clean_env):
    """content_index.db stays at ~/.screencap/content_index.db when the flag is
    off — byte-identical to today (routed through get_base_dir())."""
    from screencap import config
    from screencap.content_index import default_index_path

    assert default_index_path() == config.get_base_dir() / "content_index.db"


def test_backfill_ledger_path_flag_off_is_base_dir(_clean_env):
    """backfill_state.db stays at ~/.screencap/backfill_state.db when off."""
    from screencap import config
    from screencap.backfill.ledger import default_ledger_path

    assert default_ledger_path() == config.get_base_dir() / "backfill_state.db"


# --------------------------------------------------------------------------- #
# Flag ON: recordings + sidecars relocate; run-dir stays outside              #
# --------------------------------------------------------------------------- #


def test_data_root_flag_on_is_mountpoint(_clean_env, monkeypatch, tmp_path):
    """Flag on, no override → the recordings mountpoint, which IS the default
    recordings dir (KTD-2, no symlinks)."""
    from screencap import config

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    fake_default = tmp_path / "screencap" / "recordings"
    with mock.patch.object(config, "_DEFAULT_RECORDINGS", fake_default):
        assert config.get_data_root() == fake_default


def test_store_dir_under_data_root(_clean_env, monkeypatch, tmp_path):
    """The reserved sidecar dir is <data_root>/.store/ and is created."""
    from screencap import config

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    fake_default = tmp_path / "screencap" / "recordings"
    with mock.patch.object(config, "_DEFAULT_RECORDINGS", fake_default):
        store = config.get_store_dir()
        assert store == fake_default / ".store"
        assert store.is_dir()


def test_content_index_path_flag_on_under_store(_clean_env, monkeypatch, tmp_path):
    from screencap import config
    from screencap.content_index import default_index_path

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    fake_default = tmp_path / "screencap" / "recordings"
    with mock.patch.object(config, "_DEFAULT_RECORDINGS", fake_default):
        assert default_index_path() == fake_default / ".store" / "content_index.db"


def test_backfill_ledger_path_flag_on_under_store(_clean_env, monkeypatch, tmp_path):
    from screencap import config
    from screencap.backfill.ledger import default_ledger_path

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    fake_default = tmp_path / "screencap" / "recordings"
    with mock.patch.object(config, "_DEFAULT_RECORDINGS", fake_default):
        assert default_ledger_path() == fake_default / ".store" / "backfill_state.db"


def test_sidecars_are_siblings_under_store(_clean_env, monkeypatch, tmp_path):
    """The ledger stays a SIBLING of the index (in .store/), not a table
    inside it — the module-docstring invariant survives the relocation."""
    from screencap import config
    from screencap.backfill.ledger import default_ledger_path
    from screencap.content_index import default_index_path

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    fake_default = tmp_path / "screencap" / "recordings"
    with mock.patch.object(config, "_DEFAULT_RECORDINGS", fake_default):
        assert default_index_path().parent == default_ledger_path().parent


def test_run_dir_paths_stay_outside_container(_clean_env, monkeypatch, tmp_path):
    """Socket, audit log, pidfile, and config.toml resolve OUTSIDE the container
    even with the flag on — they must exist before any mount (KTD-3)."""
    from screencap import config

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    fake_default = tmp_path / "screencap" / "recordings"
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with mock.patch.object(config, "_DEFAULT_RECORDINGS", fake_default), \
            mock.patch.object(Path, "home", staticmethod(lambda: fake_home)):
        from screencap.daemon.audit_log import _audit_log_path
        from screencap.daemon.socket import default_socket_path

        data_root = config.get_data_root()

        socket = default_socket_path()
        audit = _audit_log_path()

        # None of these live inside the (mounted) data root.
        for p in (socket, audit):
            assert not p.is_relative_to(data_root)
            assert p.is_relative_to(fake_home / ".screencap")

    # pidfile + config.toml are module constants under ~/.screencap (run/config
    # dir), never under the recordings mountpoint.
    import screencap.enforcement.persistence as persistence
    import screencap.pidfile as pidfile

    assert pidfile.PID_FILE.parent.name == ".screencap"
    assert persistence._CONFIG_PATH.name == "config.toml"
    assert persistence._CONFIG_PATH.parent.name == ".screencap"


# --------------------------------------------------------------------------- #
# SCREENCAP_RECORDINGS_DIR bypasses the container flow                          #
# --------------------------------------------------------------------------- #


def test_env_override_bypasses_container(_clean_env, monkeypatch, tmp_path):
    """SCREENCAP_RECORDINGS_DIR set + flag on → container resolution bypassed;
    all data-plane paths land at the override (plaintext dev/test seam, KTD-8)."""
    from screencap import config
    from screencap.backfill.ledger import default_ledger_path
    from screencap.content_index import default_index_path

    override = tmp_path / "override-recs"
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(override))
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")

    assert config.get_data_root() == override
    assert config.get_store_dir() == override / ".store"
    # Sidecars still route through container_enabled(), so with the flag on they
    # relocate under the override's .store/ (the override *is* the data root).
    assert default_index_path() == override / ".store" / "content_index.db"
    assert default_ledger_path() == override / ".store" / "backfill_state.db"


def test_non_default_recordings_dir_bypasses_container(_clean_env, monkeypatch, tmp_path):
    """A non-default recordings_dir config.toml value is treated like the env
    override — bypass, plaintext (KTD-8)."""
    from screencap import config

    external = tmp_path / "external-volume" / "recs"
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    monkeypatch.setattr(config, "_load_toml", lambda: {"recordings_dir": str(external)})

    assert config.get_data_root() == external


# --------------------------------------------------------------------------- #
# catalog.list_recordings ignores dot-entries                                  #
# --------------------------------------------------------------------------- #


def _make_recording(base: Path, name: str) -> Path:
    """Create a minimally-listable recording dir (real engine DB)."""
    from screencap.engine.db import create_db, crud

    d = base / name
    d.mkdir(parents=True)
    engine, Session = create_db(str(d / "recording.db"))
    session = Session()
    recording = crud.insert_recording(session, {
        "timestamp": time.time() - 60,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    crud.insert_action_event(session, recording, time.time(), {
        "name": "click",
        "mouse_x": 1.0,
        "mouse_y": 2.0,
        "mouse_button_name": "left",
        "mouse_pressed": True,
    })
    session.close()
    engine.dispose()
    return d


def test_list_recordings_skips_dot_store(tmp_path):
    """.store/ (and other dot-dirs) are never enumerated as recordings."""
    from screencap.catalog import list_recordings

    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()

    _make_recording(recordings_dir, "real-recording")

    # A .store/ dir that even contains a stray DB must still be ignored.
    store = recordings_dir / ".store"
    store.mkdir()
    (store / "content_index.db").write_bytes(b"not a recording")
    (recordings_dir / ".hidden").mkdir()

    result = list_recordings(recordings_dir)
    names = [r.name for r in result]
    assert names == ["real-recording"]
