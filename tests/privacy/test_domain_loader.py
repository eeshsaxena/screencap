"""Tests for domain_loader: UT1 loading, supplement, merge, parent expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from screencap.privacy.domain_loader import (
    _expand_parents,
    _normalize_domain,
    build_domain_index,
    load_supplement,
    load_ut1_domains,
)
from screencap.privacy.policy import ContextClass

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Domain normalization
# ---------------------------------------------------------------------------


class TestNormalizeDomain:
    def test_lowercase(self):
        assert _normalize_domain("Chase.Com") == "chase.com"

    def test_strip_trailing_dot(self):
        assert _normalize_domain("example.com.") == "example.com"

    def test_skip_blank(self):
        assert _normalize_domain("") is None
        assert _normalize_domain("   ") is None

    def test_skip_comment(self):
        assert _normalize_domain("# comment") is None

    def test_skip_ipv4(self):
        assert _normalize_domain("192.168.1.1") is None

    def test_skip_single_label(self):
        assert _normalize_domain("localhost") is None

    def test_valid_domain(self):
        assert _normalize_domain("mail.google.com") == "mail.google.com"


# ---------------------------------------------------------------------------
# UT1 loading
# ---------------------------------------------------------------------------


class TestLoadUT1Domains:
    def test_loads_from_real_data(self):
        """Smoke test: real UT1 data loads without errors."""
        result = load_ut1_domains()
        assert len(result) > 10000
        # Spot checks
        assert result.get("chase.com") == ContextClass.BANKING
        assert result.get("mail.google.com") == ContextClass.EMAIL

    def test_loads_from_custom_dir(self, tmp_path):
        (tmp_path / "bank.txt").write_text("mybank.com\n")
        (tmp_path / "financial.txt").write_text("myfin.com\n")
        (tmp_path / "webmail.txt").write_text("mymail.com\n")
        (tmp_path / "social_networks.txt").write_text("mysocial.com\n")
        (tmp_path / "chat.txt").write_text("mychat.com\n")
        (tmp_path / "vpn.txt").write_text("myvpn.com\n")
        result = load_ut1_domains(ut1_dir=tmp_path)
        assert result["mybank.com"] == ContextClass.BANKING
        assert result["myfin.com"] == ContextClass.BANKING
        assert result["mymail.com"] == ContextClass.EMAIL
        assert result["mysocial.com"] == ContextClass.CHAT
        assert result["mychat.com"] == ContextClass.CHAT
        assert result["myvpn.com"] == ContextClass.ADMIN_CONSOLE

    def test_skips_blank_and_ip_lines(self, tmp_path):
        content = "valid.com\n\n# comment\n192.168.1.1\n10.0.0.1\n\nalso-valid.com\n"
        (tmp_path / "bank.txt").write_text(content)
        for cat in ("financial", "webmail", "social_networks", "chat", "vpn"):
            (tmp_path / f"{cat}.txt").write_text("")
        result = load_ut1_domains(ut1_dir=tmp_path)
        assert "valid.com" in result
        assert "also-valid.com" in result
        assert len(result) == 2

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="UT1 data file missing"):
            load_ut1_domains(ut1_dir=tmp_path)

    def test_first_category_wins_on_conflict(self, tmp_path):
        """bank comes before financial in _UT1_CATEGORY_MAP iteration."""
        (tmp_path / "bank.txt").write_text("overlap.com\n")
        (tmp_path / "financial.txt").write_text("overlap.com\n")
        for cat in ("webmail", "social_networks", "chat", "vpn"):
            (tmp_path / f"{cat}.txt").write_text("")
        result = load_ut1_domains(ut1_dir=tmp_path)
        assert result["overlap.com"] == ContextClass.BANKING


# ---------------------------------------------------------------------------
# Supplement loading
# ---------------------------------------------------------------------------


class TestLoadSupplement:
    def test_loads_all_entries(self):
        result = load_supplement()
        assert len(result) > 50
        assert result["vault.bitwarden.com"] == ContextClass.PASSWORD_MANAGER
        assert result["drive.google.com"] == ContextClass.CLOUD_STORAGE
        assert result["github.com"] == ContextClass.CODE_EDITOR_TERMINAL
        assert result["app.slack.com"] == ContextClass.CHAT

    def test_all_entries_have_valid_context_class(self):
        from screencap.privacy.data.supplement import SUPPLEMENT

        valid_names = {m.name for m in ContextClass}
        for domain, cls_name in SUPPLEMENT.items():
            assert cls_name in valid_names, f"{domain} has invalid class {cls_name}"


# ---------------------------------------------------------------------------
# Parent domain expansion
# ---------------------------------------------------------------------------


class TestExpandParents:
    def test_expands_child_to_parent(self):
        index = {"secure.bankofamerica.com": ContextClass.BANKING}
        parents = _expand_parents(index)
        assert "bankofamerica.com" in parents
        assert parents["bankofamerica.com"] == ContextClass.BANKING

    def test_does_not_overwrite_existing_entries(self):
        index = {
            "sub.example.com": ContextClass.CHAT,
            "example.com": ContextClass.EMAIL,
        }
        parents = _expand_parents(index)
        # example.com already in index, should not be in parents
        assert "example.com" not in parents

    def test_skips_single_label_parents(self):
        index = {"a.com": ContextClass.BANKING}
        parents = _expand_parents(index)
        # "com" is single-label, not useful
        assert "com" not in parents

    def test_multi_level_expansion(self):
        index = {"a.b.c.example.com": ContextClass.BANKING}
        parents = _expand_parents(index)
        # Should create b.c.example.com, c.example.com, example.com
        assert "b.c.example.com" in parents
        assert "c.example.com" in parents
        assert "example.com" in parents


# ---------------------------------------------------------------------------
# Full domain index build
# ---------------------------------------------------------------------------


class TestBuildDomainIndex:
    def test_supplement_overwrites_ut1(self, tmp_path):
        """Supplement should override UT1 when both define the same domain."""
        (tmp_path / "bank.txt").write_text("overlap.com\n")
        for cat in ("financial", "webmail", "social_networks", "chat", "vpn"):
            (tmp_path / f"{cat}.txt").write_text("")
        ut1 = load_ut1_domains(ut1_dir=tmp_path)
        supplement = {"overlap.com": ContextClass.EMAIL}  # override
        index = build_domain_index(ut1=ut1, supplement=supplement)
        assert index["overlap.com"] == ContextClass.EMAIL

    def test_parent_expansion_included(self, tmp_path):
        for cat in ("bank", "financial", "webmail", "social_networks", "chat", "vpn"):
            (tmp_path / f"{cat}.txt").write_text("")
        (tmp_path / "bank.txt").write_text("secure.mybank.com\n")
        index = build_domain_index(
            ut1=load_ut1_domains(ut1_dir=tmp_path), supplement={}
        )
        assert "mybank.com" in index
        assert index["mybank.com"] == ContextClass.BANKING

    def test_real_data_builds_successfully(self):
        """Smoke test: full build with real data."""
        index = build_domain_index()
        assert len(index) > 14000
        # Check UT1 domains are present
        assert index.get("chase.com") == ContextClass.BANKING
        # Check supplement domains are present
        assert index.get("vault.bitwarden.com") == ContextClass.PASSWORD_MANAGER
        assert index.get("drive.google.com") == ContextClass.CLOUD_STORAGE
