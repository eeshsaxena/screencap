"""Tests for screencap.network.config (V1 + V1.5)."""

from __future__ import annotations

import pytest

from screencap.network.config import (
    InvalidNetworkConfigError,
    NetworkConfig,
    parse_network_config,
)


class TestParseNetworkConfig:
    def test_empty_section_defaults(self):
        cfg = parse_network_config({})
        assert cfg.extra_blocklist == frozenset()
        # Default 0 means auto-negotiate within 8080-8090. Setting a
        # non-zero default would shadow the auto-negotiate branch in
        # preflight_or_raise (a busy 8080 would hard-fail instead of
        # falling back to 8081).
        assert cfg.proxy_port == 0
        assert cfg.override_default_blocklist is False
        assert cfg.body_size_cap == 100_000
        assert cfg.capture_bodies_for == frozenset()
        assert cfg.override_default_capture_bodies_for is False

    def test_none_section_defaults(self):
        cfg = parse_network_config(None)
        assert cfg == NetworkConfig()

    def test_full_valid_section(self):
        cfg = parse_network_config({
            "extra_blocklist": ["MyCompany.com", "secret.local"],
            "proxy_port": 9090,
            "override_default_blocklist": True,
            "body_size_cap": 50_000,
        })
        assert cfg.extra_blocklist == frozenset({"mycompany.com", "secret.local"})
        assert cfg.proxy_port == 9090
        assert cfg.override_default_blocklist is True
        assert cfg.body_size_cap == 50_000

    def test_body_size_cap_default(self):
        cfg = parse_network_config({"proxy_port": 8081})
        assert cfg.body_size_cap == 100_000

    def test_body_size_cap_explicit(self):
        cfg = parse_network_config({"body_size_cap": 1024 * 512})
        assert cfg.body_size_cap == 524_288

    def test_capture_bodies_for_parses_to_lowercase_frozenset(self, capsys):
        # V1.5: capture_bodies_for is now a real field (V1's warn-and-ignore
        # behavior is gone — verify no warning fires).
        cfg = parse_network_config({
            "capture_bodies_for": ["*.GitHub.com", "Notion.so"],
        })
        assert cfg.capture_bodies_for == frozenset({"*.github.com", "notion.so"})
        captured = capsys.readouterr()
        # Anti-regression for the V1 warning path.
        assert "V1.5" not in captured.out
        assert "ignored" not in captured.out

    def test_override_default_capture_bodies_for_true(self):
        cfg = parse_network_config({"override_default_capture_bodies_for": True})
        assert cfg.override_default_capture_bodies_for is True

    def test_override_default_capture_bodies_for_false_explicit(self):
        cfg = parse_network_config({"override_default_capture_bodies_for": False})
        assert cfg.override_default_capture_bodies_for is False

    def test_capture_bodies_for_not_a_list(self):
        with pytest.raises(
            InvalidNetworkConfigError,
            match="network.capture_bodies_for must be a list",
        ):
            parse_network_config({"capture_bodies_for": "github.com"})

    def test_capture_bodies_for_non_string_entry(self):
        with pytest.raises(
            InvalidNetworkConfigError,
            match=r"network\.capture_bodies_for\[0\] must be a string",
        ):
            parse_network_config({"capture_bodies_for": [42]})

    def test_override_default_capture_bodies_for_wrong_type(self):
        with pytest.raises(
            InvalidNetworkConfigError,
            match="network.override_default_capture_bodies_for must be a bool",
        ):
            parse_network_config({"override_default_capture_bodies_for": "yes"})

    # ----- error cases -----

    def test_extra_blocklist_not_a_list(self):
        with pytest.raises(
            InvalidNetworkConfigError, match="network.extra_blocklist must be a list"
        ):
            parse_network_config({"extra_blocklist": "github.com"})

    def test_extra_blocklist_non_string_entry(self):
        with pytest.raises(
            InvalidNetworkConfigError,
            match=r"network\.extra_blocklist\[1\] must be a string",
        ):
            parse_network_config({"extra_blocklist": ["ok.com", 42]})

    @pytest.mark.parametrize("port", [-1, 1023, 65536, 99999])
    def test_proxy_port_out_of_range(self, port):
        # 0 is now a valid sentinel ("auto-negotiate") so it's tested
        # separately below — only non-zero values outside [1024, 65535]
        # should raise.
        with pytest.raises(InvalidNetworkConfigError, match="network.proxy_port"):
            parse_network_config({"proxy_port": port})

    def test_proxy_port_zero_is_auto_negotiate(self):
        cfg = parse_network_config({"proxy_port": 0})
        assert cfg.proxy_port == 0

    def test_proxy_port_wrong_type(self):
        with pytest.raises(InvalidNetworkConfigError, match="must be an integer"):
            parse_network_config({"proxy_port": "8080"})

    def test_proxy_port_bool_rejected(self):
        # bool is a subclass of int; reject it explicitly.
        with pytest.raises(InvalidNetworkConfigError, match="must be an integer"):
            parse_network_config({"proxy_port": True})

    def test_override_default_blocklist_wrong_type(self):
        with pytest.raises(
            InvalidNetworkConfigError,
            match="network.override_default_blocklist must be a bool",
        ):
            parse_network_config({"override_default_blocklist": "yes"})

    @pytest.mark.parametrize("cap", [0, 1023, 10 * 1024 * 1024 + 1, 1_000_000_000])
    def test_body_size_cap_out_of_range(self, cap):
        with pytest.raises(InvalidNetworkConfigError, match="network.body_size_cap"):
            parse_network_config({"body_size_cap": cap})

    def test_body_size_cap_wrong_type(self):
        with pytest.raises(InvalidNetworkConfigError, match="must be an integer"):
            parse_network_config({"body_size_cap": "100000"})

    def test_section_not_a_dict(self):
        with pytest.raises(InvalidNetworkConfigError, match=r"\[network\] must be a table"):
            parse_network_config(["not", "a", "dict"])

    def test_dataclass_is_frozen(self):
        cfg = parse_network_config({})
        with pytest.raises((AttributeError, Exception)):
            cfg.proxy_port = 9999  # type: ignore[misc]
