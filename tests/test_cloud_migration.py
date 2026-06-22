"""Tests for the cloud-namespace migration scripts (SCR-139 / plan U8–U9).

The migration core (``scripts/cloud_migration/core.py``) is intentionally
``google.cloud``-free, so these run fully offline against a small in-memory GCS
fake — the same posture ``scripts/cloud-function/conftest.py`` gives the function
suite. The fake mirrors enough of the real surface (``list_blobs`` filtered by
prefix, per-blob ``crc32c``/``content_type``/``md5_hash``, a multi-call
``rewrite()`` with rewriteToken, ``get_iam_policy``/``set_iam_policy``, ``delete``,
and the reload-gated ``iam_configuration``/``default_object_acl`` the UBLA pre-check
reads) that a handler copying the wrong prefix, skipping verification, truncating a
large object, or staging into a publicly-ACL'd bucket is provably caught — a
prefix-only mock would miss it.

``scripts/`` is put on ``sys.path`` (mirroring ``tests/test_auth.py`` loading
``scripts/generate_provisioned.py``) so the package + thin CLI shims import.

Run with::

    PYTHONPATH=src python -m pytest tests/test_cloud_migration.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from cloud_migration import core  # noqa: E402
from cloud_migration.core import MigrationError, RecordingState  # noqa: E402

# --------------------------------------------------------------------------
# In-memory GCS fake
# --------------------------------------------------------------------------


class FakeStore:
    """Backing state shared by the fake client / bucket / blob references."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.iam_bindings: list[dict] = []
        # Bucket public-access posture (SCR-146 UBLA / default-object-ACL gate).
        # Default to the correctly-configured posture (UBLA on + PAP enforced) so
        # the gate is a no-op for tests that don't exercise it — the production
        # rule is "don't false-refuse a correctly-configured bucket".
        self.ubla_enabled: bool = True
        self.public_access_prevention: str | None = "enforced"
        # Default object ACL entries: [{"entity": "allUsers", "role": "READER"}, ...].
        self.default_object_acl: list[dict] = []
        # When set, FakeBucket.reload() / get_iam_policy() raise these — model a live
        # bucket read failing (bucket gone / permission denied) so the gate aborts.
        self.reload_error: Exception | None = None
        self.iam_policy_error: Exception | None = None
        # src name -> number of rewrite() calls needed (default 1).
        self.rewrite_plan: dict[str, int] = {}
        # src names whose copy lands with a corrupted crc32c (verify should fail).
        self.corrupt_on_rewrite: set[str] = set()
        # src names whose rewrite() never clears its token — exercises copy_blob's
        # iteration / wall-clock loop bound (SCR-145).
        self.rewrite_never_completes: set[str] = set()
        # error injection (SCR-145): name -> exception instance to raise. Additive
        # seam so a transient GCS error / RetryError on a single object is provable
        # without the SDK. Keyed by the SOURCE blob name for rewrite, by the fetched
        # name for get_blob, and by the blob name for delete.
        self.raise_on_rewrite: dict[str, BaseException] = {}
        self.raise_on_get_blob: dict[str, BaseException] = {}
        self.raise_on_delete: dict[str, BaseException] = {}
        # observability
        self.rewrite_log: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        # (op_name, timeout) recorded per per-object call so a test can assert every
        # GCS call carries an explicit timeout (SCR-145).
        self.seen_timeouts: list[tuple[str, object]] = []

    def add(self, name, *, crc32c=None, content_type="application/octet-stream",
            size=10, md5_hash=None, generation=1):
        self.objects[name] = {
            "crc32c": crc32c if crc32c is not None else f"crc-{name}",
            "content_type": content_type,
            "size": size,
            "md5_hash": md5_hash,
            "generation": generation,
        }


class FakeBlob:
    def __init__(self, store: FakeStore, name: str):
        self._store = store
        self.name = name
        self._load()

    def _load(self):
        rec = self._store.objects.get(self.name)
        if rec is not None:
            self.crc32c = rec["crc32c"]
            self.content_type = rec["content_type"]
            self.size = rec["size"]
            self.md5_hash = rec.get("md5_hash")
            self.generation = rec.get("generation", 1)
        else:
            self.crc32c = self.content_type = self.size = self.md5_hash = None
            self.generation = None

    def exists(self):
        return self.name in self._store.objects

    def reload(self, timeout=None):
        self._store.seen_timeouts.append(("reload", timeout))
        self._load()

    def delete(self, if_generation_match=None, timeout=None):
        """Honor an ``if_generation_match`` precondition like google's Blob.delete:
        raise PreconditionFailed (HTTP 412) when it does not match the CURRENT live
        generation — modeling a racing overwrite between enumeration and delete."""
        self._store.seen_timeouts.append(("delete", timeout))
        exc = self._store.raise_on_delete.get(self.name)
        if exc is not None:
            raise exc
        rec = self._store.objects.get(self.name)
        if if_generation_match is not None and rec is not None:
            live_gen = rec.get("generation", 1)
            if live_gen != if_generation_match:
                from google.api_core.exceptions import PreconditionFailed

                raise PreconditionFailed(
                    f"generation mismatch (expected {if_generation_match}, live {live_gen})"
                )
        self._store.objects.pop(self.name, None)
        self._store.deleted.append(self.name)

    def rewrite(self, source, token=None, timeout=None):
        """Multi-call rewrite: returns a non-None token until the final call, which
        copies the source record (optionally corrupting crc32c)."""
        self._store.seen_timeouts.append(("rewrite", timeout))
        exc = self._store.raise_on_rewrite.get(source.name)
        if exc is not None:
            raise exc
        if source.name in self._store.rewrite_never_completes:
            return ("stuck", 10, 100)  # token never clears → copy_blob must bound the loop
        self._store.rewrite_log.append((source.name, self.name))
        need = self._store.rewrite_plan.get(source.name, 1)
        done = (0 if token is None else int(token)) + 1
        if done < need:
            return (str(done), 10 * done, 10 * need)
        rec = dict(self._store.objects[source.name])
        if source.name in self._store.corrupt_on_rewrite:
            rec["crc32c"] = "CORRUPT"
        self._store.objects[self.name] = rec
        self._load()
        return (None, 10 * need, 10 * need)


class FakePolicy:
    def __init__(self, bindings):
        self.bindings = bindings


class FakeIAMConfiguration:
    """Mirrors google's ``bucket.iam_configuration`` surface the gate reads."""

    def __init__(self, *, uniform_bucket_level_access_enabled, public_access_prevention):
        self.uniform_bucket_level_access_enabled = uniform_bucket_level_access_enabled
        self.public_access_prevention = public_access_prevention


class FakeACL:
    """Mirrors google's ACL: ``reload()`` fetches live entries, then iterate
    ``{"entity": ..., "role": ...}`` dicts (one per entity/role)."""

    def __init__(self, store: FakeStore):
        self._store = store
        self._entries: list[dict] = []

    def reload(self, *args, **kwargs):
        self._entries = [dict(e) for e in self._store.default_object_acl]

    def __iter__(self):
        return iter(self._entries)


