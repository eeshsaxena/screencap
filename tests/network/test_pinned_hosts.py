"""Tests for the persistent pinned-hosts cache (V1.5)."""

from __future__ import annotations

import json

import pytest

from screencap.network.pinned_hosts import (
    add_known_pinned_host,
    load_known_pinned_hosts,
    remove_known_pinned_host,
)


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "known_pinned_hosts.json"


class TestLoadKnownPinnedHosts:
    def test_missing_file_returns_empty(self, cache_path):
        assert load_known_pinned_hosts(cache_path) == set()

    def test_round_trip(self, cache_path):
        cache_path.write_text(
            json.dumps({"hosts": ["api.example.com", "app.signal.org"]})
        )
        assert load_known_pinned_hosts(cache_path) == {
            "api.example.com",
            "app.signal.org",
        }

    def test_lowercases_entries(self, cache_path):
        cache_path.write_text(json.dumps({"hosts": ["API.Example.COM"]}))
        assert load_known_pinned_hosts(cache_path) == {"api.example.com"}

    def test_malformed_json_returns_empty(self, cache_path):
        cache_path.write_text("not json {{")
        assert load_known_pinned_hosts(cache_path) == set()

    def test_unexpected_shape_returns_empty(self, cache_path):
        cache_path.write_text(json.dumps(["api.example.com"]))  # list, not dict
        assert load_known_pinned_hosts(cache_path) == set()

    def test_hosts_field_wrong_type_returns_empty(self, cache_path):
        cache_path.write_text(json.dumps({"hosts": "api.example.com"}))
        assert load_known_pinned_hosts(cache_path) == set()


class TestAddKnownPinnedHost:
    def test_add_creates_file(self, cache_path):
        assert not cache_path.exists()
        result = add_known_pinned_host("api.example.com", cache_path)
        assert result is True
        assert load_known_pinned_hosts(cache_path) == {"api.example.com"}

    def test_add_lowercases(self, cache_path):
        add_known_pinned_host("API.Example.COM", cache_path)
        assert load_known_pinned_hosts(cache_path) == {"api.example.com"}

    def test_add_existing_is_idempotent(self, cache_path):
        add_known_pinned_host("api.example.com", cache_path)
        first_mtime = cache_path.stat().st_mtime_ns
        result = add_known_pinned_host("api.example.com", cache_path)
        assert result is False
        # File untouched on second call.
        assert cache_path.stat().st_mtime_ns == first_mtime

    def test_add_appends_to_existing(self, cache_path):
        add_known_pinned_host("a.com", cache_path)
        add_known_pinned_host("b.com", cache_path)
        assert load_known_pinned_hosts(cache_path) == {"a.com", "b.com"}

    def test_add_empty_string_returns_false(self, cache_path):
        assert add_known_pinned_host("", cache_path) is False
        assert not cache_path.exists()

    def test_persisted_format_is_sorted_list(self, cache_path):
        add_known_pinned_host("z.com", cache_path)
        add_known_pinned_host("a.com", cache_path)
        data = json.loads(cache_path.read_text())
        assert data == {"hosts": ["a.com", "z.com"]}


class TestRemoveKnownPinnedHost:
    def test_remove_existing(self, cache_path):
        add_known_pinned_host("a.com", cache_path)
        add_known_pinned_host("b.com", cache_path)
        assert remove_known_pinned_host("a.com", cache_path) is True
        assert load_known_pinned_hosts(cache_path) == {"b.com"}

    def test_remove_missing_returns_false(self, cache_path):
        add_known_pinned_host("a.com", cache_path)
        assert remove_known_pinned_host("b.com", cache_path) is False
        assert load_known_pinned_hosts(cache_path) == {"a.com"}

    def test_remove_from_missing_file(self, cache_path):
        assert remove_known_pinned_host("a.com", cache_path) is False
