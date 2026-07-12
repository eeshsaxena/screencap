"""SCR-236 U3 — data-plane root resolver, path unification, dot-entry skip.

Pins that:

* with the container flag OFF (or ``SCREENCAP_RECORDINGS_DIR`` set) every
  resolved data path is byte-identical to today's ``~/.screencap``-relative
  layout (the flag-off regression snapshot — no behavior change);
* with the flag ON the recordings tree, content index, and backfill ledger
  resolve under the recordings mountpoint's reserved ``.store/`` dir, while the
  run-dir paths (socket, audit log, pidfile, config.toml) stay OUTSIDE;
* ``catalog.list_recordings`` never surfaces a dot-entry (e.g. ``.store/``) as a
  phantom recording — including a ``.store/`` that carries a stray
  ``recording.db``.

Vision-free; marked ``@pytest.mark.privacy`` so the CI privacy lane runs it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import screencap.config as cfg
from screencap import catalog, content_index
from screencap.backfill import ledger

pytestmark = pytest.mark.privacy


@pytest.fixture
def isolated_base(tmp_path, monkeypatch):
    """Point config's default base + recordings at a hermetic tmp dir.

    Mirrors the daemon-conftest isolation: patch the module-level defaults and the
    config path, and clear the two env seams so neither a developer's shell nor a
    real ``~/.screencap/config.toml`` leaks into the resolution under test.
    """
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    monkeypatch.setattr(cfg, "_DEFAULT_RECORDINGS", base / "recordings")
    monkeypatch.setattr(cfg, "_DEFAULT_DOWNLOADS", base / "downloads")
    monkeypatch.setattr(cfg, "_CONFIG_PATH", base / "config.toml")
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.delenv("SCREENCAP_CONTAINER_ENABLED", raising=False)
    cfg.invalidate_config_cache()
    return base


# ---------------------------------------------------------------------------
# Flag OFF — regression snapshot: byte-identical to today's layout.
# ---------------------------------------------------------------------------


def test_flag_off_paths_are_todays_layout(isolated_base):
    """Every data path resolves under ``~/.screencap`` exactly as before U3."""
    assert cfg.get_container_enabled() is False  # default OFF (U8 flips it later)

    base = isolated_base
    # get_base_dir is the anchor today's formulas are written against.
    assert cfg.get_base_dir() == base
    # data_root + store dir collapse onto the base while the flag is off.
    assert cfg.get_data_root() == base
    assert cfg.get_store_dir() == base
    # The two local-only sidecars keep their historical sibling locations.
    assert content_index.default_index_path() == base / "content_index.db"
    assert ledger.default_ledger_path() == base / "backfill_state.db"
    # Recordings tree unchanged.
    assert cfg.get_recordings_dir() == base / "recordings"


# ---------------------------------------------------------------------------
# Flag ON — data plane relocates into the mountpoint's ``.store/``; run-dir out.
# ---------------------------------------------------------------------------


def test_flag_on_sidecars_move_under_mountpoint_store(isolated_base, monkeypatch):
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    cfg.invalidate_config_cache()

    assert cfg.get_container_enabled() is True
    mountpoint = cfg.get_recordings_dir()
    assert mountpoint == isolated_base / "recordings"

    # data_root is the recordings mountpoint (KTD-2).
    assert cfg.get_data_root() == mountpoint
    # Sidecars live in the reserved ``.store/`` inside the mountpoint (and it is
    # created on resolution).
    store = cfg.get_store_dir()
    assert store == mountpoint / ".store"
    assert store.is_dir()
    assert content_index.default_index_path() == mountpoint / ".store" / "content_index.db"
    assert ledger.default_ledger_path() == mountpoint / ".store" / "backfill_state.db"


def test_flag_on_run_dir_paths_stay_outside(isolated_base, monkeypatch):
    """Socket, audit log, pidfile, config.toml never resolve inside the container."""
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    cfg.invalidate_config_cache()

    from screencap import pidfile
    from screencap.daemon.audit_log import _audit_log_path
    from screencap.daemon.socket import default_socket_path
    from screencap.enforcement import persistence

    data_root = cfg.get_data_root()
    run_dir_paths = [
        default_socket_path(),
        _audit_log_path(),
        pidfile.PID_FILE,
        persistence._CONFIG_PATH,
    ]
    for p in run_dir_paths:
        assert not p.is_relative_to(data_root), f"{p} must stay outside {data_root}"
        assert ".store" not in p.parts


# ---------------------------------------------------------------------------
# SCREENCAP_RECORDINGS_DIR override — container resolution bypassed.
# ---------------------------------------------------------------------------


def test_recordings_dir_override_bypasses_container(isolated_base, tmp_path, monkeypatch):
    """Env override wins even with the flag ON: recordings land at the override,
    sidecars stay at today's plaintext base (no ``.store/`` relocation)."""
    override = tmp_path / "external-volume" / "recordings"
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(override))
    cfg.invalidate_config_cache()

    # Recordings land at the override.
    assert cfg.get_recordings_dir() == override
    # Container resolution is bypassed → data_root + sidecars fall back to today.
    assert cfg.get_data_root() == isolated_base
    assert content_index.default_index_path() == isolated_base / "content_index.db"
    assert ledger.default_ledger_path() == isolated_base / "backfill_state.db"
    # Nothing relocated under the override.
    assert not (override / ".store").exists()


# ---------------------------------------------------------------------------
# catalog.list_recordings ignores ``.store/`` and other dot-entries.
# ---------------------------------------------------------------------------


def _seed_recording(directory: Path) -> None:
    """Create a minimal recording dir whose ``recording.db`` is enough to list."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "recording.db").write_bytes(b"")  # unreadable-but-present is fine


def test_list_recordings_skips_dot_entries(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()

    # One genuine recording.
    _seed_recording(recordings / "rec1")
    # A reserved ``.store/`` dir carrying a STRAY ``recording.db`` — the exact
    # phantom-recording case the explicit dot-skip closes (``sorted()`` orders it
    # first, so it would otherwise be considered before rec1).
    _seed_recording(recordings / ".store")
    # Any other dot-dir with a recording.db must also be ignored.
    _seed_recording(recordings / ".hidden")

    infos = catalog.list_recordings(recordings_dir=recordings)
    names = {info.name for info in infos}

    assert names == {"rec1"}
    assert ".store" not in names
    assert ".hidden" not in names