class FakeBucket:
    def __init__(self, store: FakeStore, name: str):
        self._store = store
        self.name = name
        self._default_object_acl = FakeACL(store)
        # Populated by reload() from the live store, mirroring google's Bucket
        # whose iam_configuration reads cached _properties (empty until reloaded).
        self._reloaded = False

    def blob(self, name):
        return FakeBlob(self._store, name)

    def get_blob(self, name, timeout=None):
        """Like google's Bucket.get_blob: one GET → populated blob, or None."""
        self._store.seen_timeouts.append(("get_blob", timeout))
        exc = self._store.raise_on_get_blob.get(name)
        if exc is not None:
            raise exc
        return FakeBlob(self._store, name) if name in self._store.objects else None

    def reload(self, *args, **kwargs):
        if self._store.reload_error is not None:
            raise self._store.reload_error
        self._reloaded = True

    @property
    def iam_configuration(self):
        # google's iam_configuration reads the bucket's cached _properties, which a
        # bare client.bucket(name) ref does NOT have until reload(); model that so
        # the production code's reload-before-read is load-bearing in tests.
        if not self._reloaded:
            return FakeIAMConfiguration(
                uniform_bucket_level_access_enabled=False, public_access_prevention=None
            )
        return FakeIAMConfiguration(
            uniform_bucket_level_access_enabled=self._store.ubla_enabled,
            public_access_prevention=self._store.public_access_prevention,
        )

    @property
    def default_object_acl(self):
        return self._default_object_acl

    def get_iam_policy(self, requested_policy_version=None):
        if self._store.iam_policy_error is not None:
            raise self._store.iam_policy_error
        # Preserve every binding field (role, members, condition, ...) so the
        # production code's condition-preservation is observable in tests.
        return FakePolicy([dict(b, members=set(b["members"])) for b in self._store.iam_bindings])

    def set_iam_policy(self, policy):
        self._store.iam_bindings = [
            dict(b, members=set(b.get("members", []))) for b in policy.bindings
        ]


class FakeClient:
    def __init__(self, store: FakeStore):
        self._store = store

    def list_blobs(self, bucket_name, prefix=None, timeout=None):
        return [
            FakeBlob(self._store, n)
            for n in sorted(self._store.objects)
            if prefix is None or n.startswith(prefix)
        ]

    def bucket(self, name):
        return FakeBucket(self._store, name)


BUCKET = "screencap-recordings"
IDLE = lambda: RecordingState.IDLE  # noqa: E731


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def client(store):
    return FakeClient(store)


def _seed_two_recordings(store):
    """Two flat recordings with a nested screenshot + a session blob (excluded)."""
    store.add("recordings/alpha/manifest.json", content_type="application/json")
    store.add("recordings/alpha/video.mp4", content_type="video/mp4", size=5000)
    store.add("recordings/alpha/screenshots/0.png", content_type="image/png")
    store.add("recordings/beta/manifest.json", content_type="application/json")
    store.add("sessions/_index.json", content_type="application/json")


# --------------------------------------------------------------------------
# Key mapping + verification units
# --------------------------------------------------------------------------


def test_map_key_preserves_full_nested_suffix():
    assert (
        core.map_key("recordings/alpha/screenshots/0.png", "recordings/", "import-review/")
        == "import-review/alpha/screenshots/0.png"
    )
    # A bare folder-placeholder equal to the prefix maps to nothing.
    assert core.map_key("recordings/", "recordings/", "import-review/") is None
    assert core.map_key("sessions/x", "recordings/", "import-review/") is None


def test_verify_match_catches_crc_content_type_md5(store, client):
    bucket = client.bucket(BUCKET)
    store.add("a", crc32c="X", content_type="image/png", md5_hash="m1")
    store.add("b", crc32c="X", content_type="image/png", md5_hash="m1")
    assert core.verify_match(bucket.blob("a"), bucket.blob("b")) is None

    store.add("c", crc32c="Y", content_type="image/png")
    assert "crc32c" in core.verify_match(bucket.blob("a"), bucket.blob("c"))

    # Same bytes (crc32c), drifted content_type — count/size alone would pass.
    store.add("d", crc32c="X", content_type="application/octet-stream", md5_hash="m1")
    assert "content_type" in core.verify_match(bucket.blob("a"), bucket.blob("d"))

    store.add("e", crc32c="X", content_type="image/png", md5_hash="m2")
    assert "md5_hash" in core.verify_match(bucket.blob("a"), bucket.blob("e"))

    # Missing destination — get_blob returns None for an absent object.
    assert core.verify_match(bucket.blob("a"), bucket.get_blob("missing")) == "destination missing"


def test_verify_match_fails_closed_on_missing_crc32c(store, client):
    # A composite object can expose crc32c=None. None == None must NOT read as
    # verified — that would authorize an irreversible delete on no evidence.
    bucket = client.bucket(BUCKET)
    # Set directly so crc32c stays None (store.add() substitutes a default for None).
    store.objects["nocrc-src"] = {"crc32c": None, "content_type": "video/mp4", "size": 10, "md5_hash": None}
    store.objects["nocrc-dst"] = {"crc32c": None, "content_type": "video/mp4", "size": 10, "md5_hash": None}
    assert "crc32c unavailable" in core.verify_match(bucket.blob("nocrc-src"), bucket.blob("nocrc-dst"))


def test_verify_match_catches_size_mismatch(store, client):
    # Same crc32c (32-bit, collidable) but different size → not verified.
    bucket = client.bucket(BUCKET)
    store.add("s1", crc32c="C", content_type="video/mp4", size=10)
    store.add("s2", crc32c="C", content_type="video/mp4", size=11)
    assert "size mismatch" in core.verify_match(bucket.blob("s1"), bucket.blob("s2"))


# --------------------------------------------------------------------------
# U8 — stage
# --------------------------------------------------------------------------


def test_stage_dry_run_mutates_nothing(store, client):
    _seed_two_recordings(store)
    before = dict(store.objects)
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=True, probe=IDLE, log=lambda _m: None
    )
    assert store.objects == before  # nothing copied
    assert store.rewrite_log == []
    # Every recordings/ object is planned; sessions/ is not enumerated.
    assert all(o.action == "planned" for o in result.outcomes)
    assert {o.src for o in result.outcomes} == {
        "recordings/alpha/manifest.json",
        "recordings/alpha/video.mp4",
        "recordings/alpha/screenshots/0.png",
        "recordings/beta/manifest.json",
    }


def test_stage_copies_and_verifies_excluding_sessions(store, client):
    _seed_two_recordings(store)
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None
    )
    assert result.ok
    # Nested suffix preserved; sessions/ NOT staged.
    assert "import-review/alpha/screenshots/0.png" in store.objects
    assert "import-review/beta/manifest.json" in store.objects
    assert not any(k.startswith("import-review/") and "sessions" in k for k in store.objects)
    assert "import-review/_index.json" not in store.objects
    # No source deleted — flat namespace intact as rollback ground truth.
    assert store.deleted == []
    assert all(k.startswith(("recordings/", "import-review/", "sessions/")) for k in store.objects)
    assert "recordings/alpha/video.mp4" in store.objects
    # Manifest records every object as verified.
    assert result.manifest["object_count"] == 4
    assert all(o["verified"] for o in result.manifest["objects"].values())


def test_stage_large_object_multi_call_rewrite(store, client):
    store.add("recordings/big/video.mp4", crc32c="BIGCRC", content_type="video/mp4", size=99999)
    store.rewrite_plan["recordings/big/video.mp4"] = 3  # needs 3 rewrite() calls
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None
    )
    assert result.ok
    # Followed the rewriteToken to completion (3 calls), final crc matches.
    assert len([c for c in store.rewrite_log if c[1] == "import-review/big/video.mp4"]) == 3
    assert store.objects["import-review/big/video.mp4"]["crc32c"] == "BIGCRC"


def test_stage_recopies_truncated_object_not_skipped_on_exists(store, client):
    store.add("recordings/alpha/video.mp4", crc32c="GOOD", content_type="video/mp4")
    # A prior run left a truncated copy: it EXISTS but its checksum is wrong.
    store.objects["import-review/alpha/video.mp4"] = {
        "crc32c": "TRUNCATED", "content_type": "video/mp4", "size": 1, "md5_hash": None
    }
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None
    )
    assert result.ok
    [outcome] = result.outcomes
    assert outcome.action == "recopied"
    assert store.objects["import-review/alpha/video.mp4"]["crc32c"] == "GOOD"


def test_stage_idempotent_second_run_skips_verified(store, client):
    _seed_two_recordings(store)
    core.run_stage(client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None)
    store.rewrite_log.clear()
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None
    )
    assert result.ok
    assert all(o.action == "skipped-verified" for o in result.outcomes)
    assert store.rewrite_log == []  # nothing re-copied


