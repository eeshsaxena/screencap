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
            # NOTE: real macOS `kSecAttrSynchronizableAny` + `kSecMatchLimitOne`
            # gives NO documented ordering — it returns an arbitrary single match.
            # This fake deterministically prefers the synced flavor only for test
            # stability; the ambiguity is real ONLY when both flavors hold
            # DIFFERENT bytes (the unsupported two-Mac-Stage-1 fork). After a
            # normal rewrite both flavors are byte-identical, so which one ANY
            # returns is immaterial.
            for flavor in (True, False):
                val = self.group.get((service, account, access_group, flavor))
                if val is not None:
                    return val
            return None
        return self.group.get((service, account, access_group, bool(synchronizable)))

    def store(self, service, account, secret, access_group, *, synchronizable=False):
        # Real store() is add-then-update-on-duplicate, per sync flavor.
        assert synchronizable is not kg.SYNCHRONIZABLE_ANY, "add/update cannot be ANY"
        self.group[(service, account, access_group, bool(synchronizable))] = secret

    def add_if_absent(self, service, account, secret, access_group, *, synchronizable=False):
        # Add-only: a same-flavor item is left UNTOUCHED (never overwritten).
        assert synchronizable is not kg.SYNCHRONIZABLE_ANY, "add cannot be ANY"
        slot = (service, account, access_group, bool(synchronizable))
        if slot in self.group:
            return False
        self.group[slot] = secret
        return True

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
    monkeypatch.setattr(kg, "add_if_absent", h.add_if_absent)
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
    monkeypatch.setattr(kg, "add_if_absent", _raise)
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
    monkeypatch.setattr(kg, "add_if_absent", lambda *a, **k: True)
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


def test_get_or_create_adds_synced_copy_and_keeps_device_local(homes: _FakeHomes) -> None:
    """get_or_create adds a synchronizable copy of a device-local (Stage-1) key so
    it reaches the user's other Macs — with the SAME bytes and key id — and
    **keeps** the device-local item as a durable on-device fallback (it never
    syncs, so it survives iCloud Keychain being turned off; it is byte-identical,
    so it cannot fork). No group item is ever deleted (KTD-5)."""
    homes.seed_group(KEY_G, synchronizable=False)
    got = cc.get_or_create_cloud_kek()
    assert got == KEY_G
    assert homes.group_synced() == _b64(KEY_G)  # now syncable
    assert cc.cloud_key_id(got) == cc.cloud_key_id(KEY_G)  # key id preserved
    assert homes.group_local() == _b64(KEY_G)  # device-local copy KEPT (durable)
    assert homes.group_deletes == []  # nothing deleted — ever


def test_rewrite_is_idempotent_once_synced(homes: _FakeHomes) -> None:
    """A second get_or_create after the key is already synced adds nothing further
    and deletes nothing — the sync-on step is one-time and add-only."""
    homes.seed_group(KEY_G, synchronizable=True)
    assert cc.get_or_create_cloud_kek() == KEY_G
    assert homes.group_synced() == _b64(KEY_G)
    assert homes.group_deletes == []  # nothing deleted


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


