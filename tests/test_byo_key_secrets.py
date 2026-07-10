"""Tests for U2: per-vendor BYO API-key storage + validation
(``screencap.segmentation.secrets``).

The macOS Keychain (access-group primitives + the legacy ``keyring`` fallback)
and the HTTP validation client are both mocked, so these run fully offline and
Vision-free. Covers:
  - store → load → delete round-trip per vendor (entitled access-group path);
  - the keyring-fallback path when the access group is unavailable;
  - ``has_key`` presence-only read (never the value);
  - validation maps 200 → valid, 401 → invalid, network error → unknown;
  - the ``*-cli`` / unknown ids have no key store (UnknownVendor).
"""

from __future__ import annotations

import types

import pytest

from screencap.segmentation import secrets as byo


# --------------------------------------------------------------------------
# Fixtures — mirror tests/test_auth.py's fake_group / fake_keyring shape.
# --------------------------------------------------------------------------


@pytest.fixture
def fake_group(monkeypatch):
    """The **entitled** context: the access-group backend succeeds (in-memory)
    and ``keyring`` is a spy so tests can assert it is NOT used on the group path.

    Forces ``sys.platform == 'darwin'`` so the wrappers take the access-group
    branch regardless of the host running the test.
    """
    import keyring

    from screencap import keychain_group

    monkeypatch.setattr(byo.sys, "platform", "darwin")
    group: dict = {}
    keyring_calls: list = []

    monkeypatch.setattr(
        keychain_group, "store", lambda s, ac, sec, g: group.__setitem__((s, ac, g), sec)
    )
    monkeypatch.setattr(keychain_group, "load", lambda s, ac, g: group.get((s, ac, g)))
    monkeypatch.setattr(keychain_group, "delete", lambda s, ac, g: group.pop((s, ac, g), None))

    monkeypatch.setattr(keyring, "set_password", lambda s, ac, pw: keyring_calls.append(("set", s, ac)))
    monkeypatch.setattr(keyring, "get_password", lambda s, ac: keyring_calls.append(("get", s, ac)) or None)
    monkeypatch.setattr(keyring, "delete_password", lambda s, ac: keyring_calls.append(("del", s, ac)))
    return types.SimpleNamespace(group=group, keyring_calls=keyring_calls)


@pytest.fixture
def fake_keyring(monkeypatch):
    """The **un-entitled** context: the access-group backend raises
    ``MissingEntitlement`` so every wrapper falls back to this in-memory
    ``keyring`` stand-in deterministically."""
    import keyring
    import keyring.errors

    from screencap import keychain_group

    monkeypatch.setattr(byo.sys, "platform", "darwin")
    store: dict = {}

    monkeypatch.setattr(keyring, "set_password", lambda s, ac, pw: store.__setitem__((s, ac), pw))
    monkeypatch.setattr(keyring, "get_password", lambda s, ac: store.get((s, ac)))

    def delp(s, ac):
        if (s, ac) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(s, ac)]

    monkeypatch.setattr(keyring, "delete_password", delp)

    def _missing(*_a, **_k):
        raise keychain_group.MissingEntitlement(keychain_group.errSecMissingEntitlement, "test")

    monkeypatch.setattr(keychain_group, "store", _missing)
    monkeypatch.setattr(keychain_group, "load", _missing)
    monkeypatch.setattr(keychain_group, "delete", _missing)
    return store


ALL_KEY_VENDORS = ("openai", "anthropic", "gemini")


# --------------------------------------------------------------------------
# Store → load → delete round-trip (entitled access-group path)
# --------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize("vendor", ALL_KEY_VENDORS)
    def test_store_load_delete(self, vendor, fake_group):
        assert byo.load_key(vendor) is None  # nothing stored yet
        byo.store_key(vendor, f"sk-{vendor}-secret")
        assert byo.load_key(vendor) == f"sk-{vendor}-secret"
        assert byo.has_key(vendor) is True

        byo.delete_key(vendor)
        assert byo.load_key(vendor) is None
        assert byo.has_key(vendor) is False

    def test_per_vendor_service_isolation(self, fake_group):
        # Each vendor lands under its own service name — no cross-talk.
        byo.store_key("openai", "sk-openai")
        byo.store_key("anthropic", "sk-anthropic")
        assert byo.load_key("openai") == "sk-openai"
        assert byo.load_key("anthropic") == "sk-anthropic"
        assert byo.load_key("gemini") is None
        # The three distinct service names are what's in the group.
        services = {k[0] for k in fake_group.group}
        assert services == {"screencap-openai", "screencap-anthropic"}

    def test_group_store_load_do_not_touch_keyring(self, fake_group):
        byo.store_key("openai", "sk-openai")
        byo.load_key("openai")
        # store/load on the entitled path never fall through to legacy keyring.
        assert fake_group.keyring_calls == []

    def test_group_delete_also_clears_legacy_keyring(self, fake_group):
        # delete_key mirrors auth._delete_refresh_token: it ALSO clears any legacy
        # keyring item (belt-and-braces) even on the entitled path, so a
        # pre-fallback key can never resurrect. That one keyring 'del' is expected.
        byo.store_key("openai", "sk-openai")
        byo.delete_key("openai")
        assert fake_group.keyring_calls == [("del", "screencap-openai", "default")]


