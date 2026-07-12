"""Cloud-KEK custody: shared access group first (KTD-1) with the legacy-``keyring``
fallback for un-entitled binaries, the one-time write-verify-then-delete legacy
migration (KTD-2, U1), and the Stage-2 iCloud-Keychain sync flip (KTD-5, U7).

Security-critical: unlike the re-mintable auth token, a lost or forked KEK is
permanently undecryptable ciphertext — so these tests pin that an existing group
item always wins (never overwritten), that reads find the key regardless of its
sync flavor (so a pre-Stage-2 device-local item is never mistaken for absent →
a forking second mint), that the sync-on rewrite only ever deletes the OLD
device-local item (a delete that never propagates), and that the migration only
ever uses the non-interactive legacy read (never prompts).
"""

from __future__ import annotations

import base64
import logging
import sys
from pathlib import Path

import keyring
import pytest

from screencap import cloud_crypto as cc
from screencap import keychain_group as kg

pytestmark = pytest.mark.privacy

KEY_G = b"G" * 32  # a key living in the shared group
KEY_L = b"L" * 32  # a key living in the legacy keyring home


def _b64(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def _gslot(synchronizable: bool) -> tuple[str, str, str, bool]:
    return (cc.CLOUD_SERVICE, cc.CLOUD_KEK_ACCOUNT, cc.KEYCHAIN_ACCESS_GROUP, synchronizable)


def _lkey() -> tuple[str, str]:
    return (cc.CLOUD_SERVICE, cc.CLOUD_KEK_ACCOUNT)


class _FakeHomes:
    """Both KEK homes with the group modeled as macOS actually treats it: a
    synchronizable and a non-synchronizable item are DISTINCT (the group dict is
    keyed by ``(service, account, access_group, synchronizable)``). Reads honor
    ``SYNCHRONIZABLE_ANY`` (match either, preferring the synced flavor — the
    post-migration home). The legacy keyring/login item backs both the ``keyring``
    surface and the non-interactive legacy SecItem read, so one dict serves both.
    """

    def __init__(self) -> None:
        self.group: dict[tuple[str, str, str, bool], str] = {}
        self.legacy: dict[tuple[str, str], str] = {}
        self.legacy_deletes = 0
        # Every group delete, as (service, account, access_group, synchronizable) —
        # the deletion-path audit asserts a synced item is never among them.
        self.group_deletes: list[tuple[str, str, str, bool]] = []

    # -- test seeding helpers --
    def seed_group(self, key: bytes, *, synchronizable: bool) -> None:
        self.group[_gslot(synchronizable)] = _b64(key)

    def group_synced(self) -> str | None:
        return self.group.get(_gslot(True))

    def group_local(self) -> str | None:
        return self.group.get(_gslot(False))

    # -- keychain_group public surface (entitled path) --
    def load(self, service, account, access_group, *, synchronizable=False):
        if synchronizable is kg.SYNCHRONIZABLE_ANY:
            for flavor in (True, False):  # prefer the synced (post-migration) home
                val = self.group.get((service, account, access_group, flavor))
                if val is not None:
                    return val
            return None
        return self.group.get((service, account, access_group, bool(synchronizable)))

    def store(self, service, account, secret, access_group, *, synchronizable=False):
        # Real store() is add-then-update-on-duplicate, per sync flavor.
        assert synchronizable is not kg.SYNCHRONIZABLE_ANY, "add/update cannot be ANY"
        self.group[(service, account, access_group, bool(synchronizable))] = secret

    def delete(self, service, account, access_group, *, synchronizable=False):
        # ANY (or True) deletes would risk destroying a synced item fleet-wide
        # (KTD-5) — the audit forbids them, so the fake refuses ANY outright and
        # records the flavor of every delete for the behavioral pin.
        assert synchronizable is not kg.SYNCHRONIZABLE_ANY, "delete must never match ANY"
        self.group_deletes.append((service, account, access_group, bool(synchronizable)))
        self.group.pop((service, account, access_group, bool(synchronizable)), None)

    def load_legacy_noninteractive(self, service, account):
        return self.legacy.get((service, account))

    def delete_legacy_noninteractive(self, service, account) -> bool:
        self.legacy_deletes += 1
        self.legacy.pop((service, account), None)
        return True


@pytest.fixture
def homes(monkeypatch: pytest.MonkeyPatch) -> _FakeHomes:
    h = _FakeHomes()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(kg, "load", h.load)
    monkeypatch.setattr(kg, "store", h.store)
    monkeypatch.setattr(kg, "delete", h.delete)
    monkeypatch.setattr(kg, "load_legacy_noninteractive", h.load_legacy_noninteractive)
    monkeypatch.setattr(kg, "delete_legacy_noninteractive", h.delete_legacy_noninteractive)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: h.legacy.get((s, a)))
    monkeypatch.setattr(
        keyring, "set_password", lambda s, a, v: h.legacy.__setitem__((s, a), v)
    )
    return h


