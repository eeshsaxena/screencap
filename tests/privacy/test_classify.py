"""Tests for the shared context classifier (SCR-33 U4).

The pure, DB-free classifier slice split out of the old
``screencap.privacy.context`` module now lives in
``screencap.privacy.classify``. These assertions are the classifier half of
the former ``tests/privacy/test_context.py`` characterization baseline.
"""

from __future__ import annotations

import pytest

from screencap.privacy.classify import (
    BROWSER_BUNDLE_IDS,
    BUNDLE_ID_MAP,
    DefaultContextClassifier,
    _classify_title,
)
from screencap.privacy.policy import ContextClass, FrameMetadata

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# _classify_title — title heuristic border symbol (direct coverage)
# ---------------------------------------------------------------------------


class TestClassifyTitle:
    def test_matches_title_heuristic_and_returns_pattern(self):
        result = _classify_title("My Inbox")
        assert result is not None
        ctx_class, pattern = result
        assert ctx_class == ContextClass.EMAIL
        assert "inbox" in pattern.lower()

    def test_returns_none_for_unmatched_title(self):
        assert _classify_title("just some random window") is None


# ---------------------------------------------------------------------------
# DefaultContextClassifier — classification priority chain
# ---------------------------------------------------------------------------


class TestDefaultContextClassifier:
    def setup_method(self):
        self.classifier = DefaultContextClassifier()

    # Path 1: known app bundle ID
    def test_known_bundle_id_classifies_directly(self):
        meta = FrameMetadata(bundle_id="com.apple.mail", window_title="Inbox")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "bundle_id"

    # Path 1 > Path 2: bundle ID takes priority over browser+domain
    def test_known_bundle_id_beats_browser_domain(self):
        meta = FrameMetadata(
            bundle_id="com.apple.mail",
            domain="mail.google.com",
        )
        result = self.classifier.classify(meta)
        assert result.confidence == "bundle_id"

    # Path 2a: browser with verified domain
    def test_browser_with_known_domain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="mail.google.com",
            window_title="Inbox - Gmail",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "domain"

    # Path 2a: subdomain matching (parent-domain fallback)
    def test_browser_subdomain_matches_parent_domain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="workspace.mail.google.com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "domain"

    # Path 2b: browser + unknown domain falls to title heuristic
    def test_browser_unknown_domain_falls_to_title(self):
        meta = FrameMetadata(
            bundle_id="com.apple.Safari",
            domain="random-site.com",
            window_title="My Inbox - Some Mail",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "title"

    # Path 2c: browser without domain data → browser_unverified
    def test_browser_without_domain_is_unverified(self):
        meta = FrameMetadata(bundle_id="com.google.Chrome")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # Path 3: unknown app with title heuristic
    def test_unknown_app_uses_title_heuristic(self):
        meta = FrameMetadata(
            bundle_id="com.unknown.app",
            window_title="Bank of America - Account",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING
        assert result.confidence == "title"

    # Path 4: no signal → explicit unknown
    def test_no_signal_produces_explicit_unknown(self):
        result = self.classifier.classify(FrameMetadata())
        assert result.context_class == ContextClass.UNKNOWN
        assert result.evidence == "no_matching_signal"

    # Data integrity: expanded bundle ID map spot checks
    @pytest.mark.parametrize("bundle_id, expected", [
        ("org.keepassxc.keepassxc", ContextClass.PASSWORD_MANAGER),
        ("org.whispersystems.signal-desktop", ContextClass.CHAT),
        ("io.alacritty", ContextClass.CODE_EDITOR_TERMINAL),
        ("com.mitchellh.ghostty", ContextClass.CODE_EDITOR_TERMINAL),
        ("com.apple.Passwords", ContextClass.PASSWORD_MANAGER),
        ("com.apple.FaceTime", ContextClass.VIDEO_CALL),
        ("org.mozilla.thunderbird", ContextClass.EMAIL),
        ("com.tableplus.TablePlus", ContextClass.ADMIN_CONSOLE),
    ])
    def test_expanded_bundle_id_map(self, bundle_id, expected):
        meta = FrameMetadata(bundle_id=bundle_id)
        result = self.classifier.classify(meta)
        assert result.context_class == expected

    # Data integrity: bundle ID map and browser IDs must be disjoint
    def test_bundle_id_map_disjoint_from_browser_ids(self):
        overlap = set(BUNDLE_ID_MAP.keys()) & BROWSER_BUNDLE_IDS
        assert overlap == set(), (
            f"Bundle IDs in both BUNDLE_ID_MAP and BROWSER_BUNDLE_IDS would "
            f"never reach the browser path: {overlap}"
        )


# ---------------------------------------------------------------------------
# Domain classification via loaded index (UT1 + supplement)
# ---------------------------------------------------------------------------


class TestDomainClassification:
    """Test domain lookup using the loaded domain index."""

    def setup_method(self):
        self.classifier = DefaultContextClassifier()

    @pytest.mark.parametrize("domain, expected", [
        ("chase.com", ContextClass.BANKING),              # UT1
        ("vault.bitwarden.com", ContextClass.PASSWORD_MANAGER),  # supplement
        ("drive.google.com", ContextClass.CLOUD_STORAGE),        # supplement
        ("outlook.live.com", ContextClass.EMAIL),                # supplement gap-fill
    ])
    def test_known_domains(self, domain, expected):
        meta = FrameMetadata(bundle_id="com.google.Chrome", domain=domain)
        result = self.classifier.classify(meta)
        assert result.context_class == expected
        assert result.confidence == "domain"

    def test_parent_domain_expansion(self):
        """Child domain should inherit parent's classification."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="online.banking.chase.com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING

    def test_trailing_dot_normalized(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="chase.com.",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING

    def test_case_insensitive_domain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="Chase.Com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING

    def test_unknown_domain_falls_through(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="docs.example.com",
        )
        result = self.classifier.classify(meta)
        # Unknown domain, no title → browser_unverified
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    def test_shared_host_not_over_classified(self):
        """google.com must not inherit EMAIL from mail.google.com."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="www.google.com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED


# ---------------------------------------------------------------------------
# Keyword detection (auth/payment flows)
# ---------------------------------------------------------------------------


class TestKeywordDetection:
    """Test URL path and subdomain keyword matching."""

    def setup_method(self):
        self.classifier = DefaultContextClassifier()

    # Auth keywords in URL path — representative subset
    @pytest.mark.parametrize("path, expected", [
        ("/login", ContextClass.AUTH_FLOW),           # simple token
        ("/sign-in", ContextClass.AUTH_FLOW),         # hyphenated, full-segment match
        ("/forgot-password", ContextClass.AUTH_FLOW), # multi-hyphen, full-segment match
        ("/oauth", ContextClass.AUTH_FLOW),           # protocol-specific keyword
    ])
    def test_auth_keywords_in_path(self, path, expected):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url=f"https://example.com{path}",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == expected

    # Payment keywords in URL path — representative subset
    @pytest.mark.parametrize("path", [
        "/checkout", "/pay",
    ])
    def test_payment_keywords_in_path(self, path):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url=f"https://example.com{path}",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.PAYMENT_FLOW

    # Auth keywords in subdomain
    def test_auth_keyword_in_subdomain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="login.example.com",
            browser_url="https://login.example.com/",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    def test_auth_subdomain_with_co_uk_tld(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="auth.payments.example.co.uk",
            browser_url="https://auth.payments.example.co.uk/",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    # First-segment-only matching
    def test_first_segment_only(self):
        """Only the first path segment triggers keyword matching."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/blog/login",
        )
        result = self.classifier.classify(meta)
        # First segment is "blog", not "login"
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # No substring matching
    def test_no_substring_matching(self):
        """Token 'blogindex' should NOT match 'login'."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/blogindex",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # URL decoding
    def test_url_decoded_path(self):
        """URL-encoded path segments should be decoded before matching."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/%6Cogin",  # 'login' encoded
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    # Segment tokenization
    def test_segment_tokenization(self):
        """Hyphenated segments should be tokenized: login-callback → ['login', 'callback']."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/login-callback",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    # Combined domain + keyword (stricter wins)
    def test_domain_plus_keyword_stricter_wins(self):
        """chase.com/login: BANKING + AUTH_FLOW → stricter action wins."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="chase.com",
            browser_url="https://chase.com/login",
        )
        result = self.classifier.classify(meta)
        # AUTH_FLOW is EXCLUDE in public, BANKING is also EXCLUDE in public.
        # In internal mode: AUTH_FLOW is MASK_WINDOW, BANKING is MASK_WINDOW.
        # Either way, stricter wins. The combined confidence should show both.
        assert result.confidence == "domain+keyword"

    def test_github_login_picks_auth_flow(self):
        """github.com/login: CODE_EDITOR_TERMINAL (TEXT_REDACT in public) vs AUTH_FLOW (EXCLUDE) → AUTH_FLOW wins."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="github.com",
            browser_url="https://github.com/login",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    # SPA hash-routing detection
    def test_hash_routing_auth(self):
        """SPA hash route /#/login should be detected as AUTH_FLOW."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/#/login",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    def test_hash_routing_payment(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/#/checkout",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.PAYMENT_FLOW

    def test_hash_routing_under_subpath(self):
        """SPA hash route under a subpath: /app#/login should detect auth."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/app#/login",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    def test_path_keyword_beats_fragment(self):
        """Path keyword should win even when fragment also has a route."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/checkout#/success",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.PAYMENT_FLOW

    def test_regular_fragment_no_false_positive(self):
        """Normal anchor fragments (page#section) should not trigger keywords."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/docs#login",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # No keyword match for regular paths
    def test_no_match_for_regular_path(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/about",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # Without browser_url, only subdomain keywords work
    def test_subdomain_keyword_without_browser_url(self):
        """If no browser_url, subdomain labels still checked via domain field."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="login.example.com",
        )
        result = self.classifier.classify(meta)
        # Without browser_url, path is empty but subdomain "login" matches
        assert result.context_class == ContextClass.AUTH_FLOW
