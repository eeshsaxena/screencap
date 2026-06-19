"""Tests for screencap.enforcement.persistence — cross-session disable persistence."""

from __future__ import annotations

import sys

import pytest

from screencap import config as config_module
from screencap.enforcement.persistence import persist_disable

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _read_config(path) -> dict:
    return tomllib.loads(path.read_text())


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Make sure the global cache doesn't leak across tests."""
    config_module.invalidate_config_cache()
    yield
    config_module.invalidate_config_cache()


# ---------------------------------------------------------------------------
# Native app → exclude_apps
# ---------------------------------------------------------------------------


class TestExcludeApps:
    def test_appends_to_exclude_apps(self, tmp_path):
        cfg = tmp_path / "config.toml"
        added = persist_disable(
            "com.tinyspeck.slackmacgap", None, config_path=cfg,
        )
        assert added is True
        data = _read_config(cfg)
        assert "com.tinyspeck.slackmacgap" in data["privacy"]["exclude_apps"]

    def test_idempotent_on_duplicate(self, tmp_path):
        cfg = tmp_path / "config.toml"
        first = persist_disable("com.example.app", None, config_path=cfg)
        second = persist_disable("com.example.app", None, config_path=cfg)
        assert first is True
        assert second is False
        data = _read_config(cfg)
        # Only one entry, no duplication
        assert data["privacy"]["exclude_apps"].count("com.example.app") == 1

    def test_appends_to_existing_exclude_apps(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[privacy]\nexclude_apps = [\"com.first.app\"]\n",
        )
        added = persist_disable("com.second.app", None, config_path=cfg)
        assert added is True
        data = _read_config(cfg)
        assert "com.first.app" in data["privacy"]["exclude_apps"]
        assert "com.second.app" in data["privacy"]["exclude_apps"]

    def test_creates_config_if_missing(self, tmp_path):
        cfg = tmp_path / "subdir" / "config.toml"
        added = persist_disable("com.example.app", None, config_path=cfg)
        assert added is True
        assert cfg.exists()
        data = _read_config(cfg)
        assert data["privacy"]["exclude_apps"] == ["com.example.app"]

    def test_empty_bundle_id_returns_false(self, tmp_path):
        cfg = tmp_path / "config.toml"
        added = persist_disable("", None, config_path=cfg)
        assert added is False
        assert not cfg.exists()


# ---------------------------------------------------------------------------
# Browser tabs → mask_domains
# ---------------------------------------------------------------------------


class TestMaskDomains:
    def test_appends_to_mask_domains(self, tmp_path):
        cfg = tmp_path / "config.toml"
        added = persist_disable(
            "com.google.Chrome", "chase.com", config_path=cfg,
        )
        assert added is True
        data = _read_config(cfg)
        assert "chase.com" in data["privacy"]["mask_domains"]
        # Should NOT touch exclude_apps
        assert "exclude_apps" not in data["privacy"]

    def test_extracts_root_domain(self, tmp_path):
        cfg = tmp_path / "config.toml"
        added = persist_disable(
            "com.google.Chrome", "secure.login.chase.com", config_path=cfg,
        )
        assert added is True
        data = _read_config(cfg)
        assert "chase.com" in data["privacy"]["mask_domains"]
        assert "secure.login.chase.com" not in data["privacy"]["mask_domains"]

    def test_idempotent_on_duplicate_domain(self, tmp_path):
        cfg = tmp_path / "config.toml"
        first = persist_disable(
            "com.google.Chrome", "github.com", config_path=cfg,
        )
        second = persist_disable(
            "com.google.Chrome", "www.github.com", config_path=cfg,
        )
        assert first is True
        # www.github.com → github.com (root) → already present
        assert second is False
        data = _read_config(cfg)
        assert data["privacy"]["mask_domains"].count("github.com") == 1


# ---------------------------------------------------------------------------
# Comment preservation (tomlkit round-trip)
# ---------------------------------------------------------------------------


class TestCommentPreservation:
    def test_preserves_inline_comments(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "# Top-of-file comment\n"
            "\n"
            "[privacy]\n"
            "# Apps the user manually blocked\n"
            "exclude_apps = [\"com.first.app\"]\n",
        )
        added = persist_disable("com.second.app", None, config_path=cfg)
        assert added is True
        text = cfg.read_text()
        assert "# Top-of-file comment" in text
        assert "# Apps the user manually blocked" in text
        # And the new entry was appended
        data = _read_config(cfg)
        assert "com.second.app" in data["privacy"]["exclude_apps"]

    def test_preserves_unrelated_sections(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            'recordings_dir = "/custom/path"\n'
            "audio_default = false\n"
            "\n"
            "[menubar]\n"
            "first_seen_prompt = true\n",
        )
        added = persist_disable("com.example.app", None, config_path=cfg)
        assert added is True
        data = _read_config(cfg)
        assert data["recordings_dir"] == "/custom/path"
        assert data["audio_default"] is False
        assert data["menubar"]["first_seen_prompt"] is True
        assert "com.example.app" in data["privacy"]["exclude_apps"]


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    def test_cache_invalidated_after_write(self, tmp_path, monkeypatch):
        """Next config read after persist_disable should see the new entry."""
        cfg = tmp_path / "config.toml"
        # Redirect the global config path to the tmp file
        monkeypatch.setattr(config_module, "_CONFIG_PATH", cfg)

        cfg.write_text("[privacy]\nmode = \"public\"\n")
        # Prime the cache
        config_module._load_toml()
        assert config_module._config_cache is not None

        # Persist a new entry (uses tmp_path → still hits the same cfg)
        persist_disable("com.example.app", None, config_path=cfg)

        # Cache should be cleared by persist_disable
        assert config_module._config_cache is None

        # Re-loading sees the new entry
        fresh = config_module._load_toml()
        assert "com.example.app" in fresh["privacy"]["exclude_apps"]


# ---------------------------------------------------------------------------
# Lockfile contention
# ---------------------------------------------------------------------------


class TestLockfileContention:
    def test_creates_lockfile(self, tmp_path):
        cfg = tmp_path / "config.toml"
        persist_disable("com.example.app", None, config_path=cfg)
        assert (tmp_path / "config.lock").exists()

    def test_concurrent_persists_serialize(self, tmp_path):
        """Two persist_disable calls in quick succession both succeed.

        This is not a true contention test (we don't fork) but it
        verifies that the lock is released between calls so a second
        caller in the same process can acquire it.
        """
        cfg = tmp_path / "config.toml"
        a = persist_disable("com.first.app", None, config_path=cfg)
        b = persist_disable("com.second.app", None, config_path=cfg)
        assert a is True
        assert b is True
        data = _read_config(cfg)
        assert set(data["privacy"]["exclude_apps"]) == {
            "com.first.app", "com.second.app",
        }