@pytest.fixture
def unentitled(homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch) -> _FakeHomes:
    """The pip/pyenv/Debug fork: every group operation raises MissingEntitlement."""

    def _raise(*_a: object, **_k: object):
        raise kg.MissingEntitlement(kg.errSecMissingEntitlement, "test")

    monkeypatch.setattr(kg, "load", _raise)
    monkeypatch.setattr(kg, "store", _raise)
    monkeypatch.setattr(kg, "delete", _raise)
    return homes


# --------------------------------------------------------------------------
# Group-first read (KTD-1)
# --------------------------------------------------------------------------


def test_group_first_read_wins_when_both_homes_hold_a_key(homes: _FakeHomes) -> None:
    homes.seed_group(KEY_G, synchronizable=True)
    homes.legacy[_lkey()] = _b64(KEY_L)
    assert cc.get_cloud_kek() == KEY_G
    # No migration ran: the legacy item is untouched.
    assert homes.legacy[_lkey()] == _b64(KEY_L)
    assert homes.legacy_deletes == 0


def test_group_absent_everywhere_returns_none(homes: _FakeHomes) -> None:
    assert cc.get_cloud_kek() is None
    assert homes.group == {}


def test_create_lands_in_group_synced_not_keyring(homes: _FakeHomes) -> None:
    created = cc.get_or_create_cloud_kek()
    # U7: a freshly minted key is written synchronizable so it reaches the user's
    # other Macs; nothing lands in the legacy keyring home.
    assert homes.group_synced() == _b64(created)
    assert homes.group_local() is None
    assert homes.legacy == {}
    assert cc.get_cloud_kek() == created  # not re-minted


# --------------------------------------------------------------------------
# MissingEntitlement → legacy keyring fallback for read AND write
# --------------------------------------------------------------------------


def test_missing_entitlement_falls_back_to_keyring_read(unentitled: _FakeHomes) -> None:
    unentitled.legacy[_lkey()] = _b64(KEY_L)
    assert cc.get_cloud_kek() == KEY_L


def test_missing_entitlement_falls_back_to_keyring_write(unentitled: _FakeHomes) -> None:
    created = cc.get_or_create_cloud_kek()
    assert unentitled.legacy[_lkey()] == _b64(created)
    assert unentitled.group == {}


def test_unentitled_never_attempts_a_synchronizable_write(unentitled: _FakeHomes) -> None:
    """The fallback (un-entitled) home has no iCloud sync — get_or_create with a
    legacy key present must not touch the group at all (no sync store/delete)."""
    unentitled.legacy[_lkey()] = _b64(KEY_L)
    assert cc.get_or_create_cloud_kek() == KEY_L
    assert unentitled.group == {}
    assert unentitled.group_deletes == []


# --------------------------------------------------------------------------
# One-time legacy migration (KTD-2) — now lands the key synchronizable (U7)
# --------------------------------------------------------------------------


def test_migration_copies_verifies_then_deletes_legacy(homes: _FakeHomes) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    assert cc.get_cloud_kek() == KEY_L
    assert homes.group_synced() == _b64(KEY_L)  # copied into the group, synced (U7)
    assert _lkey() not in homes.legacy  # legacy removed after verified readback
    assert homes.legacy_deletes == 1