# --------------------------------------------------------------------------
# Keyring fallback (access group unavailable / non-entitled dev build)
# --------------------------------------------------------------------------


class TestKeyringFallback:
    @pytest.mark.parametrize("vendor", ALL_KEY_VENDORS)
    def test_fallback_round_trip(self, vendor, fake_keyring):
        byo.store_key(vendor, f"sk-{vendor}")
        assert byo.load_key(vendor) == f"sk-{vendor}"
        assert byo.has_key(vendor) is True
        # It landed in the keyring stand-in under the per-vendor service.
        service = byo._VENDOR_SERVICE[vendor]
        assert fake_keyring[(service, byo.KEYCHAIN_ACCOUNT)] == f"sk-{vendor}"

        byo.delete_key(vendor)
        assert byo.load_key(vendor) is None

    def test_delete_missing_is_noop(self, fake_keyring):
        # Deleting an absent key must not raise (PasswordDeleteError swallowed).
        byo.delete_key("openai")
        assert byo.load_key("openai") is None


# --------------------------------------------------------------------------
# Unknown / CLI-delegation ids hold no key
# --------------------------------------------------------------------------


class TestUnknownVendor:
    @pytest.mark.parametrize("vendor", ("openai-cli", "anthropic-cli", "gemini-cli", "bogus"))
    def test_no_store_for_non_key_vendor(self, vendor, fake_group):
        with pytest.raises(byo.UnknownVendor):
            byo.store_key(vendor, "should-not-store")
        with pytest.raises(byo.UnknownVendor):
            byo.load_key(vendor)
        with pytest.raises(byo.UnknownVendor):
            byo.delete_key(vendor)

    def test_validate_unknown_vendor_raises(self):
        with pytest.raises(byo.UnknownVendor):
            byo.validate_key("openai-cli", "sk-x")


# --------------------------------------------------------------------------
# Validation — 200 → valid, 401 → invalid, network error → unknown
# --------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class TestValidation:
    @pytest.mark.parametrize("vendor", ALL_KEY_VENDORS)
    def test_200_is_valid(self, vendor, monkeypatch):
        import requests

        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200))
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(200))
        assert byo.validate_key(vendor, "sk-good") == byo.VALID

    @pytest.mark.parametrize("vendor", ALL_KEY_VENDORS)
    @pytest.mark.parametrize("status", (401, 403))
    def test_401_403_is_invalid(self, vendor, status, monkeypatch):
        import requests

        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(status))
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(status))
        assert byo.validate_key(vendor, "sk-bad") == byo.INVALID

    @pytest.mark.parametrize("vendor", ALL_KEY_VENDORS)
    def test_network_error_is_unknown(self, vendor, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("offline")

        monkeypatch.setattr(requests, "get", boom)
        monkeypatch.setattr(requests, "post", boom)
        assert byo.validate_key(vendor, "sk-x") == byo.UNKNOWN

    def test_5xx_and_429_are_unknown(self, monkeypatch):
        import requests

        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(500))
        assert byo.validate_key("openai", "sk-x") == byo.UNKNOWN
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(429))
        assert byo.validate_key("openai", "sk-x") == byo.UNKNOWN

    def test_validation_pins_hardcoded_https_hosts(self, vendor="openai", monkeypatch=None):
        # Guard: the validation URLs are hardcoded HTTPS vendor hosts — no override.
        assert byo._OPENAI_MODELS_URL == "https://api.openai.com/v1/models"
        assert byo._ANTHROPIC_COUNT_TOKENS_URL == "https://api.anthropic.com/v1/messages/count_tokens"
        assert byo._GEMINI_MODELS_URL == "https://generativelanguage.googleapis.com/v1beta/models"
        for url in (
            byo._OPENAI_MODELS_URL,
            byo._ANTHROPIC_COUNT_TOKENS_URL,
            byo._GEMINI_MODELS_URL,
        ):
            assert url.startswith("https://")

    def test_validate_sends_key_to_pinned_host_only(self, monkeypatch):
        # The key travels in the auth header to exactly the pinned URL, verified TLS.
        seen = {}
        import requests

        def cap_get(url, headers=None, timeout=None, **k):
            seen["url"] = url
            seen["headers"] = headers
            return _FakeResp(200)

        monkeypatch.setattr(requests, "get", cap_get)
        byo.validate_key("openai", "sk-live")
        assert seen["url"] == byo._OPENAI_MODELS_URL
        assert seen["headers"]["Authorization"] == "Bearer sk-live"