def test_stage_failed_verify_marks_incomplete(store, client):
    store.add("recordings/alpha/video.mp4", crc32c="GOOD", content_type="video/mp4")
    store.corrupt_on_rewrite.add("recordings/alpha/video.mp4")
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None
    )
    assert not result.ok
    assert result.failures[0].action == "failed"
    assert result.manifest["objects"]["recordings/alpha/video.mp4"]["verified"] is False


# --------------------------------------------------------------------------
# U8 — bucket-IAM pre-check + quiesce guard
# --------------------------------------------------------------------------


def test_stage_refuses_with_public_iam_binding(store, client):
    _seed_two_recordings(store)
    store.iam_bindings = [{"role": "roles/storage.objectViewer", "members": {"allUsers"}}]
    with pytest.raises(MigrationError, match="public read bindings"):
        core.run_stage(client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None)
    assert store.rewrite_log == []  # never started copying


def test_stage_removes_public_iam_then_stages(store, client):
    _seed_two_recordings(store)
    store.iam_bindings = [
        {"role": "roles/storage.objectViewer", "members": {"allUsers", "someone@x.com"}},
        {"role": "roles/storage.legacyObjectReader", "members": {"allAuthenticatedUsers"}},
    ]
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, remove_public_iam=True,
        probe=IDLE, log=lambda _m: None,
    )
    assert result.ok
    assert core.find_public_iam_bindings(client.bucket(BUCKET)) == []
    # The non-public member on the shared binding is preserved.
    members = {m for b in store.iam_bindings for m in b["members"]}
    assert "someone@x.com" in members
    assert "allUsers" not in members and "allAuthenticatedUsers" not in members


def test_stage_remove_public_iam_preserves_conditional_binding(store, client):
    # A conditional binding on a non-public member must survive removal of the
    # public one — dropping its `condition` would silently widen it.
    store.iam_bindings = [
        {"role": "roles/storage.objectViewer", "members": {"allUsers"}},
        {
            "role": "roles/storage.objectViewer",
            "members": {"svc@x.iam.gserviceaccount.com"},
            "condition": {"title": "tmp", "expression": "request.time < timestamp('2027-01-01T00:00:00Z')"},
        },
    ]
    core.remove_public_iam_bindings(client.bucket(BUCKET), log=lambda _m: None)
    survivors = [b for b in store.iam_bindings if "svc@x.iam.gserviceaccount.com" in b["members"]]
    assert len(survivors) == 1
    assert survivors[0].get("condition", {}).get("title") == "tmp"  # condition preserved
    assert core.find_public_iam_bindings(client.bucket(BUCKET)) == []


def test_stage_dry_run_with_public_iam_refuses_with_hint(store, client):
    _seed_two_recordings(store)
    store.iam_bindings = [{"role": "roles/storage.objectViewer", "members": {"allUsers"}}]
    # --dry-run + --remove-public-iam must refuse (dry-run cannot mutate IAM) and
    # give the dry-run-specific remediation hint.
    with pytest.raises(MigrationError, match="dry-run does not mutate IAM"):
        core.run_stage(
            client=client, bucket_name=BUCKET, dry_run=True, remove_public_iam=True,
            probe=IDLE, log=lambda _m: None,
        )


# --------------------------------------------------------------------------
# U8 — UBLA / default-object-ACL pre-check (the second public door, SCR-146)
# --------------------------------------------------------------------------


def test_read_iam_configuration_reads_live_posture(store, client):
    # Must reload() before reading: the default fake posture (correctly-configured)
    # is only visible after reload — an un-reloaded bucket reads UBLA off / PAP unset.
    assert core.read_iam_configuration(client.bucket(BUCKET)) == (True, "enforced")
    store.ubla_enabled = False
    store.public_access_prevention = "inherited"
    assert core.read_iam_configuration(client.bucket(BUCKET)) == (False, "inherited")


def test_find_public_default_object_acl_detects_both_public_entities(store, client):
    store.default_object_acl = [
        {"entity": "allUsers", "role": "READER"},
        {"entity": "user-someone@x.com", "role": "OWNER"},  # private grant, ignored
        {"entity": "allAuthenticatedUsers", "role": "READER"},
    ]
    found = core.find_public_default_object_acl(client.bucket(BUCKET))
    assert sorted(found) == [("READER", "allAuthenticatedUsers"), ("READER", "allUsers")]


def test_stage_refuses_with_public_default_object_acl_when_ubla_off(store, client):
    # The exact SCR-146 hole: zero public IAM bindings, but UBLA off + a public
    # default object ACL would make every staged import-review/ object public.
    _seed_two_recordings(store)
    store.ubla_enabled = False
    store.public_access_prevention = "inherited"
    store.default_object_acl = [{"entity": "allUsers", "role": "READER"}]
    with pytest.raises(MigrationError, match="default object ACL"):
        core.run_stage(client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None)
    assert store.rewrite_log == []  # never started copying


def test_stage_dry_run_refuses_with_public_default_object_acl(store, client):
    # The ACL gate is read-only and fail-closed, so it refuses even on a dry-run
    # (unlike --remove-public-iam, it never mutates anything to defer).
    _seed_two_recordings(store)
    store.ubla_enabled = False
    store.public_access_prevention = "inherited"  # PAP not enforced -> ACL is live
    store.default_object_acl = [{"entity": "allAuthenticatedUsers", "role": "READER"}]
    with pytest.raises(MigrationError, match="default object ACL"):
        core.run_stage(client=client, bucket_name=BUCKET, dry_run=True, probe=IDLE, log=lambda _m: None)
    assert store.rewrite_log == []  # never started copying


def test_stage_proceeds_when_ubla_enabled_even_with_public_default_acl(store, client):
    # UBLA on makes object ACLs inert — a stale public default ACL must NOT refuse
    # (no false-refuse). PAP is left un-enforced so this exercises the UBLA branch
    # (not the PAP short-circuit). Also guards the reload(): without it the gate
    # would read UBLA off, enumerate the ACL, find allUsers, and wrongly refuse.
    _seed_two_recordings(store)
    store.ubla_enabled = True
    store.public_access_prevention = "inherited"
    store.default_object_acl = [{"entity": "allUsers", "role": "READER"}]
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None
    )
    assert result.ok
    assert store.rewrite_log  # staging proceeded


def test_stage_proceeds_when_ubla_off_with_private_default_acl(store, client):
    # UBLA off but the default object ACL grants no public access — staged objects
    # inherit a private ACL, so staging must proceed (no false-refuse).
    _seed_two_recordings(store)
    store.ubla_enabled = False
    store.public_access_prevention = "inherited"
    store.default_object_acl = [{"entity": "user-someone@x.com", "role": "OWNER"}]
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None
    )
    assert result.ok


def test_stage_proceeds_when_pap_enforced_even_with_ubla_off_and_public_acl(store, client):
    # public_access_prevention=enforced blocks all public access bucket-wide (IAM +
    # ACLs), so a hardened-but-not-yet-UBLA bucket with a stale public default ACL is
    # genuinely private and must NOT be refused (the ticket's "don't false-refuse a
    # correctly-configured bucket" — GCS would never serve that ACL publicly).
    _seed_two_recordings(store)
    store.ubla_enabled = False
    store.public_access_prevention = "enforced"
    store.default_object_acl = [{"entity": "allUsers", "role": "READER"}]
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None
    )
    assert result.ok
    assert store.rewrite_log  # staging proceeded


def test_stage_fails_closed_when_bucket_reload_errors(store, client):
    # The UBLA pre-check reads LIVE bucket state; if that read fails (bucket gone,
    # permission denied), staging must abort before any copy — never fail-open by
    # treating an un-read bucket as private. The raw GCS error is surfaced as a
    # fail-closed MigrationError (clean message + exit 2), not an uncaught traceback.
    from google.api_core.exceptions import Forbidden

    _seed_two_recordings(store)
    store.reload_error = Forbidden("missing storage.buckets.get / defaultObjectAcl read")
    with pytest.raises(MigrationError, match="could not read bucket metadata"):
        core.run_stage(client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None)
    assert store.rewrite_log == []  # never started copying


