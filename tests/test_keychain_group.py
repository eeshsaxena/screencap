"""Unit tests for the SCR-241 access-group Keychain primitive.

The `ctypes`→Security.framework marshaling lives in private ``_sec_item_*``
primitives; these tests stub those primitives with an in-memory fake so the
**policy / error-routing** (the spike-verified OSStatus mapping) is what gets
pinned. The real marshaling is covered by the macOS-gated round-trip at the
bottom and the signed-build smoke check.
"""

from __future__ import annotations

import sys

import pytest

from screencap import keychain_group as kg

pytestmark = pytest.mark.privacy

GROUP = "2A8S6MV8DZ.com.screencap.shared"
SERVICE = "screencap-auth"
ACCOUNT = "default"


class _FakeGroupKeychain:
    """In-memory stand-in for the data-protection-keychain access-group store.

    Items are keyed by ``synchronizable`` too, mirroring macOS: the synced and
    non-synced items are distinct, and a query only matches its own flavor.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str, bool], bytes] = {}

    def add(
        self, service: str, account: str, secret: bytes, access_group: str, *, synchronizable: bool = False
    ) -> int:
        key = (service, account, access_group, synchronizable)
        if key in self.items:
            return kg.errSecDuplicateItem
        self.items[key] = secret
        return kg.errSecSuccess

    def update(
        self, service: str, account: str, secret: bytes, access_group: str, *, synchronizable: bool = False
    ) -> int:
        key = (service, account, access_group, synchronizable)
        if key not in self.items:
            return kg.errSecItemNotFound
        self.items[key] = secret
        return kg.errSecSuccess

    def copy(
        self, service: str, account: str, access_group: str, *, synchronizable: bool = False
    ) -> tuple[int, bytes | None]:
        key = (service, account, access_group, synchronizable)
        if key in self.items:
            return kg.errSecSuccess, self.items[key]
        return kg.errSecItemNotFound, None

    def delete(
        self, service: str, account: str, access_group: str, *, synchronizable: bool = False
    ) -> int:
        key = (service, account, access_group, synchronizable)
        if key in self.items:
            del self.items[key]
            return kg.errSecSuccess
        return kg.errSecItemNotFound


@pytest.fixture
def fake_group(monkeypatch: pytest.MonkeyPatch) -> _FakeGroupKeychain:
    fake = _FakeGroupKeychain()
    monkeypatch.setattr(kg, "_sec_item_add", fake.add)
    monkeypatch.setattr(kg, "_sec_item_update", fake.update)
    monkeypatch.setattr(kg, "_sec_item_copy_matching", fake.copy)
    monkeypatch.setattr(kg, "_sec_item_delete", fake.delete)
    return fake


# --- happy path -----------------------------------------------------------


def test_store_then_load_roundtrips(fake_group: _FakeGroupKeychain) -> None:
    kg.store(SERVICE, ACCOUNT, "refresh-token-abc", GROUP)
    assert kg.load(SERVICE, ACCOUNT, GROUP) == "refresh-token-abc"


def test_store_existing_updates_not_errors(fake_group: _FakeGroupKeychain) -> None:
    kg.store(SERVICE, ACCOUNT, "first", GROUP)
    kg.store(SERVICE, ACCOUNT, "second", GROUP)  # errSecDuplicateItem → SecItemUpdate
    assert kg.load(SERVICE, ACCOUNT, GROUP) == "second"
    assert len(fake_group.items) == 1


def test_store_duplicate_then_update_fails_surfaces_update_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # add → errSecDuplicateItem → update fails: the UPDATE's status must surface,
    # not the swallowed duplicate.
    monkeypatch.setattr(kg, "_sec_item_add", lambda *a, **k: kg.errSecDuplicateItem)
    monkeypatch.setattr(kg, "_sec_item_update", lambda *a, **k: -25291)  # errSecNotAvailable
    with pytest.raises(kg.KeychainError) as exc:
        kg.store(SERVICE, ACCOUNT, "x", GROUP)
    assert exc.value.status == -25291


def test_store_duplicate_then_update_missing_entitlement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kg, "_sec_item_add", lambda *a, **k: kg.errSecDuplicateItem)
    monkeypatch.setattr(kg, "_sec_item_update", lambda *a, **k: kg.errSecMissingEntitlement)
    with pytest.raises(kg.MissingEntitlement):
        kg.store(SERVICE, ACCOUNT, "x", GROUP)


def test_non_ascii_secret_roundtrips(fake_group: _FakeGroupKeychain) -> None:
    secret = "réfrèsh-tökèn-🔑-Ω"
    kg.store(SERVICE, ACCOUNT, secret, GROUP)
    assert kg.load(SERVICE, ACCOUNT, GROUP) == secret


# --- not-found / delete ---------------------------------------------------


def test_load_missing_returns_none(fake_group: _FakeGroupKeychain) -> None:
    assert kg.load(SERVICE, ACCOUNT, GROUP) is None


def test_delete_missing_is_noop(fake_group: _FakeGroupKeychain) -> None:
    kg.delete(SERVICE, ACCOUNT, GROUP)  # must not raise


def test_delete_removes_item(fake_group: _FakeGroupKeychain) -> None:
    kg.store(SERVICE, ACCOUNT, "x", GROUP)
    kg.delete(SERVICE, ACCOUNT, GROUP)
    assert kg.load(SERVICE, ACCOUNT, GROUP) is None


# --- error routing (the spike-verified fallback contract) -----------------


def test_missing_entitlement_raises_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kg, "_sec_item_add", lambda *a, **k: kg.errSecMissingEntitlement)
    with pytest.raises(kg.MissingEntitlement) as exc:
        kg.store(SERVICE, ACCOUNT, "x", GROUP)
    assert exc.value.status == kg.errSecMissingEntitlement


def test_missing_entitlement_on_load_raises_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kg, "_sec_item_copy_matching", lambda *a, **k: (kg.errSecMissingEntitlement, None))
    with pytest.raises(kg.MissingEntitlement):
        kg.load(SERVICE, ACCOUNT, GROUP)


def test_unexpected_status_raises_keychainerror_not_missing_entitlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # -25291 errSecNotAvailable is NOT the un-entitled trigger → must surface as a
    # real error, never be masked as un-entitled (KTD-4).
    monkeypatch.setattr(kg, "_sec_item_add", lambda *a, **k: -25291)
    with pytest.raises(kg.KeychainError) as exc:
        kg.store(SERVICE, ACCOUNT, "x", GROUP)
    assert exc.value.status == -25291
    assert not isinstance(exc.value, kg.MissingEntitlement)


# --- legacy non-interactive read (migration; never prompts) ---------------


def test_legacy_read_success_returns_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg, "_sec_item_copy_legacy_noninteractive", lambda *a: (kg.errSecSuccess, b"legacy-token")
    )
    assert kg.load_legacy_noninteractive(SERVICE, ACCOUNT) == "legacy-token"


@pytest.mark.parametrize(
    "status",
    [
        kg.errSecAuthFailed,  # SecKeychainSetUserInteractionAllowed(false) path
        kg.errSecInteractionNotAllowed,
        kg.errSecUserCanceled,  # kSecUseAuthenticationUIFail path
        kg.errSecItemNotFound,
    ],
)
def test_legacy_read_not_readable_returns_none(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setattr(kg, "_sec_item_copy_legacy_noninteractive", lambda *a: (status, None))
    assert kg.load_legacy_noninteractive(SERVICE, ACCOUNT) is None


def test_legacy_read_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object) -> tuple[int, bytes | None]:
        raise OSError("keychain exploded")

    monkeypatch.setattr(kg, "_sec_item_copy_legacy_noninteractive", _boom)
    assert kg.load_legacy_noninteractive(SERVICE, ACCOUNT) is None


@pytest.mark.parametrize(
    ("status", "gone"),
    [
        (kg.errSecSuccess, True),
        (kg.errSecItemNotFound, True),  # already absent counts as gone
        (kg.errSecAuthFailed, False),  # couldn't remove → still present
    ],
)
def test_delete_legacy_maps_status_to_gone(monkeypatch: pytest.MonkeyPatch, status: int, gone: bool) -> None:
    monkeypatch.setattr(kg, "_sec_item_delete_legacy_noninteractive", lambda *a: status)
    assert kg.delete_legacy_noninteractive(SERVICE, ACCOUNT) is gone


def test_delete_legacy_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object) -> int:
        raise OSError("keychain exploded")

    monkeypatch.setattr(kg, "_sec_item_delete_legacy_noninteractive", _boom)
    assert kg.delete_legacy_noninteractive(SERVICE, ACCOUNT) is False


# --- synchronizable threading (SCR-220 U1) ---------------------------------


def test_synchronizable_reaches_all_four_primitives(monkeypatch: pytest.MonkeyPatch) -> None:
    # The flag must ride the add *attributes* AND every query — macOS queries
    # default to matching only non-synchronizable items, so a primitive that
    # drops it makes a synced item invisible to that operation.
    seen: dict[str, bool] = {}

    def add(service, account, secret, access_group, *, synchronizable=False):
        seen["add"] = synchronizable
        return kg.errSecDuplicateItem  # force the update leg too

    def update(service, account, secret, access_group, *, synchronizable=False):
        seen["update"] = synchronizable
        return kg.errSecSuccess

    def copy(service, account, access_group, *, synchronizable=False):
        seen["copy"] = synchronizable
        return kg.errSecItemNotFound, None

    def delete(service, account, access_group, *, synchronizable=False):
        seen["delete"] = synchronizable
        return kg.errSecSuccess

    monkeypatch.setattr(kg, "_sec_item_add", add)
    monkeypatch.setattr(kg, "_sec_item_update", update)
    monkeypatch.setattr(kg, "_sec_item_copy_matching", copy)
    monkeypatch.setattr(kg, "_sec_item_delete", delete)

    kg.store(SERVICE, ACCOUNT, "x", GROUP, synchronizable=True)
    kg.load(SERVICE, ACCOUNT, GROUP, synchronizable=True)
    kg.delete(SERVICE, ACCOUNT, GROUP, synchronizable=True)
    assert seen == {"add": True, "update": True, "copy": True, "delete": True}

    seen.clear()
    kg.store(SERVICE, ACCOUNT, "x", GROUP)  # default stays device-local
    kg.load(SERVICE, ACCOUNT, GROUP)
    kg.delete(SERVICE, ACCOUNT, GROUP)
    assert seen == {"add": False, "update": False, "copy": False, "delete": False}


def test_synced_and_unsynced_items_are_distinct(fake_group: _FakeGroupKeychain) -> None:
    kg.store(SERVICE, ACCOUNT, "synced", GROUP, synchronizable=True)
    assert kg.load(SERVICE, ACCOUNT, GROUP) is None  # default query can't see it
    assert kg.load(SERVICE, ACCOUNT, GROUP, synchronizable=True) == "synced"


# --- macOS-gated real-keychain integration --------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="Security.framework is macOS-only")
def test_real_keychain_graceful_on_unentitled_host() -> None:
    """On an un-entitled test runner, store must fail with MissingEntitlement
    (spike-verified -34018), not crash. On an entitled host it round-trips."""
    svc = "screencap-auth-itest"
    try:
        kg.store(svc, ACCOUNT, "itest-secret", GROUP)
    except kg.MissingEntitlement:
        return  # expected on an un-entitled runner — the fallback contract holds
    # Entitled host: full round-trip + cleanup.
    try:
        assert kg.load(svc, ACCOUNT, GROUP) == "itest-secret"
    finally:
        kg.delete(svc, ACCOUNT, GROUP)
