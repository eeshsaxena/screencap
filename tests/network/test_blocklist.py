"""Tests for screencap.network.blocklist (V1 + V1.5)."""

from __future__ import annotations

import re

import pytest

from screencap.network.blocklist import (
    DEFAULT_BLOCKLIST,
    DEFAULT_CAPTURE_BODIES_FOR,
    REQUIRED_AUTH_IGNORE_HOSTS,
    build_ignore_hosts_regex,
    effective_capture_bodies_for,
    is_host_blocked,
    is_host_in_capture_bodies_for,
    is_ip_literal,
    missing_required_auth_hosts,
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
    capture: set[str] | None = None,
    override_capture: bool = False,
) -> NetworkConfig:
    return NetworkConfig(
        extra_blocklist=frozenset(h.lower() for h in (extra or set())),
        proxy_port=proxy_port,
        override_default_blocklist=override_default,
        body_size_cap=100_000,
        capture_bodies_for=frozenset(h.lower() for h in (capture or set())),
        override_default_capture_bodies_for=override_capture,
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


class TestEffectiveCaptureBodiesFor:
    def test_default_returns_default_set(self):
        assert effective_capture_bodies_for(_network()) == DEFAULT_CAPTURE_BODIES_FOR

    def test_user_extends_default(self):
        net = _network(capture={"mycompany.com"})
        result = effective_capture_bodies_for(net)
        assert "mycompany.com" in result
        assert "*.github.com" in result  # default still present

    def test_override_replaces_default(self):
        net = _network(capture={"mycompany.com"}, override_capture=True)
        assert effective_capture_bodies_for(net) == frozenset({"mycompany.com"})

    def test_override_with_empty_user_list_returns_empty(self):
        # The "metadata-only for all hosts" posture.
        net = _network(override_capture=True)
        assert effective_capture_bodies_for(net) == frozenset()


class TestIsHostInCaptureBodiesFor:
    def test_default_list_matches_subdomain(self):
        assert is_host_in_capture_bodies_for(
            "api.github.com", _privacy(), _network()
        ) is True

    def test_default_list_matches_apex(self):
        assert is_host_in_capture_bodies_for(
            "github.com", _privacy(), _network()
        ) is True

    def test_blocklist_always_wins_even_when_user_adds_to_capture(self):
        # User explicitly added chase.com to capture_bodies_for —
        # blocklist (mask_domains) still wins. This is the enforcement
        # test for "auth/banking/password-managers always win."
        net = _network(capture={"chase.com"})
        privacy = _privacy({"chase.com"})
        assert is_host_in_capture_bodies_for("api.chase.com", privacy, net) is False

    def test_blocklist_wins_via_default_blocklist(self):
        # bitwarden.com is in DEFAULT_BLOCKLIST. Even if user adds it to
        # capture_bodies_for, the default blocklist still wins.
        net = _network(capture={"vault.bitwarden.com"})
        assert is_host_in_capture_bodies_for(
            "vault.bitwarden.com", _privacy(), net
        ) is False

    def test_override_with_empty_list_blocks_all_capture(self):
        net = _network(override_capture=True)
        # github.com would normally be allowed by default — override
        # disables that.
        assert is_host_in_capture_bodies_for(
            "api.github.com", _privacy(), net
        ) is False

    def test_override_with_user_list_only(self):
        net = _network(capture={"mycompany.com"}, override_capture=True)
        assert is_host_in_capture_bodies_for(
            "api.mycompany.com", _privacy(), net
        ) is True
        # Default-list entry no longer matches under override.
        assert is_host_in_capture_bodies_for(
            "api.github.com", _privacy(), net
        ) is False

    def test_union_when_no_override(self):
        # Both user list AND default list are honored.
        net = _network(capture={"mycompany.com"})
        assert is_host_in_capture_bodies_for(
            "api.mycompany.com", _privacy(), net
        ) is True
        assert is_host_in_capture_bodies_for(
            "api.github.com", _privacy(), net
        ) is True

    def test_unrelated_host_not_captured(self):
        # mycompany.com is NOT in default list and not in user list.
        net = _network()
        assert is_host_in_capture_bodies_for(
            "api.mycompany.com", _privacy(), net
        ) is False

    def test_empty_host(self):
        # Don't crash on empty input.
        assert is_host_in_capture_bodies_for("", _privacy(), _network()) is False

    def test_ip_literal_not_captured(self):
        # IP literals are blocked (always tunneled), so body capture
        # is implicitly off.
        assert is_host_in_capture_bodies_for(
            "192.168.1.1", _privacy(), _network()
        ) is False

    # ---------------------------------------------------------------
    # Adversarial substring-bypass tests (mirror the blocklist guard).
    # ---------------------------------------------------------------

    @pytest.mark.parametrize("hostile", [
        "github.com.evil.com",
        "notgithub.com",
        "github.com.attacker.example",
        "something-github.com",
    ])
    def test_no_substring_bypass(self, hostile):
        net = _network()
        assert is_host_in_capture_bodies_for(hostile, _privacy(), net) is False

    def test_default_capture_bodies_for_contents_cover_expected_categories(self):
        # Spot-check that the curated default list covers each major
        # category. Full inventory in blocklist.DEFAULT_CAPTURE_BODIES_FOR.
        critical = {
            "*.github.com",
            "api.linear.app",
            "*.notion.so",
            "*.slack.com",
            "*.figma.com",
            "claude.ai",
            "*.anthropic.com",
            "chat.openai.com",
        }
        assert critical.issubset(DEFAULT_CAPTURE_BODIES_FOR)

    def test_default_lists_do_not_overlap(self):
        # Inclusion criteria #3: DEFAULT_CAPTURE_BODIES_FOR must NOT
        # overlap with DEFAULT_BLOCKLIST. Bare-form comparison handles
        # the *. wildcard prefix.
        capture_bare = {
            e[2:] if e.startswith("*.") else e for e in DEFAULT_CAPTURE_BODIES_FOR
        }
        block_bare = {
            e[2:] if e.startswith("*.") else e for e in DEFAULT_BLOCKLIST
        }
        assert capture_bare.isdisjoint(block_bare), (
            "Overlap between DEFAULT_BLOCKLIST and DEFAULT_CAPTURE_BODIES_FOR — "
            "see V1.5 ticket inclusion criteria #3."
        )


class TestRequiredAuthHosts:
    """The client's own auth/token hosts must NEVER be capturable, even when the
    user overrides the default blocklist (U5 self-capture hardening)."""

    _NEW_AUTH_HOSTS = [
        "oauth2.googleapis.com",
        "identitytoolkit.googleapis.com",
        "securetoken.googleapis.com",
    ]

    def test_accounts_google_already_present(self):
        assert "accounts.google.com" in REQUIRED_AUTH_IGNORE_HOSTS
        assert "accounts.google.com" in DEFAULT_BLOCKLIST

    @pytest.mark.parametrize("host", _NEW_AUTH_HOSTS)
    def test_auth_hosts_blocked_even_when_default_overridden(self, host):
        # override_default_blocklist drops DEFAULT_BLOCKLIST but must NOT drop the
        # auth hosts — capturing your own credentials is never permitted.
        net = _network(override_default=True)
        assert is_host_blocked(host, _privacy(), net) is True
        # subdomains too (suffix match)
        assert is_host_blocked(f"foo.{host}", _privacy(), net) is True

    def test_auth_hosts_in_ignore_regex_under_override(self):
        net = _network(override_default=True)
        patterns = build_ignore_hosts_regex(_privacy(), net)
        assert missing_required_auth_hosts(patterns) == set()
        # Each required host's :443 form matches some pattern.
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        for host in REQUIRED_AUTH_IGNORE_HOSTS:
            assert any(c.match(f"{host}:443") for c in compiled), host

    def test_missing_required_auth_hosts_detects_a_dropped_host(self):
        net = _network()
        patterns = build_ignore_hosts_regex(_privacy(), net)
        # Drop the securetoken pattern -> the fail-closed check must flag it.
        survivors = [p for p in patterns if "securetoken" not in p]
        missing = missing_required_auth_hosts(survivors)
        assert "securetoken.googleapis.com" in missing

    def test_normal_build_has_no_missing_auth_hosts(self):
        patterns = build_ignore_hosts_regex(_privacy(), _network())
        assert missing_required_auth_hosts(patterns) == set()