def test_rewrite_failopen_keeps_device_local_when_synced_add_fails(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If adding the synced copy fails, the device-local key stays fully usable —
    it is never deleted, so the failure is fail-open by construction."""
    homes.seed_group(KEY_G, synchronizable=False)

    def _boom(*_a, **_k):
        raise kg.KeychainError(-1, "SecItemAdd")

    monkeypatch.setattr(kg, "add_if_absent", _boom)  # sync-copy add fails
    got = cc.get_or_create_cloud_kek()
    assert got == KEY_G  # still serves from the device-local item
    assert homes.group_local() == _b64(KEY_G)  # NOT deleted
    assert homes.group_synced() is None  # add failed, no synced copy
    assert homes.group_deletes == []


def test_rewrite_never_overwrites_a_raced_in_synced_key(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DIFFERENT synced key that races in from another Mac between the sync-on
    check and the add is kept, never overwritten (add-only) — so it can never be
    clobbered fleet-wide."""
    homes.seed_group(KEY_G, synchronizable=False)  # this Mac's Stage-1 key
    real_add = homes.add_if_absent

    def racing_add(service, account, secret, access_group, *, synchronizable=False):
        # Mac B's key K_L syncs in just before our add fires.
        homes.group.setdefault(_gslot(True), _b64(KEY_L))
        return real_add(service, account, secret, access_group, synchronizable=synchronizable)

    monkeypatch.setattr(kg, "add_if_absent", racing_add)
    cc.get_or_create_cloud_kek()
    assert homes.group_synced() == _b64(KEY_L)  # the raced-in key is NOT overwritten
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


def _racy_add(homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch) -> None:
    """A synced KEY_G lands in the group exactly when ``add_if_absent`` fires (a
    concurrent writer, or a copy synced from another Mac in the TOCTOU window
    between the "absent?" read and the add). Add-only must keep it, never
    overwrite — so the caller ends up with KEY_G's bytes, not what it tried to
    write."""
    real_add = homes.add_if_absent

    def add_if_absent(service, account, secret, access_group, *, synchronizable=False):
        homes.group.setdefault(_gslot(True), _b64(KEY_G))  # concurrent arrival
        return real_add(service, account, secret, access_group, synchronizable=synchronizable)

    monkeypatch.setattr(kg, "add_if_absent", add_if_absent)


def test_concurrent_group_key_wins_over_fresh_mint(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    _racy_add(homes, monkeypatch)
    assert cc.get_or_create_cloud_kek() == KEY_G  # existing item's bytes win
    assert homes.group_synced() == _b64(KEY_G)  # never overwritten by the mint


def test_migration_never_overwrites_a_concurrent_group_key(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    _racy_add(homes, monkeypatch)
    assert cc.get_cloud_kek() == KEY_G  # the group key wins over the legacy one
    assert homes.group_synced() == _b64(KEY_G)  # not overwritten by the legacy key
    # The legacy item stays — its ciphertext may still need it.
    assert homes.legacy[_lkey()] == _b64(KEY_L)
    assert homes.legacy_deletes == 0


# --------------------------------------------------------------------------
# Deletion-path audit (KTD-5): no code path deletes a SYNCED KEK item
# --------------------------------------------------------------------------


def test_no_entry_point_ever_deletes_a_group_kek_item(homes: _FakeHomes) -> None:
    """Exercise every cloud-KEK entry point and assert NO group item is ever
    deleted — deleting a synchronizable item propagates fleet-wide (KTD-5), and
    the sync-on step keeps the device-local item too, so the safe state is zero
    group-KEK deletes on any path."""
    # 1. Fresh mint.
    cc.get_or_create_cloud_kek()
    # 2. Sync-on of a device-local item.
    homes.group.clear()
    homes.seed_group(KEY_G, synchronizable=False)
    cc.get_or_create_cloud_kek()
    # 3. Read paths.
    cc.get_cloud_kek()
    cc.resolve_cloud_key()
    # 4. Legacy migration (deletes the LEGACY keyring item, never a group item).
    homes.group.clear()
    homes.legacy[_lkey()] = _b64(KEY_L)
    cc.get_cloud_kek()

    assert homes.group_deletes == [], f"a group KEK item was deleted: {homes.group_deletes}"


def test_source_never_deletes_a_synced_kek_tree_wide() -> None:
    """Static guard across the WHOLE src tree (not just cloud_crypto): no
    ``keychain_group.delete`` / ``kg.delete`` call anywhere passes
    ``synchronizable=True`` or ``SYNCHRONIZABLE_ANY``. Deleting a synced item
    propagates to all the user's Macs = fleet-wide data loss (KTD-5); the auth
    token and BYO-segmentation keys are also device-local, so this invariant is
    tree-wide, and a future synced delete added in any module trips this test."""
    import re
    from glob import glob

    src_root = Path(cc.__file__).resolve().parents[1]  # src/screencap/
    delete_call = re.compile(r"(?:keychain_group|kg)\.delete\((.*?)\)", re.DOTALL)
    for path in glob(str(src_root / "**" / "*.py"), recursive=True):
        text = Path(path).read_text()
        for m in delete_call.finditer(text):
            body = m.group(1)
            assert "synchronizable=True" not in body, f"synced delete in {path}: {body!r}"
            assert "SYNCHRONIZABLE_ANY" not in body, f"ANY delete in {path}: {body!r}"


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