def test_stage_fails_closed_when_bucket_reload_raises_retry_error(store, client):
    # RetryError (retry-deadline exhaustion) is NOT a GoogleAPICallError — it sits
    # under the broader GoogleAPIError base. The pre-check catch must still fail
    # closed with a MigrationError rather than letting the raw error escape as an
    # uncaught traceback (exit 1).
    from google.api_core.exceptions import RetryError

    _seed_two_recordings(store)
    store.reload_error = RetryError("retry deadline exceeded", TimeoutError("transient"))
    with pytest.raises(MigrationError, match="could not read bucket metadata"):
        core.run_stage(client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None)
    assert store.rewrite_log == []  # never started copying


def test_stage_fails_closed_when_iam_policy_read_errors(store, client):
    # Same fail-closed contract for the IAM-binding pre-check: a get_iam_policy
    # failure refuses to stage with a MigrationError rather than crashing.
    from google.api_core.exceptions import Forbidden

    _seed_two_recordings(store)
    store.iam_policy_error = Forbidden("missing storage.buckets.getIamPolicy")
    with pytest.raises(MigrationError, match="could not read bucket IAM policy"):
        core.run_stage(client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None)
    assert store.rewrite_log == []  # never started copying


def test_stage_refuses_while_recording_active(store, client):
    _seed_two_recordings(store)
    with pytest.raises(MigrationError, match="ACTIVE"):
        core.run_stage(
            client=client, bucket_name=BUCKET, dry_run=False,
            probe=lambda: RecordingState.ACTIVE, log=lambda _m: None,
        )
    assert store.rewrite_log == []


def test_stage_unknown_state_requires_confirm(store, client):
    _seed_two_recordings(store)
    with pytest.raises(MigrationError, match="confirm-quiesced"):
        core.run_stage(
            client=client, bucket_name=BUCKET, dry_run=False,
            probe=lambda: RecordingState.UNKNOWN, log=lambda _m: None,
        )
    # With the attestation it proceeds.
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, confirm_quiesced=True,
        probe=lambda: RecordingState.UNKNOWN, log=lambda _m: None,
    )
    assert result.ok


# --------------------------------------------------------------------------
# U8 — promote
# --------------------------------------------------------------------------


def _seed_staging(store):
    store.add("import-review/alpha/manifest.json", content_type="application/json")
    store.add("import-review/alpha/_unlisted", content_type="application/octet-stream", size=0)
    store.add("import-review/alpha/show_on_website", content_type="application/octet-stream", size=0)
    store.add("import-review/alpha/screenshots/0.png", content_type="image/png")
    store.add("import-review/beta/manifest.json", content_type="application/json")


def test_promote_only_allow_listed_and_strips_markers(store, client):
    _seed_staging(store)
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["alpha"], dry_run=False, log=lambda _m: None
    )
    assert result.ok
    assert result.promoted == ["alpha"]
    # alpha promoted with nested suffix preserved; markers stripped (not copied).
    assert "demo/alpha/manifest.json" in store.objects
    assert "demo/alpha/screenshots/0.png" in store.objects
    assert "demo/alpha/_unlisted" not in store.objects
    assert "demo/alpha/show_on_website" not in store.objects
    assert {"import-review/alpha/_unlisted", "import-review/alpha/show_on_website"} == set(
        result.stripped_markers
    )
    # beta was NOT allow-listed — never reaches demo/.
    assert not any(k.startswith("demo/beta/") for k in store.objects)


def test_promote_dry_run_mutates_nothing(store, client):
    _seed_staging(store)
    before = dict(store.objects)
    core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["alpha", "beta"], dry_run=True, log=lambda _m: None
    )
    assert store.objects == before
    assert store.rewrite_log == []


def test_promote_reports_missing_allow_list_entry(store, client):
    _seed_staging(store)
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["alpha", "ghost"], dry_run=False, log=lambda _m: None
    )
    assert result.missing_from_staging == ["ghost"]


def test_promote_skips_invalid_demo_name(store, client):
    # A staged recording whose NAME the demo handlers would reject must NOT be
    # promoted (it would be hidden-but-public).
    store.add("import-review/..evil/manifest.json", content_type="application/json")
    store.add("import-review/ok/manifest.json", content_type="application/json")
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["..evil", "ok"], dry_run=False, log=lambda _m: None
    )
    assert "..evil" in result.invalid_names
    assert not any(k.startswith("demo/..evil") for k in store.objects)
    assert "demo/ok/manifest.json" in store.objects
    assert "..evil" not in result.missing_from_staging  # surfaced as invalid, not missing


def test_promote_verify_failure_marks_incomplete(store, client):
    store.add("import-review/alpha/video.mp4", crc32c="V", content_type="video/mp4")
    store.corrupt_on_rewrite.add("import-review/alpha/video.mp4")
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["alpha"], dry_run=False, log=lambda _m: None
    )
    assert not result.ok
    assert result.failures[0].action == "failed"


def test_promote_dry_run_promoted_is_empty(store, client):
    _seed_staging(store)
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["alpha"], dry_run=True, log=lambda _m: None
    )
    assert result.promoted == []  # nothing actually promoted on a dry-run


def test_promote_idempotent_second_run_skips_verified(store, client):
    _seed_staging(store)
    core.run_promote(client=client, bucket_name=BUCKET, allow_list=["alpha"], dry_run=False, log=lambda _m: None)
    store.rewrite_log.clear()
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["alpha"], dry_run=False, log=lambda _m: None
    )
    assert result.ok
    assert all(o.action == "skipped-verified" for o in result.outcomes)
    assert store.rewrite_log == []


# --------------------------------------------------------------------------
# U9 — decommission
# --------------------------------------------------------------------------


def _seed_flat_with_staging(store, *, verified=True):
    """Flat recordings/ with matching import-review/ copies (verified iff True)."""
    store.add("recordings/alpha/manifest.json", crc32c="A", content_type="application/json")
    store.add("recordings/alpha/video.mp4", crc32c="V", content_type="video/mp4")
    store.objects["import-review/alpha/manifest.json"] = {
        "crc32c": "A", "content_type": "application/json", "size": 10, "md5_hash": None
    }
    store.objects["import-review/alpha/video.mp4"] = {
        "crc32c": "V" if verified else "MISMATCH", "content_type": "video/mp4",
        "size": 10, "md5_hash": None,
    }


def test_decommission_dry_run_lists_but_deletes_nothing(store, client):
    _seed_flat_with_staging(store)
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=True,
        probe=IDLE, log=lambda _m: None,
    )
    assert store.deleted == []
    assert all(o.action == "planned" for o in result.outcomes)
    assert result.rescan is None  # re-scan only runs on a live pass


def test_decommission_deletes_verified_and_rescan_empty(store, client):
    _seed_flat_with_staging(store)
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    assert set(store.deleted) == {"recordings/alpha/manifest.json", "recordings/alpha/video.mp4"}
    assert result.rescan.empty
    assert result.rescan.new_blobs == []
    # Staging copies untouched.
    assert "import-review/alpha/video.mp4" in store.objects


def test_decommission_gate_holds_for_unverified_staging(store, client):
    _seed_flat_with_staging(store, verified=False)  # video staging crc mismatches
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    # The mismatched object is KEPT; the verified one is deleted.
    assert "recordings/alpha/video.mp4" not in store.deleted
    assert "recordings/alpha/video.mp4" in store.objects
    assert "recordings/alpha/manifest.json" in store.deleted
    assert any(o.action == "kept" for o in result.outcomes)
    assert not result.rescan.empty  # the kept object remains


def test_decommission_keeps_source_without_any_staging_copy(store, client):
    store.add("recordings/orphan/manifest.json", crc32c="O", content_type="application/json")
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    assert store.deleted == []
    assert result.kept[0].reason == "no staging copy"


