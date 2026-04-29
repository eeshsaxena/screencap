"""Tests for screencap.network.redaction (V1)."""

from __future__ import annotations

import pytest

from screencap.network.redaction import (
    AUTH_HEADER_NAMES,
    SENSITIVE_HEADER_NAMES,
    SENSITIVE_QUERY_PARAM_NAMES,
    is_auth_header,
    is_sensitive_header,
    redact_headers,
    redact_url_query,
)


class TestIsAuthHeader:
    @pytest.mark.parametrize("name", [
        "Authorization",
        "authorization",
        "AUTHORIZATION",
        "Cookie",
        "Set-Cookie",
        "Proxy-Authorization",
        "X-Hub-Signature",
        "X-Hub-Signature-256",
        "X-Webhook-Secret",
        "X-AccessKey",
    ])
    def test_exact_names_case_insensitive(self, name):
        assert is_auth_header(name) is True

    @pytest.mark.parametrize("name", [
        "X-API-Key",
        "x-api-key",
        "CF-API-Key",
        "session-token",
        "X-Custom-Token",
        "X-Service-Secret",
        "auth-secret",
    ])
    def test_glob_patterns(self, name):
        assert is_auth_header(name) is True

    @pytest.mark.parametrize("name", [
        "Content-Type",
        "Accept",
        "Host",
        "User-Agent",
        "X-Custom-Header",
    ])
    def test_unrelated_headers(self, name):
        assert is_auth_header(name) is False


class TestIsSensitiveHeader:
    @pytest.mark.parametrize("name", [
        "Referer",
        "referer",
        "REFERER",
        "Origin",
        "X-CSRF-Token",
        "X-XSRF-Token",
        "X-Auth-Token",  # also matches *-token in is_auth_header
        "X-Forwarded-For",
        "X-Real-IP",
        "True-Client-IP",
    ])
    def test_sensitive_names(self, name):
        # We only test the predicate's own match here. Note that
        # X-Auth-Token also satisfies is_auth_header (auth wins in
        # redact_headers).
        assert is_sensitive_header(name) is True

    def test_not_sensitive(self):
        assert is_sensitive_header("Content-Type") is False
        assert is_sensitive_header("Accept-Language") is False


class TestRedactHeaders:
    def test_basic_authorization_redacted(self):
        out = redact_headers([
            ("Authorization", "Bearer abc"),
            ("X-Custom", "ok"),
        ])
        assert out == [
            ("Authorization", "[REDACTED:auth-header]"),
            ("X-Custom", "ok"),
        ]

    def test_case_insensitive_match(self):
        out = redact_headers([
            ("authorization", "x"),
            ("AUTHORIZATION", "y"),
        ])
        assert all(v == "[REDACTED:auth-header]" for _, v in out)
        # Names are preserved verbatim.
        assert [n for n, _ in out] == ["authorization", "AUTHORIZATION"]

    def test_glob_patterns_match(self):
        out = redact_headers([
            ("X-API-Key", "secret"),
            ("api-key", "secret"),
            ("session-token", "x"),
        ])
        for _, v in out:
            assert v == "[REDACTED:auth-header]"

    def test_auth_and_sensitive_tags(self):
        out = redact_headers([
            ("Cookie", "x=y"),
            ("Set-Cookie", "z=w"),
            ("Referer", "https://example.com"),
            ("X-Forwarded-For", "1.2.3.4"),
        ])
        assert out == [
            ("Cookie", "[REDACTED:auth-header]"),
            ("Set-Cookie", "[REDACTED:auth-header]"),
            ("Referer", "[REDACTED:sensitive-header]"),
            ("X-Forwarded-For", "[REDACTED:sensitive-header]"),
        ]

    def test_multiple_set_cookie_redacted(self):
        out = redact_headers([
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
            ("Set-Cookie", "c=3"),
        ])
        # All three preserved as separate entries, all redacted.
        assert len(out) == 3
        assert all(v == "[REDACTED:auth-header]" for _, v in out)
        assert all(n == "Set-Cookie" for n, _ in out)

    def test_unrelated_headers_pass_through(self):
        out = redact_headers([
            ("Content-Type", "application/json"),
            ("Accept", "*/*"),
        ])
        assert out == [
            ("Content-Type", "application/json"),
            ("Accept", "*/*"),
        ]

    def test_empty_input(self):
        assert redact_headers([]) == []

    def test_returns_new_list(self):
        original = [("Authorization", "Bearer x")]
        out = redact_headers(original)
        # Same shape, different identity, original is not mutated.
        assert out is not original
        assert original == [("Authorization", "Bearer x")]

    def test_auth_takes_precedence_over_sensitive(self):
        # X-Auth-Token matches both is_auth_header (suffix *-token) and
        # is_sensitive_header (exact name). Auth tag wins.
        out = redact_headers([("X-Auth-Token", "x")])
        assert out == [("X-Auth-Token", "[REDACTED:auth-header]")]


