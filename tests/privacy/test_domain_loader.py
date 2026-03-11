"""Tests for domain_loader: UT1 loading, supplement, merge."""

from __future__ import annotations

from pathlib import Path

import pytest

from screencap.privacy.domain_loader import (
    _is_tld_like,
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

    def test_skip_url_with_path(self):
        assert _normalize_domain("app.proton.me/pass") is None


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

    def test_no_false_positive_parent_expansion(self):
        """Shared hosts like google.com must NOT be in the index."""
        index = build_domain_index()
        # mail.google.com is in UT1 but google.com should not be expanded
        assert "mail.google.com" in index
        assert "google.com" not in index
        # Same for other multi-purpose domains
        assert "amazon.com" not in index
        assert "cloudflare.com" not in index


# ---------------------------------------------------------------------------
# TLD-like detection (security-relevant for parent walk-up)
# ---------------------------------------------------------------------------


class TestIsTldLike:
    @pytest.mark.parametrize("domain", [
        "co.uk", "com.au", "org.br", "ac.jp", "gov.uk",
        "or.jp", "go.com", "go.kr", "gr.jp", "asn.au", "web.app", "my.id",
    ])
    def test_rejects_tld_like_parents(self, domain):
        assert _is_tld_like(domain) is True

    @pytest.mark.parametrize("domain", ["bankofamerica.com", "google.co.uk", "example.org"])
    def test_keeps_real_domains(self, domain):
        assert _is_tld_like(domain) is False