def test_decommission_surfaces_new_blob_since_enumeration(store, client):
    _seed_flat_with_staging(store)

    # Simulate a write that races in DURING the run: a fresh flat blob with no
    # staging copy appears. The re-scan must surface it, not delete it.
    real_list = client.list_blobs
    injected = {"done": False}

    def racing_list(bucket_name, prefix=None, timeout=None):
        blobs = real_list(bucket_name, prefix=prefix, timeout=timeout)
        if not injected["done"] and prefix == core.FLAT_RECORDINGS_PREFIX:
            injected["done"] = True  # only after the first (enumeration) listing
            store.add("recordings/late/manifest.json", crc32c="L", content_type="application/json")
        return blobs

    client.list_blobs = racing_list
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    assert "recordings/late/manifest.json" in result.rescan.new_blobs
    assert "recordings/late/manifest.json" not in store.deleted


def test_decommission_sessions_only_with_opt_in(store, client):
    store.add("sessions/_index.json", crc32c="S", content_type="application/json")
    store.add("sessions/foo/data.json", crc32c="S2", content_type="application/json")

    # Without the opt-in, sessions/ is untouched.
    core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    assert store.deleted == []

    # With the opt-in AND the backup attestation, retired sessions are deleted (no
    # staging gate — they were never migrated; the zkairdrop archive is the backup).
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=True, dry_run=False,
        sessions_backup_confirmed=True, probe=IDLE, log=lambda _m: None,
    )
    assert set(store.deleted) == {"sessions/_index.json", "sessions/foo/data.json"}
    assert result.rescan.empty


def test_decommission_idempotent_rerun(store, client):
    _seed_flat_with_staging(store)
    core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    store.deleted.clear()
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    assert store.deleted == []  # nothing left to delete
    assert result.rescan.empty


def test_decommission_refuses_while_recording_active(store, client):
    # The irreversible step must honor the quiesce guard too.
    _seed_flat_with_staging(store)
    with pytest.raises(MigrationError, match="ACTIVE"):
        core.run_decommission(
            client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
            probe=lambda: RecordingState.ACTIVE, log=lambda _m: None,
        )
    assert store.deleted == []


def test_decommission_unknown_state_requires_confirm(store, client):
    _seed_flat_with_staging(store)
    with pytest.raises(MigrationError, match="confirm-quiesced"):
        core.run_decommission(
            client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
            probe=lambda: RecordingState.UNKNOWN, log=lambda _m: None,
        )
    # With attestation it proceeds.
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        confirm_quiesced=True, probe=lambda: RecordingState.UNKNOWN, log=lambda _m: None,
    )
    assert result.rescan.empty


# --------------------------------------------------------------------------
# Thin CLI shims (arg parsing, gates, exit codes) via the fake client
# --------------------------------------------------------------------------


@pytest.fixture
def patch_build_client(monkeypatch, client):
    """Make every shim's core.build_client return the in-memory fake, and stub the
    real ``screencap status`` probe to IDLE so shims need not shell out."""
    monkeypatch.setattr(core, "build_client", lambda project=None: client)
    monkeypatch.setattr(core, "probe_recording_state", lambda: RecordingState.IDLE)


def test_stage_cli_dry_run_writes_sidecar_manifest(store, client, patch_build_client, tmp_path):
    import migrate_flat_to_staging as stage_cli

    _seed_two_recordings(store)
    manifest = tmp_path / "m.json"
    rc = stage_cli.main(["--bucket", BUCKET, "--dry-run", "--manifest", str(manifest)])
    assert rc == 0
    assert (tmp_path / "m.json.dryrun.json").exists()
    assert not manifest.exists()  # real manifest path untouched on a dry-run
    assert store.rewrite_log == []


def test_stage_cli_public_iam_returns_error_code(store, client, patch_build_client, tmp_path):
    import migrate_flat_to_staging as stage_cli

    _seed_two_recordings(store)
    store.iam_bindings = [{"role": "roles/storage.objectViewer", "members": {"allUsers"}}]
    rc = stage_cli.main(["--bucket", BUCKET, "--manifest", str(tmp_path / "m.json")])
    assert rc == 2  # MigrationError -> exit 2


def test_stage_cli_public_default_object_acl_returns_error_code(store, client, patch_build_client, tmp_path):
    import migrate_flat_to_staging as stage_cli

    _seed_two_recordings(store)
    store.ubla_enabled = False
    store.public_access_prevention = "inherited"
    store.default_object_acl = [{"entity": "allUsers", "role": "READER"}]
    rc = stage_cli.main(["--bucket", BUCKET, "--manifest", str(tmp_path / "m.json")])
    assert rc == 2  # MigrationError -> exit 2


def test_promote_cli_empty_allow_list_refuses(client, patch_build_client, tmp_path):
    import promote_staging_to_demo as promote_cli

    empty = tmp_path / "allow.txt"
    empty.write_text("# only comments\n\n")
    rc = promote_cli.main(["--bucket", BUCKET, "--allow-list", str(empty)])
    assert rc == 2


def test_promote_cli_promotes_from_allow_list_file(store, client, patch_build_client, tmp_path):
    import promote_staging_to_demo as promote_cli

    _seed_staging(store)
    allow = tmp_path / "allow.txt"
    allow.write_text("alpha\n# beta intentionally withheld\n")
    rc = promote_cli.main(["--bucket", BUCKET, "--allow-list", str(allow)])
    assert rc == 0
    assert "demo/alpha/manifest.json" in store.objects
    assert not any(k.startswith("demo/beta/") for k in store.objects)


def test_promote_cli_returns_rc1_on_gcs_error_escaping_run_promote(
    store, client, patch_build_client, tmp_path, monkeypatch
):
    # A GCS error that escapes run_promote ITSELF (e.g. enumeration via iter_blobs,
    # not the per-object _copy_one seam) must hit the shim's top-level catch — the
    # `raise_on_rewrite` seam only reaches _copy_one, so it cannot exercise this path.
    # RetryError proves the catch uses the broad GoogleAPIError, not GoogleAPICallError.
    import promote_staging_to_demo as promote_cli
    from google.api_core.exceptions import RetryError

    _seed_staging(store)
    allow = tmp_path / "allow.txt"
    allow.write_text("alpha\n")

    def boom(*_a, **_k):
        raise RetryError("simulated retry exhaustion enumerating staging", None)

    monkeypatch.setattr(core, "iter_blobs", boom)
    rc = promote_cli.main(["--bucket", BUCKET, "--allow-list", str(allow)])
    assert rc == 1  # GCS error during promote -> exit 1, no raw traceback


def test_decommission_cli_requires_confirm_for_live(store, client, patch_build_client, tmp_path):
    import decommission_flat_namespace as decom_cli

    _seed_flat_with_staging(store)
    # --audit-log to tmp for consistency/hygiene; the --confirm guard returns before
    # the audit log is opened, so nothing is written here today.
    rc = decom_cli.main(["--bucket", BUCKET, "--audit-log", str(tmp_path / "a.jsonl")])
    assert rc == 2
    assert store.deleted == []


def test_decommission_cli_confirm_deletes(store, client, patch_build_client, tmp_path):
    import decommission_flat_namespace as decom_cli

    _seed_flat_with_staging(store)
    audit = str(tmp_path / "audit.jsonl")
    rc = decom_cli.main(["--bucket", BUCKET, "--confirm", "--audit-log", audit])
    assert rc == 0
    assert set(store.deleted) == {"recordings/alpha/manifest.json", "recordings/alpha/video.mp4"}


def test_stage_cli_verify_failure_returns_rc1(store, client, patch_build_client, tmp_path):
    import migrate_flat_to_staging as stage_cli

    store.add("recordings/alpha/video.mp4", crc32c="V", content_type="video/mp4")
    store.corrupt_on_rewrite.add("recordings/alpha/video.mp4")
    rc = stage_cli.main(["--bucket", BUCKET, "--manifest", str(tmp_path / "m.json")])
    assert rc == 1  # incomplete staging — operator must not promote/decommission