class TestRedactUrlQuery:
    def test_basic_token_redacted(self):
        out = redact_url_query("https://api.example.com/?token=abc&keep=ok")
        assert out == (
            "https://api.example.com/?token=[REDACTED:query-param]&keep=ok"
        )

    def test_no_query_unchanged(self):
        url = "https://api.example.com/path"
        assert redact_url_query(url) == url

    def test_empty_value(self):
        # ?token= (empty value) → still redacted.
        out = redact_url_query("https://api.example.com/?token=&keep=ok")
        assert out == (
            "https://api.example.com/?token=[REDACTED:query-param]&keep=ok"
        )

    def test_duplicate_keys_preserved(self):
        out = redact_url_query(
            "https://api.example.com/?token=a&token=b&keep=ok"
        )
        # Both occurrences are redacted; ordering preserved.
        assert out == (
            "https://api.example.com/"
            "?token=[REDACTED:query-param]"
            "&token=[REDACTED:query-param]"
            "&keep=ok"
        )

    def test_case_insensitive_match(self):
        out = redact_url_query("https://api.example.com/?Token=abc")
        # Name is preserved as-is, value redacted.
        assert "Token=[REDACTED:query-param]" in out

    def test_multiple_sensitive_params(self):
        out = redact_url_query(
            "https://api.example.com/?access_token=a&id_token=b&keep=ok"
        )
        assert "access_token=[REDACTED:query-param]" in out
        assert "id_token=[REDACTED:query-param]" in out
        assert "keep=ok" in out

    def test_aws_signature_redacted(self):
        out = redact_url_query(
            "https://s3.example.com/key?X-Amz-Signature=xxx&X-Amz-Date=20260101"
        )
        assert "X-Amz-Signature=[REDACTED:query-param]" in out
        assert "X-Amz-Date=20260101" in out

    def test_csrf_redacted(self):
        out = redact_url_query("https://example.com/?_csrf=xxx")
        assert "_csrf=[REDACTED:query-param]" in out

    def test_unrelated_query_unchanged(self):
        url = "https://api.example.com/?page=1&size=20"
        assert redact_url_query(url) == url

    def test_fragment_preserved(self):
        out = redact_url_query(
            "https://example.com/path?token=abc#section"
        )
        assert out.endswith("#section")
        assert "token=[REDACTED:query-param]" in out


class TestDenylistConstants:
    def test_auth_header_names_lowercase(self):
        for name in AUTH_HEADER_NAMES:
            assert name == name.lower(), f"{name!r} not lowercased"

    def test_sensitive_header_names_lowercase(self):
        for name in SENSITIVE_HEADER_NAMES:
            assert name == name.lower(), f"{name!r} not lowercased"

    def test_sensitive_query_param_names_lowercase(self):
        for name in SENSITIVE_QUERY_PARAM_NAMES:
            assert name == name.lower(), f"{name!r} not lowercased"

    def test_critical_auth_entries_present(self):
        # Spot-check a few load-bearing entries.
        for name in ["authorization", "cookie", "set-cookie",
                     "proxy-authorization", "x-webhook-secret"]:
            assert name in AUTH_HEADER_NAMES

    def test_critical_query_params_present(self):
        for name in ["access_token", "id_token", "refresh_token",
                     "client_secret", "password", "code"]:
            assert name in SENSITIVE_QUERY_PARAM_NAMES
