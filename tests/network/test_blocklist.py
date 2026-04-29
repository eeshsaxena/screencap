"""Tests for screencap.network.blocklist (V1)."""

from __future__ import annotations

import re

import pytest

from screencap.network.blocklist import (
    DEFAULT_BLOCKLIST,
    build_ignore_hosts_regex,
    is_host_blocked,
    is_ip_literal,
)
from screencap.network.config import NetworkConfig
from screencap.privacy.policy import PrivacyConfig


def _privacy(mask_domains: set[str] | None = None) -> PrivacyConfig:
    return PrivacyConfig(
        mask_domains=frozenset(d.lower() for d in (mask_domains or set()))
    )


def _network(
    *,
    extra: set[str] | None = None,
    override_default: bool = False,
    proxy_port: int = 8080,
) -> NetworkConfig:
    return NetworkConfig(
        extra_blocklist=frozenset(h.lower() for h in (extra or set())),
        proxy_port=proxy_port,
        override_default_blocklist=override_default,
        body_size_cap=100_000,
    )


class TestIsIpLiteral:
    def test_ipv4(self):
        assert is_ip_literal("192.168.1.1") is True
        assert is_ip_literal("10.0.0.1") is True
        assert is_ip_literal("127.0.0.1") is True

    def test_ipv6_bracketed(self):
        assert is_ip_literal("[::1]") is True
        assert is_ip_literal("[2001:db8::1]") is True

    def test_ipv6_bare(self):
        assert is_ip_literal("::1") is True

    def test_hostnames(self):
        assert is_ip_literal("github.com") is False
        assert is_ip_literal("api.example.com") is False

    def test_empty_input(self):
        # Don't crash on empty input.
        assert is_ip_literal("") is False

    def test_only_brackets(self):
        # Strip the brackets and see we have nothing left.
        assert is_ip_literal("[]") is False


class TestIsHostBlocked:
    def test_mask_domains_match(self):
        privacy = _privacy({"chase.com"})
        net = _network()
        assert is_host_blocked("api.chase.com", privacy, net) is True
        assert is_host_blocked("chase.com", privacy, net) is True

    def test_extra_blocklist_match(self):
        net = _network(extra={"plaid.com"}, override_default=True)
        assert is_host_blocked("foo.plaid.com", _privacy(), net) is True
        assert is_host_blocked("plaid.com", _privacy(), net) is True

    def test_default_blocklist_active_by_default(self):
        # Empty extra_blocklist + default not overridden → DEFAULT entries
        # still active.
        net = _network()
        assert is_host_blocked("vault.bitwarden.com", _privacy(), net) is True
        assert is_host_blocked("login.microsoftonline.com", _privacy(), net) is True
        assert is_host_blocked("api.stripe.com", _privacy(), net) is True

    def test_override_default_disables_curated_list(self):
        # override_default_blocklist=True + empty extra_blocklist →
        # only IP literals remain blocked.
        net = _network(extra=set(), override_default=True)
        assert is_host_blocked("vault.bitwarden.com", _privacy(), net) is False
        assert is_host_blocked("api.chase.com", _privacy(), net) is False
        # IP literal still blocked.
        assert is_host_blocked("192.168.1.1", _privacy(), net) is True

    def test_unrelated_host_not_blocked(self):
        net = _network(extra={"acme.test"}, override_default=True)
        assert is_host_blocked("github.com", _privacy(), net) is False

    def test_empty_host(self):
        # Don't crash on empty input.
        assert is_host_blocked("", _privacy(), _network()) is False

    def test_ip_literal_blocked(self):
        net = _network(override_default=True)
        assert is_host_blocked("192.168.1.1", _privacy(), net) is True
        assert is_host_blocked("[::1]", _privacy(), net) is True

    # -------------------------------------------------------------------
    # Adversarial wildcard / suffix-bypass tests.
    # `bare in host` (substring containment) would erroneously match
    # several of these. Use `host == bare or host.endswith("." + bare)`.
    # -------------------------------------------------------------------

    @pytest.mark.parametrize("hostile", [
        "mycompany.com.evil.com",
        "notmycompany.com",
        "mycompany.com.attacker.example",
        "something-mycompany.com",
    ])
    def test_extra_blocklist_no_substring_bypass(self, hostile):
        net = _network(extra={"mycompany.com"}, override_default=True)
        assert is_host_blocked(hostile, _privacy(), net) is False

    @pytest.mark.parametrize("legit", [
        "mycompany.com",
        "api.mycompany.com",
        "deep.nested.mycompany.com",
    ])
    def test_extra_blocklist_legitimate_matches(self, legit):
        net = _network(extra={"mycompany.com"}, override_default=True)
        assert is_host_blocked(legit, _privacy(), net) is True

    def test_wildcard_prefix_normalized(self):
        # `*.mycompany.com` should behave identically to `mycompany.com`.
        net = _network(extra={"*.mycompany.com"}, override_default=True)
        assert is_host_blocked("mycompany.com", _privacy(), net) is True
        assert is_host_blocked("api.mycompany.com", _privacy(), net) is True
        assert is_host_blocked("mycompany.com.evil.com", _privacy(), net) is False

    def test_case_insensitive_match(self):
        net = _network(extra={"mycompany.com"}, override_default=True)
        assert is_host_blocked("API.MyCompany.COM", _privacy(), net) is True