def test_promote_cli_missing_entry_returns_rc1(store, client, patch_build_client, tmp_path):
    import promote_staging_to_demo as promote_cli

    _seed_staging(store)
    allow = tmp_path / "allow.txt"
    allow.write_text("alpha\nghost\n")  # ghost is not staged
    rc = promote_cli.main(["--bucket", BUCKET, "--allow-list", str(allow)])
    assert rc == 1  # a typo'd / unstaged entry must not read as a complete promote


def test_decommission_cli_new_blob_returns_rc1(store, client, patch_build_client, tmp_path):
    import decommission_flat_namespace as decom_cli

    _seed_flat_with_staging(store)
    real_list = client.list_blobs
    injected = {"done": False}

    def racing_list(bucket_name, prefix=None, timeout=None):
        blobs = real_list(bucket_name, prefix=prefix, timeout=timeout)
        if not injected["done"] and prefix == core.FLAT_RECORDINGS_PREFIX:
            injected["done"] = True
            store.add("recordings/late/manifest.json", crc32c="L", content_type="application/json")
        return blobs

    client.list_blobs = racing_list
    rc = decom_cli.main(["--bucket", BUCKET, "--confirm", "--audit-log", str(tmp_path / "a.jsonl")])
    assert rc == 1  # new flat blob since enumeration — R11 not satisfied


# --------------------------------------------------------------------------
# U9 — generation-pinned delete (verify→delete atomicity)
# --------------------------------------------------------------------------


def test_decommission_keeps_source_changed_since_enumeration(store, client):
    # The source verifies against staging at enumeration, but is OVERWRITTEN (its
    # live generation bumps) before the delete fires. The generation-pinned delete
    # must refuse (PreconditionFailed) and KEEP the source rather than destroy the
    # newer, un-staged bytes.
    _seed_flat_with_staging(store)

    orig_get_blob = FakeBucket.get_blob
    bumped = {"done": False}

    # Wrap get_blob so the verify GET for the video's staging copy advances the
    # LIVE source generation — modeling a racing overwrite that lands between the
    # verify and the (now-stale-generation) delete.
    def get_blob_then_bump(self, name, timeout=None):
        res = orig_get_blob(self, name, timeout=timeout)
        if name == "import-review/alpha/video.mp4" and not bumped["done"]:
            bumped["done"] = True
            store.objects["recordings/alpha/video.mp4"]["generation"] = 99
        return res

    FakeBucket.get_blob = get_blob_then_bump
    try:
        result = core.run_decommission(
            client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
            probe=IDLE, log=lambda _m: None,
        )
    finally:
        FakeBucket.get_blob = orig_get_blob

    # The video source CHANGED since enumeration → KEPT, not deleted.
    assert "recordings/alpha/video.mp4" not in store.deleted
    assert "recordings/alpha/video.mp4" in store.objects
    kept_video = [o for o in result.kept if o.src == "recordings/alpha/video.mp4"]
    assert kept_video and "generation mismatch" in kept_video[0].reason
    # Pin the EXACT reason so the except-ordering invariant is actually exercised:
    # PreconditionFailed (a GoogleAPICallError subclass) MUST be caught before the
    # broad `except GoogleAPIError`. If the order were reversed, this would land in
    # the generic `error: …` bucket — whose string also happens to contain
    # "generation mismatch", so the substring check above alone would not catch it.
    assert not kept_video[0].reason.startswith("error:")
    assert kept_video[0].reason == "source changed since enumeration (generation mismatch)"
    # The unchanged manifest source still deletes normally.
    assert "recordings/alpha/manifest.json" in store.deleted


# --------------------------------------------------------------------------
# U9 — partial accounting on a GCS error during delete
# --------------------------------------------------------------------------


def test_decommission_keeps_source_on_gcs_error_and_continues(store, client):
    from google.api_core.exceptions import GoogleAPICallError

    _seed_flat_with_staging(store)

    orig_delete = FakeBlob.delete

    def flaky_delete(self, if_generation_match=None, timeout=None):
        if self.name == "recordings/alpha/video.mp4":
            raise GoogleAPICallError("simulated transient GCS error")
        return orig_delete(self, if_generation_match=if_generation_match, timeout=timeout)

    FakeBlob.delete = flaky_delete
    try:
        result = core.run_decommission(
            client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
            probe=IDLE, log=lambda _m: None,
        )
    finally:
        FakeBlob.delete = orig_delete

    # The errored object is KEPT (never marked deleted) but the loop CONTINUED and
    # deleted the other verified source — a complete deleted/kept accounting.
    assert "recordings/alpha/video.mp4" not in store.deleted
    assert "recordings/alpha/manifest.json" in store.deleted
    assert any(o.src == "recordings/alpha/video.mp4" and o.action == "kept" for o in result.outcomes)
    assert result.kept_on_error and result.kept_on_error[0].reason.startswith("error: ")


# --------------------------------------------------------------------------
# SCR-145 U1 — RetryError breadth (GoogleAPIError, NOT just GoogleAPICallError)
# RetryError is the exception the SDK raises once it exhausts its OWN retries on a
# persistent 429/503; it is a GoogleAPIError but NOT a GoogleAPICallError, so a
# narrow `except GoogleAPICallError` would let it abort the whole loop.
# --------------------------------------------------------------------------


def test_stage_keeps_running_on_retry_error_during_copy(store, client):
    from google.api_core.exceptions import RetryError

    _seed_two_recordings(store)
    store.raise_on_rewrite["recordings/alpha/video.mp4"] = RetryError(
        "simulated retry exhaustion", None
    )
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None,
    )
    # The errored object is recorded failed; the loop CONTINUED and staged the rest,
    # and run_stage still produced a result (manifest), surfacing the failure.
    assert not result.ok
    assert any(o.src == "recordings/alpha/video.mp4" and o.action == "failed" for o in result.failures)
    assert any(o.src == "recordings/alpha/manifest.json" and o.verified for o in result.outcomes)
    assert any(o.src == "recordings/beta/manifest.json" and o.verified for o in result.outcomes)


def test_decommission_keeps_source_on_retry_error_during_delete(store, client):
    from google.api_core.exceptions import RetryError

    _seed_flat_with_staging(store)
    store.raise_on_delete["recordings/alpha/video.mp4"] = RetryError(
        "simulated retry exhaustion", None
    )
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    assert "recordings/alpha/video.mp4" not in store.deleted
    assert any(o.src == "recordings/alpha/video.mp4" and o.action == "kept" for o in result.outcomes)
    assert result.kept_on_error and result.kept_on_error[0].reason.startswith("error: ")
    # Loop continued and deleted the verified sibling.
    assert "recordings/alpha/manifest.json" in store.deleted


def test_decommission_keeps_session_on_retry_error_during_delete(store, client):
    # The sessions/ delete loop's widened catch (GoogleAPIError) must absorb a
    # RetryError per-object too — structurally identical to the recordings/ branch
    # but its own except block, so it gets its own regression test.
    from google.api_core.exceptions import RetryError

    store.add("sessions/_index.json", crc32c="S", content_type="application/json")
    store.raise_on_delete["sessions/_index.json"] = RetryError("simulated retry exhaustion", None)
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=True, dry_run=False,
        sessions_backup_confirmed=True, probe=IDLE, log=lambda _m: None,
    )
    assert "sessions/_index.json" not in store.deleted
    assert result.kept_on_error and any(
        o.src == "sessions/_index.json" and o.reason.startswith("error: ")
        for o in result.kept_on_error
    )


def test_decommission_keeps_source_on_error_fetching_staging(store, client):
    # A transient error fetching the staging copy (the currently-unwrapped get_blob)
    # must KEEP that source and continue — not abort the whole loop. RetryError also
    # proves the wrap uses the broad GoogleAPIError, not GoogleAPICallError.
    from google.api_core.exceptions import RetryError

    _seed_flat_with_staging(store)
    store.raise_on_get_blob["import-review/alpha/video.mp4"] = RetryError(
        "simulated retry exhaustion", None
    )
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    assert "recordings/alpha/video.mp4" not in store.deleted
    assert any(
        o.src == "recordings/alpha/video.mp4" and o.action == "kept"
        and o.reason.startswith("error: ")
        for o in result.outcomes
    )
    assert "recordings/alpha/manifest.json" in store.deleted