def test_migration_aborts_with_legacy_intact_when_readback_fails(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    # The group write is silently lost → the readback cannot verify the copy.
    monkeypatch.setattr(kg, "store", lambda *a, **k: None)
    assert cc.get_cloud_kek() == KEY_L  # fail-open: legacy key still serves
    assert homes.legacy[_lkey()] == _b64(KEY_L)  # legacy NOT deleted
    assert homes.legacy_deletes == 0


def test_migration_never_prompts_only_noninteractive_legacy_read(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)

    def _interactive(*_a: object) -> str:
        raise AssertionError("interactive legacy keyring read during migration")

    monkeypatch.setattr(keyring, "get_password", _interactive)
    # Succeeds via load_legacy_noninteractive alone — the promptable path is dead.
    assert cc.get_cloud_kek() == KEY_L
    assert homes.group_synced() == _b64(KEY_L)


# --------------------------------------------------------------------------
# Stage 2 (U7): reads find the device-local key; the sync-on rewrite
# --------------------------------------------------------------------------


def test_get_cloud_kek_finds_stage1_device_local_item(homes: _FakeHomes) -> None:
    """A Stage-1 beta key lives in the group as ``synchronizable=false``. The
    read must find it (SYNCHRONIZABLE_ANY) — never treat it as absent, which
    would let get_or_create mint a forking second key."""
    homes.seed_group(KEY_G, synchronizable=False)
    assert cc.get_cloud_kek() == KEY_G


def test_no_second_key_minted_when_device_local_key_exists(homes: _FakeHomes) -> None:
    """The load-bearing anti-fork invariant: get_or_create over a pre-Stage-2
    device-local key returns THAT key's bytes, never a fresh mint."""
    homes.seed_group(KEY_G, synchronizable=False)
    got = cc.get_or_create_cloud_kek()
    assert got == KEY_G  # same bytes — no fresh random key


def test_get_or_create_rewrites_device_local_to_synced(homes: _FakeHomes) -> None:
    """get_or_create rewrites a device-local (Stage-1) item to synchronizable so
    it reaches the user's other Macs: the synced item holds the same bytes and
    key id, and the OLD device-local item is deleted — a delete that never
    propagates (it never synced), unlike a synced item (KTD-5)."""
    homes.seed_group(KEY_G, synchronizable=False)
    got = cc.get_or_create_cloud_kek()
    assert got == KEY_G
    assert homes.group_synced() == _b64(KEY_G)  # now syncable
    assert cc.cloud_key_id(got) == cc.cloud_key_id(KEY_G)  # key id preserved
    assert homes.group_local() is None  # device-local copy dropped
    # The only delete was the non-synchronizable one.
    assert homes.group_deletes == [_gslot(False)]


def test_rewrite_is_idempotent_once_synced(homes: _FakeHomes) -> None:
    """A second get_or_create after the key is already synced does no further
    store or delete — the rewrite is one-time."""
    homes.seed_group(KEY_G, synchronizable=True)
    before_deletes = list(homes.group_deletes)
    assert cc.get_or_create_cloud_kek() == KEY_G
    assert homes.group_synced() == _b64(KEY_G)
    assert homes.group_local() is None
    assert homes.group_deletes == before_deletes  # nothing deleted


def test_rewrite_prefers_existing_synced_item_never_overwrites(
    homes: _FakeHomes,
) -> None:
    """If a synced item already exists (e.g. a copy that arrived from another Mac
    that migrated first), the rewrite keeps it and never overwrites it, even when
    a stale device-local item with different bytes is also present."""
    homes.seed_group(KEY_G, synchronizable=True)
    homes.seed_group(KEY_L, synchronizable=False)  # a stale device-local remnant
    got = cc.get_or_create_cloud_kek()
    assert got == KEY_G  # the synced item wins (SYNCHRONIZABLE_ANY prefers it)
    assert homes.group_synced() == _b64(KEY_G)  # untouched
    assert homes.group_deletes == []  # never deletes when already synced


def test_rewrite_failopen_keeps_device_local_when_verify_fails(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the synced-copy write can't be confirmed by readback, the rewrite must
    NOT delete the device-local item — the key stays usable locally (fail-open)."""
    homes.seed_group(KEY_G, synchronizable=False)
    monkeypatch.setattr(kg, "store", lambda *a, **k: None)  # sync write silently lost
    got = cc.get_or_create_cloud_kek()
    assert got == KEY_G  # still serves from the device-local item
    assert homes.group_local() == _b64(KEY_G)  # NOT deleted
    assert homes.group_deletes == []


def test_migrated_legacy_key_is_synced_and_syncs_only_once(homes: _FakeHomes) -> None:
    """A pre-Stage-1 keyring key that migrates on read lands synchronizable, and
    a subsequent get_or_create finds it already synced (no re-store, no delete)."""
    homes.legacy[_lkey()] = _b64(KEY_L)
    assert cc.get_cloud_kek() == KEY_L
    assert homes.group_synced() == _b64(KEY_L)
    homes.group_deletes.clear()
    assert cc.get_or_create_cloud_kek() == KEY_L
    assert homes.group_deletes == []  # already synced → idempotent


# --------------------------------------------------------------------------
# Prefer-existing: an already-present group KEK is never overwritten (KTD-2)
# --------------------------------------------------------------------------


def _racy_load(homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch) -> list:
    """Group empty on the first read, concurrently populated with a synced KEY_G
    after; records every group store attempt (there must be none)."""
    calls = {"n": 0}

    def load(service, account, access_group, **_k: object):
        calls["n"] += 1
        if calls["n"] > 1:
            homes.group.setdefault(_gslot(True), _b64(KEY_G))
        return homes.load(service, account, access_group, **_k)

    stores: list = []
    monkeypatch.setattr(kg, "load", load)
    monkeypatch.setattr(kg, "store", lambda *a, **k: stores.append(a))
    return stores


def test_concurrent_group_key_wins_over_fresh_mint(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = _racy_load(homes, monkeypatch)
    assert cc.get_or_create_cloud_kek() == KEY_G  # existing item's bytes win
    assert stores == []  # never overwritten
    assert homes.group_synced() == _b64(KEY_G)


def test_migration_never_overwrites_a_concurrent_group_key(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    stores = _racy_load(homes, monkeypatch)
    assert cc.get_cloud_kek() == KEY_G  # the group key wins over the legacy one
    assert stores == []
    # The legacy item stays — its ciphertext may still need it.
    assert homes.legacy[_lkey()] == _b64(KEY_L)
    assert homes.legacy_deletes == 0


# --------------------------------------------------------------------------
# Deletion-path audit (KTD-5): no code path deletes a SYNCED KEK item
# --------------------------------------------------------------------------


def test_no_entry_point_ever_deletes_a_synchronizable_kek(homes: _FakeHomes) -> None:
    """Exercise every cloud-KEK entry point and assert no delete ever targets a
    synchronizable (True) or ANY item — such a delete propagates to all the
    user's Macs = fleet-wide data loss. Only the device-local (False) rewrite
    cleanup may delete."""
    # 1. Fresh mint.
    cc.get_or_create_cloud_kek()
    # 2. Rewrite of a device-local item.
    homes.group.clear()
    homes.seed_group(KEY_G, synchronizable=False)
    cc.get_or_create_cloud_kek()
    # 3. Read paths.
    cc.get_cloud_kek()
    cc.resolve_cloud_key()
    # 4. Legacy migration.
    homes.group.clear()
    homes.legacy[_lkey()] = _b64(KEY_L)
    cc.get_cloud_kek()

    assert all(sync is False for (_s, _a, _g, sync) in homes.group_deletes), (
        f"a synced KEK item was deleted: {homes.group_deletes}"
    )


def test_source_never_deletes_a_synced_cloud_kek() -> None:
    """Static guard: every ``keychain_group.delete(`` call in cloud_crypto.py
    passes ``synchronizable=False`` (or the default) — never ``True`` /
    ``SYNCHRONIZABLE_ANY``. Pins the KTD-5 delete-propagation hazard against
    future edits that add a stray synced delete."""
    import re

    src = Path(cc.__file__).read_text()
    # Match keychain_group.delete(...) call bodies (single- or multi-line).
    for m in re.finditer(r"keychain_group\.delete\((.*?)\)", src, re.DOTALL):
        body = m.group(1)
        assert "synchronizable=True" not in body, f"synced delete: {body!r}"
        assert "SYNCHRONIZABLE_ANY" not in body, f"ANY delete: {body!r}"


# --------------------------------------------------------------------------
# key_id home-independence + no key material in logs
# --------------------------------------------------------------------------


def test_cloud_key_id_stable_across_homes(homes: _FakeHomes) -> None:
    homes.seed_group(KEY_G, synchronizable=True)
    from_group = cc.get_cloud_kek()
    homes.group.clear()
    homes.legacy[_lkey()] = _b64(KEY_G)  # same key, legacy home (migrates on read)
    from_legacy = cc.get_cloud_kek()
    assert from_group == from_legacy
    assert cc.cloud_key_id(from_group) == cc.cloud_key_id(from_legacy)


def test_migration_logs_never_contain_key_material(
    homes: _FakeHomes, caplog: pytest.LogCaptureFixture
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    with caplog.at_level(logging.DEBUG):
        assert cc.get_cloud_kek() == KEY_L
    assert _b64(KEY_L) not in caplog.text
    assert repr(KEY_L) not in caplog.text