class TestBuildIgnoreHostsRegex:
    def test_default_blocklist_compiles(self):
        net = _network()
        patterns = build_ignore_hosts_regex(_privacy(), net)
        for p in patterns:
            re.compile(p, re.IGNORECASE)
        # No IP-literal anchors here -- mitmproxy matches ignore_hosts
        # against the resolved peername (always an IP), so an IPv4
        # anchor would tunnel every connection. IP-literal blocking
        # lives in the addon's is_host_blocked first-check.
        assert not any(r"\d+\.\d+\.\d+\.\d+" in p for p in patterns)
        assert not any("0-9a-f:" in p for p in patterns)

    def test_normal_host_does_not_match(self):
        """Regression: regex must NOT match a normal hostname like google.com.

        Combined with mitmproxy's behavior of also testing against the
        resolved peername, ANY pattern that matches an IPv4-shaped string
        causes universal passthrough. Test both the original hostname and
        a representative resolved IP.
        """
        net = _network()  # use the default blocklist
        patterns = build_ignore_hosts_regex(_privacy(), net)
        for p in patterns:
            assert not re.search(p, "google.com:443", re.IGNORECASE), (
                f"pattern unexpectedly matches google.com:443: {p}"
            )
            assert not re.search(p, "142.250.190.46:443", re.IGNORECASE), (
                f"pattern unexpectedly matches an IPv4 peername: {p} "
                f"(would cause universal passthrough)"
            )

    def test_chase_com_matches_with_subdomain(self):
        privacy = _privacy({"chase.com"})
        net = _network(override_default=True)
        patterns = build_ignore_hosts_regex(privacy, net)
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

        # Bare apex matches.
        assert any(r.match("Chase.com:443") for r in compiled)
        # Subdomain matches.
        assert any(r.match("api.chase.com:443") for r in compiled)
        # Non-443 ports also match (don't hardcode 443).
        assert any(r.match("api.chase.com:8443") for r in compiled)

    def test_unrelated_host_does_not_match(self):
        net = _network(extra={"mycompany.com"}, override_default=True)
        patterns = build_ignore_hosts_regex(_privacy(), net)
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        assert not any(r.match("github.com:443") for r in compiled)

    def test_override_default_drops_curated(self):
        net = _network(extra=set(), override_default=True)
        patterns = build_ignore_hosts_regex(_privacy(), net)
        # Curated entries should not appear as suffix anchors.
        assert not any("chase" in p for p in patterns)
        assert not any("stripe" in p for p in patterns)
        # And NO IP-literal anchors either (would universal-passthrough).
        assert not any(r"\d+\.\d+\.\d+\.\d+" in p for p in patterns)

    def test_default_blocklist_contents_cover_critical_entries(self):
        # Spot-check a representative subset; the full inventory is in
        # blocklist.DEFAULT_BLOCKLIST.
        critical = {
            "1password.com",
            "vault.bitwarden.com",
            "chase.com",
            "plaid.com",
            "stripe.com",
            "okta.com",
            "auth0.com",
            "accounts.google.com",
            "login.microsoftonline.com",
        }
        assert critical.issubset(DEFAULT_BLOCKLIST)