def test_promote_marks_failed_on_retry_error(store, client):
    from google.api_core.exceptions import RetryError

    store.add("import-review/alpha/video.mp4", crc32c="V", content_type="video/mp4")
    store.raise_on_rewrite["import-review/alpha/video.mp4"] = RetryError(
        "simulated retry exhaustion", None
    )
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["alpha"], dry_run=False,
        log=lambda _m: None,
    )
    assert not result.ok
    assert any(o.action == "failed" for o in result.outcomes)


# --------------------------------------------------------------------------
# SCR-145 U2 — every per-object GCS call passes an explicit timeout
# --------------------------------------------------------------------------


def test_stage_passes_explicit_timeout_on_every_call(store, client):
    _seed_two_recordings(store)  # fresh recordings/, nothing staged → real copies
    core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None,
    )
    ops = {op for op, _t in store.seen_timeouts}
    assert {"rewrite", "reload", "get_blob"} <= ops
    assert store.seen_timeouts and all(t == core._OP_TIMEOUT for _op, t in store.seen_timeouts)


def test_decommission_passes_explicit_timeout_on_delete_and_get(store, client):
    _seed_flat_with_staging(store)
    core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    ops = {op for op, _t in store.seen_timeouts}
    assert {"get_blob", "delete"} <= ops
    assert store.seen_timeouts and all(t == core._OP_TIMEOUT for _op, t in store.seen_timeouts)


# --------------------------------------------------------------------------
# SCR-145 U3 — copy_blob rewriteToken loop is bounded (iterations + wall-clock)
# --------------------------------------------------------------------------


def test_stage_fails_object_on_stuck_rewrite_token_iteration_cap(store, client, monkeypatch):
    monkeypatch.setattr(core, "_REWRITE_MAX_ITERS", 5)  # small cap so the test is fast
    _seed_two_recordings(store)
    store.rewrite_never_completes.add("recordings/alpha/video.mp4")
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None,
    )
    # The stuck object failed; the loop CONTINUED and staged the rest; run still ok'd.
    assert not result.ok
    assert any(
        o.src == "recordings/alpha/video.mp4" and o.action == "failed" for o in result.failures
    )
    assert any(o.src == "recordings/beta/manifest.json" and o.verified for o in result.outcomes)


def test_copy_blob_fails_on_wall_clock_deadline(store, client, monkeypatch):
    # Iteration cap left high; a clock jumped past the deadline trips the wall-clock
    # bound, proving it fires independently of iteration count. Single object so the
    # only time.monotonic() callers are copy_blob's entry + first cap check.
    store.add("recordings/solo/video.mp4", crc32c="V", content_type="video/mp4")
    store.rewrite_never_completes.add("recordings/solo/video.mp4")
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else core._REWRITE_MAX_WALL + 1.0

    monkeypatch.setattr(core.time, "monotonic", clock)
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None,
    )
    assert not result.ok
    assert any(
        o.src == "recordings/solo/video.mp4" and o.action == "failed" for o in result.failures
    )


def test_stage_large_object_under_cap_still_completes(store, client):
    # A legitimately large object needing many (but under-cap) rewrite calls must NOT
    # false-trigger the cap.
    store.add("recordings/big/video.mp4", crc32c="B", content_type="video/mp4", size=9999)
    store.rewrite_plan["recordings/big/video.mp4"] = 500
    result = core.run_stage(
        client=client, bucket_name=BUCKET, dry_run=False, probe=IDLE, log=lambda _m: None,
    )
    assert result.ok
    assert any(o.src == "recordings/big/video.mp4" and o.action == "copied" for o in result.outcomes)


# --------------------------------------------------------------------------
# SCR-145 U4 — durable decommission audit log (on_outcome + .jsonl, 0o600)
# --------------------------------------------------------------------------


def test_decommission_on_outcome_fires_per_outcome(store, client):
    _seed_flat_with_staging(store)
    seen: list = []
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, on_outcome=seen.append, log=lambda _m: None,
    )
    # Exactly one callback per outcome, in loop order, matching the returned result.
    assert [(o.src, o.action) for o in seen] == [(o.src, o.action) for o in result.outcomes]
    # 'deleted' rows correspond to what was actually removed.
    assert {o.src for o in seen if o.action == "deleted"} == set(store.deleted)


def test_decommission_audit_callback_records_before_a_mid_run_crash(store, client):
    # A non-GCS error (uncaught → models a process crash) on the 2nd delete must leave
    # the 1st object's outcome ALREADY emitted to the sink — proving the audit log is
    # written incrementally per outcome, not batched at the end.
    _seed_flat_with_staging(store)
    store.raise_on_delete["recordings/alpha/video.mp4"] = RuntimeError("simulated crash")
    seen: list = []
    with pytest.raises(RuntimeError, match="simulated crash"):
        core.run_decommission(
            client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
            probe=IDLE, on_outcome=seen.append, log=lambda _m: None,
        )
    # manifest.json sorts before video.mp4 → it was deleted and emitted pre-crash.
    assert any(o.src == "recordings/alpha/manifest.json" and o.action == "deleted" for o in seen)
    assert "recordings/alpha/manifest.json" in store.deleted


def test_decommission_cli_writes_audit_log_0o600(store, client, patch_build_client, tmp_path):
    import decommission_flat_namespace as decom_cli

    _seed_flat_with_staging(store)
    audit = tmp_path / "audit.jsonl"
    rc = decom_cli.main(["--bucket", BUCKET, "--confirm", "--audit-log", str(audit)])
    assert rc == 0
    rows = [json.loads(ln) for ln in audit.read_text(encoding="utf-8").splitlines()]
    assert rows and all({"src", "action", "reason"} <= r.keys() for r in rows)
    assert {r["src"] for r in rows if r["action"] == "deleted"} == set(store.deleted)
    # The irreversible-step audit artifact is owner-only.
    assert (audit.stat().st_mode & 0o777) == 0o600


def test_decommission_cli_audit_log_appends_across_runs(store, client, patch_build_client, tmp_path):
    import decommission_flat_namespace as decom_cli

    audit = tmp_path / "audit.jsonl"
    _seed_flat_with_staging(store)  # alpha manifest + video, both verified
    decom_cli.main(["--bucket", BUCKET, "--confirm", "--audit-log", str(audit)])
    first = audit.read_text(encoding="utf-8").splitlines()
    assert first  # first run recorded its deletions

    # A second live run on a fresh source must APPEND, never truncate the prior record
    # (the irreversible step's audit history must survive a re-run).
    store.add("recordings/gamma/manifest.json", crc32c="G", content_type="application/json")
    store.objects["import-review/gamma/manifest.json"] = {
        "crc32c": "G", "content_type": "application/json", "size": 10, "md5_hash": None,
    }
    decom_cli.main(["--bucket", BUCKET, "--confirm", "--audit-log", str(audit)])
    second = audit.read_text(encoding="utf-8").splitlines()
    assert len(second) > len(first)
    assert second[: len(first)] == first  # prior run's lines intact at the head


def test_decommission_cli_dry_run_writes_dryrun_sidecar(store, client, patch_build_client, tmp_path):
    import decommission_flat_namespace as decom_cli

    _seed_flat_with_staging(store)
    audit = tmp_path / "audit.jsonl"
    rc = decom_cli.main(["--bucket", BUCKET, "--dry-run", "--audit-log", str(audit)])
    assert rc == 0
    assert store.deleted == []
    assert not audit.exists()  # live path is never written on a dry-run
    preview = tmp_path / "audit.jsonl.dryrun.jsonl"
    rows = [json.loads(ln) for ln in preview.read_text(encoding="utf-8").splitlines()]
    assert rows and all(r["action"] == "planned" for r in rows)


def test_decommission_cli_aborts_cleanly_on_audit_write_failure(
    store, client, patch_build_client, tmp_path, monkeypatch
):
    # The audit sink itself fails (full disk) mid-run: `_record`'s write raises an
    # OSError AFTER the first delete has already committed. The CLI must NOT crash
    # with a raw traceback — it must abort cleanly with rc 1 (incomplete/investigate),
    # and the already-deleted object stays in store.deleted (the delete committed,
    # the run understates rather than crashes).
    import decommission_flat_namespace as decom_cli

    _seed_flat_with_staging(store)  # alpha manifest + video, both verified

    real_open = decom_cli._open_audit_log

    def open_then_break_write(path, *, dry_run):
        fp = real_open(path, dry_run=dry_run)
        # A `deleted` outcome is emitted only AFTER its delete returns, so failing
        # the first write means at least one delete has already committed.
        def _boom(*_a, **_k):
            raise OSError("No space left on device")

        monkeypatch.setattr(fp, "write", _boom)
        return fp

    monkeypatch.setattr(decom_cli, "_open_audit_log", open_then_break_write)

    audit = tmp_path / "audit.jsonl"
    rc = decom_cli.main(["--bucket", BUCKET, "--confirm", "--audit-log", str(audit)])
    assert rc == 1  # clean abort, not a raw traceback
    # The delete that committed before the audit write failed is reflected — the run
    # understates (a lost audit line) rather than overstates or crashes.
    assert store.deleted  # at least one delete committed before the abort


# --------------------------------------------------------------------------
# U9 — sessions backup gate
# --------------------------------------------------------------------------


def test_decommission_sessions_withheld_without_backup_confirmation(store, client):
    store.add("sessions/_index.json", crc32c="S", content_type="application/json")
    # --include-sessions without the backup attestation must refuse (fail-closed)
    # BEFORE any delete — sessions/ has no staging copy to fall back on.
    with pytest.raises(MigrationError, match="sessions-backup-confirmed"):
        core.run_decommission(
            client=client, bucket_name=BUCKET, include_sessions=True, dry_run=False,
            sessions_backup_confirmed=False, probe=IDLE, log=lambda _m: None,
        )
    assert store.deleted == []  # nothing touched

    # With BOTH the opt-in and the attestation, retired sessions are deleted.
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=True, dry_run=False,
        sessions_backup_confirmed=True, probe=IDLE, log=lambda _m: None,
    )
    assert "sessions/_index.json" in store.deleted
    assert result.rescan.empty


def test_decommission_cli_include_sessions_requires_backup_flag(store, client, patch_build_client, tmp_path):
    import decommission_flat_namespace as decom_cli

    audit_path = tmp_path / "audit.jsonl"
    audit = str(audit_path)
    store.add("sessions/_index.json", crc32c="S", content_type="application/json")
    rc = decom_cli.main(["--bucket", BUCKET, "--confirm", "--include-sessions", "--audit-log", audit])
    assert rc == 2  # MigrationError (backup not confirmed) -> exit 2
    assert store.deleted == []
    # The audit log is opened BEFORE run_decommission, so even on the early
    # MigrationError refusal the `finally` must have closed it cleanly, leaving a
    # well-formed (empty) file — exercising the finally-close path.
    assert audit_path.exists()
    assert audit_path.read_text(encoding="utf-8") == ""

    rc = decom_cli.main(
        ["--bucket", BUCKET, "--confirm", "--include-sessions", "--sessions-backup-confirmed",
         "--audit-log", audit]
    )
    assert rc == 0
    assert "sessions/_index.json" in store.deleted


# --------------------------------------------------------------------------
# U9 — keep-path integration (fail-closed crc32c on staging)
# --------------------------------------------------------------------------


def test_decommission_keeps_source_when_staging_crc_unavailable(store, client):
    # Source has a real crc32c; its staging copy exposes crc32c=None (a composite
    # object). verify_match fails closed → the source must be KEPT, not deleted.
    store.add("recordings/comp/video.mp4", crc32c="REAL", content_type="video/mp4")
    store.objects["import-review/comp/video.mp4"] = {
        "crc32c": None, "content_type": "video/mp4", "size": 10, "md5_hash": None,
        "generation": 1,
    }
    result = core.run_decommission(
        client=client, bucket_name=BUCKET, include_sessions=False, dry_run=False,
        probe=IDLE, log=lambda _m: None,
    )
    assert store.deleted == []
    assert "recordings/comp/video.mp4" in store.objects
    [outcome] = [o for o in result.outcomes if o.src == "recordings/comp/video.mp4"]
    assert outcome.action == "kept"


# --------------------------------------------------------------------------
# U8 — promote: marker-only recordings + sessions exclusion
# --------------------------------------------------------------------------


def test_promote_marker_only_recording_surfaced_not_silently_dropped(store, client):
    # An allow-listed recording present in staging ONLY as marker blobs produces no
    # demo object — it must be surfaced as marker_only, not silently dropped, and
    # not reported as missing.
    store.add("import-review/markeronly/_unlisted", content_type="application/octet-stream", size=0)
    store.add("import-review/markeronly/show_on_website", content_type="application/octet-stream", size=0)
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["markeronly"], dry_run=False,
        log=lambda _m: None,
    )
    assert result.marker_only == ["markeronly"]
    assert result.missing_from_staging == []  # it WAS present (as markers)
    assert result.promoted == []
    assert not any(k.startswith("demo/markeronly/") for k in store.objects)


def test_promote_cli_marker_only_returns_rc1(store, client, patch_build_client, tmp_path):
    import promote_staging_to_demo as promote_cli

    store.add("import-review/markeronly/_unlisted", content_type="application/octet-stream", size=0)
    allow = tmp_path / "allow.txt"
    allow.write_text("markeronly\n")
    rc = promote_cli.main(["--bucket", BUCKET, "--allow-list", str(allow)])
    assert rc == 1  # nothing actually promoted — not a complete promote


def test_promote_refuses_sessions_name(store, client):
    # An allow-list typo naming 'sessions' must never publish retired session data
    # to public demo/.
    store.add("import-review/sessions/_index.json", content_type="application/json")
    result = core.run_promote(
        client=client, bucket_name=BUCKET, allow_list=["sessions"], dry_run=False,
        log=lambda _m: None,
    )
    assert not any(k.startswith("demo/sessions") for k in store.objects)
    assert result.promoted == []


# --------------------------------------------------------------------------
# Validator agreement — core.is_valid_demo_name vs the function's is_valid_name
# --------------------------------------------------------------------------


def test_demo_name_validator_agrees_with_cloud_function_paths():
    # The migration's "reaches demo/" guard MUST agree with the function's
    # "listable + playable" guard, or a promoted name could be hidden-but-public
    # (or a servable name wrongly skipped). Import the function's is_valid_name via
    # the same scripts/ sys.path injection the module already set up, then assert
    # the two validators agree over a shared corpus. Fall back to a regex-literal
    # equality check if the function module cannot be imported.
    import importlib.util

    paths_file = _SCRIPTS / "cloud-function" / "paths.py"
    spec = importlib.util.spec_from_file_location("_cf_paths", paths_file)
    corpus = [
        # valid
        "alpha", "a", "demo-2024", "rec_01", "x.y.z", "A1", "0start", "ok-name",
        # invalid / traversal
        "..", "..evil", "a/b", "foo/", "/foo", "", ".", "a..b/c",
        "a b", "naïve", "with space", "tab\tname", "-leadingdash",
    ]
    if spec is not None and spec.loader is not None:
        cf_paths = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cf_paths)
        for name in corpus:
            assert core.is_valid_demo_name(name) == cf_paths.is_valid_name(name), (
                f"validator divergence on {name!r}: "
                f"core={core.is_valid_demo_name(name)} cf={cf_paths.is_valid_name(name)}"
            )
    else:  # pragma: no cover — import seam present in this layout
        assert core._DEMO_NAME_RE.pattern == r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,255}$"
