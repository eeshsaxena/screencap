"""Shared, SDK-injectable core for the cloud-migration admin scripts.

This module is deliberately ``google.cloud``-free at import scope: the live GCS
client is built lazily (:func:`build_client`) only when an entry point actually
runs against GCS, and every operation function takes an injected ``client`` +
``bucket_name``. So the whole copy/verify/gate state machine is unit-testable
with a small in-memory fake (``tests/test_cloud_migration.py``) without the
``google-cloud-storage`` SDK or any GCP credential — the same offline posture
``scripts/cloud-function/conftest.py`` gives the function suite.

Safety invariants encoded here (plan U8/U9 + the project's never-delete-without-
fresh-remote-confirm rule):

* **Per-object integrity, not count/size.** A copy is verified by ``crc32c`` +
  ``content_type`` (+ ``md5_hash`` when present). Aggregate count/size collide
  across the many same-sized 0-byte marker blobs and miss a truncated or
  content-type-mangled copy that would break website playback.
* **Idempotent, checksum-gated.** A re-run trusts a CHECKSUM match, never
  ``exists()`` alone — a truncated copy "exists" but is corrupt and is re-copied.
* **No source deletes in staging/promotion.** The flat namespace is the rollback
  ground truth until U9, which deletes a source ONLY after a fresh live re-verify
  of its staging copy.
* **Private staging is a pre-condition, not a hope.** Staging refuses to run
  while the bucket carries any ``allUsers``/``allAuthenticatedUsers`` IAM binding,
  AND (SCR-146) while Uniform Bucket-Level Access is off with a public *default
  object ACL* — the second public door, which serves objects by direct URL even
  with zero public IAM bindings.
* **Quiesce.** A migration refuses while a recording is in flight on this
  machine (a late completion sentinel can land a flat blob after enumeration).
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Protocol

# --------------------------------------------------------------------------
# Namespaces (all within the one shared recordings bucket) and constants
# --------------------------------------------------------------------------

FLAT_RECORDINGS_PREFIX = "recordings/"
FLAT_SESSIONS_PREFIX = "sessions/"
STAGING_PREFIX = "import-review/"
DEMO_PREFIX = "demo/"

# Markers superseded by namespace isolation (plan Key Technical Decisions). The
# function's ``demo-list`` is marker-blind, but a vestigial marker blob copied
# into ``demo/`` wastes space and muddies audits, so promotion drops them. Matched
# as the immediate child of the recording dir (``{name}/<marker>``), mirroring the
# ``parts[1] == "_unlisted"`` check in the function's ``_collect_recordings``.
STRIPPED_MARKERS = frozenset({"_unlisted", "show_on_website"})

# IAM members that make bucket objects world-readable. "Private staging" is a
# fiction while either is bound to ANY role, so staging refuses to run until both
# are gone (plan U8 bucket-IAM pre-check; the SCR-139 ticket's first pre-check).
# The same two tokens name the public principals in a legacy object/default-object
# ACL (the ACL ``entity`` field), so the UBLA/ACL pre-check (SCR-146) reuses the set.
PUBLIC_IAM_MEMBERS = frozenset({"allUsers", "allAuthenticatedUsers"})

# ``public_access_prevention`` reads ``"enforced"`` when GCS blocks all public
# access; any other value (``"inherited"`` / legacy ``"unspecified"``) does not.
PUBLIC_ACCESS_PREVENTION_ENFORCED = "enforced"

# Mirrors the cloud function's recording-name guard (paths.py ``_NAME_RE`` +
# ``is_valid_name``): a public demo name may not contain a slash or climb via
# "..". A staged recording whose name fails this is rejected by the demo
# handlers, so promoting it would create a hidden-but-public object — promotion
# skips it (see ``run_promote``).
_DEMO_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,255}$")

# list_blobs page timeout — matches the function's 60s.
_LIST_TIMEOUT = 60

Log = Callable[[str], None]


# --------------------------------------------------------------------------
# Typed SDK-injection seam
# --------------------------------------------------------------------------
#
# Structural ``Protocol``s capturing ONLY the GCS attributes/methods this module
# actually touches. They keep ``core`` ``google.cloud``-free at import scope while
# giving the entry points a real type for the injected client/bucket/blob, and the
# in-memory test fake satisfies them structurally with no test changes.


class GCSBlobProtocol(Protocol):
    name: str
    crc32c: str | None
    content_type: str | None
    size: int | None
    generation: int | None

    def rewrite(self, source: Any, token: str | None = ...) -> tuple[str | None, int, int]: ...

    def reload(self) -> None: ...

    def delete(self, **kwargs: Any) -> None: ...

    def exists(self) -> bool: ...


class GCSBucketProtocol(Protocol):
    # Bucket-level metadata for the public-exposure pre-checks. ``iam_configuration``
    # and ``default_object_acl`` read from the bucket's cached ``_properties``, so a
    # bare ``client.bucket(name)`` reference exposes only DEFAULTS (UBLA reads False)
    # until ``reload()`` populates them from the live bucket — see
    # :func:`read_iam_configuration`.
    iam_configuration: Any
    default_object_acl: Any

    def blob(self, name: str) -> GCSBlobProtocol: ...

    def get_blob(self, name: str) -> GCSBlobProtocol | None: ...

    def list_blobs(self, *args: Any, **kwargs: Any) -> Iterable[GCSBlobProtocol]: ...

    def get_iam_policy(self, *args: Any, **kwargs: Any) -> Any: ...

    def set_iam_policy(self, policy: Any) -> Any: ...

    def reload(self, *args: Any, **kwargs: Any) -> None: ...


class GCSClientProtocol(Protocol):
    def bucket(self, name: str) -> GCSBucketProtocol: ...

    def list_blobs(self, *args: Any, **kwargs: Any) -> Iterable[GCSBlobProtocol]: ...


class MigrationError(RuntimeError):
    """A pre-condition gate failed and the operation refused to run (fail-closed)."""


@contextmanager
def _refuse_on_gcs_error(what: str):
    """Convert a GCS API failure during a pre-check READ into a fail-closed
    ``MigrationError``, so a transient/permission/not-found error refuses to stage
    with an actionable message + clean exit code rather than an uncaught traceback.
    A pre-check that cannot confirm the bucket is private must NOT proceed.
    """
    # google.api_core is imported lazily so core stays SDK-free at import scope.
    from google.api_core.exceptions import GoogleAPICallError

    try:
        yield
    except GoogleAPICallError as exc:
        raise MigrationError(
            f"refusing to stage: could not read {what} to verify the bucket is "
            f"private ({exc}). The Step 0 pre-checks need bucket IAM + metadata + "
            "default-object-ACL read (roles/storage.admin) — see the runbook."
        ) from exc


# --------------------------------------------------------------------------
# Key mapping
# --------------------------------------------------------------------------


def _remainder(key: str, prefix: str) -> str | None:
    """The part of ``key`` after ``prefix``, or ``None`` if ``key`` is exactly the
    prefix (a folder-placeholder object) or does not live under it."""
    if not key.startswith(prefix):
        return None
    rem = key[len(prefix):]
    return rem or None


def is_valid_demo_name(name: str) -> bool:
    """True iff ``name`` is a safe public demo recording-name segment.

    Mirrors the cloud function's ``is_valid_name`` so the migration's "reaches
    ``demo/``" set matches the function's "listable + playable" set — a name the
    function would reject is never promoted into the public namespace.
    """
    return ".." not in name and bool(_DEMO_NAME_RE.match(name))


def map_key(key: str, src_prefix: str, dst_prefix: str) -> str | None:
    """Swap ``src_prefix`` for ``dst_prefix``, preserving the FULL nested suffix.

    ``recordings/foo/screenshots/0.png`` -> ``import-review/foo/screenshots/0.png``.
    Returns ``None`` for a placeholder object equal to ``src_prefix``.
    """
    rem = _remainder(key, src_prefix)
    if rem is None:
        return None
    return dst_prefix + rem


# --------------------------------------------------------------------------
# Enumeration + per-object verification
# --------------------------------------------------------------------------


def iter_blobs(
    client: GCSClientProtocol, bucket_name: str, prefix: str
) -> Iterator[GCSBlobProtocol]:
    """Yield blobs under ``prefix``, skipping any folder-placeholder (key==prefix)."""
    for blob in client.list_blobs(bucket_name, prefix=prefix, timeout=_LIST_TIMEOUT):
        if _remainder(blob.name, prefix) is None:
            continue
        yield blob


def verify_match(src, dst) -> str | None:
    """Return ``None`` if ``dst`` faithfully copies ``src``, else a reason string.

    crc32c is the primary content-integrity signal (catches truncation that a
    count/size compare misses); content_type guards against a mangled copy that
    breaks website playback; md5_hash is a belt-and-suspenders check when both
    sides expose it.

    ``dst`` is a metadata-populated blob (from ``bucket.get_blob``) or ``None``
    when the object is absent — callers fetch existence + metadata in one GET, so
    this never issues its own round-trip.
    """
    if dst is None:
        return "destination missing"
    # Fail closed when the primary integrity signal is absent. A composite object
    # can expose ``crc32c=None``, and ``None == None`` must NOT read as "verified"
    # — this gate authorizes the irreversible U9 delete.
    if src.crc32c is None or dst.crc32c is None:
        return f"crc32c unavailable (src={src.crc32c!r} dst={dst.crc32c!r}) — cannot verify"
    if src.crc32c != dst.crc32c:
        return f"crc32c mismatch (src={src.crc32c!r} dst={dst.crc32c!r})"
    # crc32c is only 32 bits; a size cross-check is cheap defense-in-depth against
    # a collision before an irreversible delete.
    s_size = getattr(src, "size", None)
    d_size = getattr(dst, "size", None)
    if s_size is not None and d_size is not None and s_size != d_size:
        return f"size mismatch (src={s_size} dst={d_size})"
    if (src.content_type or None) != (dst.content_type or None):
        return f"content_type mismatch (src={src.content_type!r} dst={dst.content_type!r})"
    s_md5 = getattr(src, "md5_hash", None)
    d_md5 = getattr(dst, "md5_hash", None)
    if s_md5 and d_md5 and s_md5 != d_md5:
        return f"md5_hash mismatch (src={s_md5!r} dst={d_md5!r})"
    return None


def copy_blob(bucket: GCSBucketProtocol, src_blob: GCSBlobProtocol, dst_name: str) -> Any:
    """``rewrite()`` ``src_blob`` into ``dst_name``, following ``rewriteToken`` to
    completion, and return the reloaded destination blob.

    A large video object needs multiple ``rewrite()`` calls — one rewriteToken
    round-trip per server-side chunk. Stopping at the first call truncates it
    silently, so loop until the token clears.
    """
    dst_blob = bucket.blob(dst_name)
    token = None
    while True:
        token, _rewritten, _total = dst_blob.rewrite(src_blob, token=token)
        if token is None:
            break
    dst_blob.reload()
    return dst_blob


# --------------------------------------------------------------------------
# Bucket-IAM pre-check (private-staging precondition)
# --------------------------------------------------------------------------


def _policy_bindings(policy) -> list:
    """The bindings list off a google ``Policy`` (or a plain list, for the fake)."""
    return list(getattr(policy, "bindings", policy))


def find_public_iam_bindings(bucket: GCSBucketProtocol) -> list[tuple[str, str]]:
    """``(role, member)`` pairs granting ``allUsers``/``allAuthenticatedUsers`` anything.

    Any such binding can make ``import-review/`` objects world-readable by direct
    URL, so private staging cannot be guaranteed while one exists.
    """
    with _refuse_on_gcs_error("bucket IAM policy"):
        policy = bucket.get_iam_policy(requested_policy_version=3)
    found: list[tuple[str, str]] = []
    for binding in _policy_bindings(policy):
        role = binding.get("role")
        for member in binding.get("members", []):
            if member in PUBLIC_IAM_MEMBERS:
                found.append((role, member))
    return found


def remove_public_iam_bindings(
    bucket: GCSBucketProtocol, *, log: Log = print
) -> list[tuple[str, str]]:
    """Strip every ``allUsers``/``allAuthenticatedUsers`` member, write the policy
    back, and re-read to assert they are gone. Returns the removed ``(role, member)``
    pairs. Mutates bucket-wide access — opt-in only.
    """
    policy = bucket.get_iam_policy(requested_policy_version=3)
    new_bindings: list[dict] = []
    removed: list[tuple[str, str]] = []
    for binding in _policy_bindings(policy):
        members = set(binding.get("members", []))
        public = members & PUBLIC_IAM_MEMBERS
        for member in public:
            removed.append((binding.get("role"), member))
        members -= PUBLIC_IAM_MEMBERS
        if members:
            # Preserve every other field (notably ``condition`` on a v3
            # conditional binding) — rebuilding as bare {role, members} would
            # silently widen a conditional grant to unconditional.
            kept = dict(binding)
            kept["members"] = members
            new_bindings.append(kept)
    policy.bindings = new_bindings
    bucket.set_iam_policy(policy)
    still = find_public_iam_bindings(bucket)
    if still:
        raise MigrationError(f"public IAM bindings persist after removal: {still}")
    for role, member in removed:
        log(f"  removed public IAM binding: {member} : {role}")
    return removed


# --------------------------------------------------------------------------
# UBLA / object-ACL pre-check (the second public door — SCR-146)
# --------------------------------------------------------------------------
#
# Bucket IAM is not the only way GCS serves objects publicly. While Uniform
# Bucket-Level Access (UBLA) is DISABLED (the default on older buckets), legacy
# per-object and DEFAULT-object ACLs are live, and a public default object ACL
# makes every newly written object world-readable by direct URL — even with zero
# public IAM bindings. So a bucket that clears ``find_public_iam_bindings`` can
# still expose ``import-review/``. This pre-check closes that door, fail-closed.


def read_iam_configuration(bucket: GCSBucketProtocol) -> tuple[bool, str | None]:
    """``(uniform_bucket_level_access_enabled, public_access_prevention)``.

    Reloads the bucket FIRST: ``iam_configuration`` reads the bucket's cached
    ``_properties``, and a bare ``client.bucket(name)`` reference carries none — so
    without the reload UBLA would read ``False`` on a bucket we never inspected and
    fail-OPEN into the ACL path. The reload makes the read reflect live state.
    """
    with _refuse_on_gcs_error("bucket metadata (iam_configuration)"):
        bucket.reload()
    cfg = bucket.iam_configuration
    ubla = bool(getattr(cfg, "uniform_bucket_level_access_enabled", False))
    pap = getattr(cfg, "public_access_prevention", None)
    return ubla, pap


def find_public_default_object_acl(bucket: GCSBucketProtocol) -> list[tuple[str, str]]:
    """``(role, entity)`` grants of ``allUsers``/``allAuthenticatedUsers`` in the
    bucket's DEFAULT OBJECT ACL — the ACL every newly written object inherits.

    Only meaningful while UBLA is disabled (UBLA makes object ACLs inert); callers
    gate on :func:`read_iam_configuration` before consulting this.
    """
    acl = bucket.default_object_acl
    with _refuse_on_gcs_error("bucket default object ACL"):
        acl.reload()
    found: list[tuple[str, str]] = []
    for entry in acl:
        entity = entry.get("entity")
        if entity in PUBLIC_IAM_MEMBERS:
            found.append((entry.get("role"), entity))
    return found


def assert_no_public_object_acl(
    bucket: GCSBucketProtocol, bucket_name: str, *, log: Log = print
) -> None:
    """Refuse to stage if a default object ACL could make ``import-review/`` objects
    world-readable (SCR-146). Fail-closed; read-only (no auto-remediation).

    Public Access Prevention enforced -> public access is impossible bucket-wide;
    OK. UBLA enabled -> object ACLs are inert; OK. Otherwise (UBLA off AND PAP not
    enforced) a public DEFAULT object ACL would be inherited by every staged object,
    so refuse when one is present. A correctly-configured bucket (PAP enforced, UBLA
    on, or UBLA off with a private default ACL) is NOT refused.
    """
    ubla, pap = read_iam_configuration(bucket)
    # Public Access Prevention, when enforced, blocks ALL public access bucket-wide
    # (IAM *and* ACLs) and cannot be overridden — so no object can be served publicly
    # regardless of UBLA or any legacy ACL. It is the strongest lock and a
    # definitively-safe posture: honor it so a hardened bucket is never falsely
    # refused over a stale ACL GCS would refuse to serve anyway.
    if pap == PUBLIC_ACCESS_PREVENTION_ENFORCED:
        log(
            "UBLA pre-check: public_access_prevention=enforced — public access is "
            "blocked bucket-wide (IAM + ACLs), so object ACLs cannot expose staged "
            "objects regardless of UBLA. OK."
        )
        return
    if ubla:
        log(
            "UBLA pre-check: uniform_bucket_level_access enabled "
            f"(public_access_prevention={pap or 'unset'}) — object ACLs inert. OK."
        )
        return
    public_acl = find_public_default_object_acl(bucket)
    if public_acl:
        detail = ", ".join(f"{entity}:{role}" for role, entity in public_acl)
        raise MigrationError(
            f"refusing to stage: bucket {bucket_name!r} has Uniform Bucket-Level "
            "Access DISABLED, public_access_prevention not enforced, and a public "
            f"default object ACL ({detail}). Newly staged import-review/ objects "
            "would inherit it and be world-readable by direct URL despite no public "
            "IAM binding. Enable UBLA (gcloud storage buckets update "
            "--uniform-bucket-level-access) and set public_access_prevention="
            "enforced, then re-run (runbook Step 0)."
        )
    log(
        "UBLA pre-check: UBLA disabled and public_access_prevention not enforced, "
        f"but the default object ACL grants no public access (pap={pap or 'unset'}). "
        "OK — staged objects inherit a private default ACL; enabling UBLA and "
        "enforcing PAP is still recommended."
    )


# --------------------------------------------------------------------------
# Quiesce guard
# --------------------------------------------------------------------------


class RecordingState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    UNKNOWN = "unknown"


def probe_recording_state() -> RecordingState:
    """Best-effort: is a screencap recording in flight on THIS machine?

    Shells out to ``screencap status --json`` (the same CLI seam the macOS app
    uses) and reads ``is_recording``. Returns ``UNKNOWN`` if the CLI is absent or
    errors — an admin box need not have screencap installed, in which case the
    operator attests quiesce explicitly (``--confirm-quiesced``) per the runbook.
    """
    import shutil
    import subprocess

    exe = shutil.which("screencap")
    if not exe:
        return RecordingState.UNKNOWN
    try:
        proc = subprocess.run(
            [exe, "status", "--json"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return RecordingState.UNKNOWN
    if proc.returncode != 0:
        return RecordingState.UNKNOWN
    try:
        snap = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return RecordingState.UNKNOWN
    rec = snap.get("is_recording")  # status --json emits this at the top level
    if rec is True:
        return RecordingState.ACTIVE
    if rec is False:
        return RecordingState.IDLE
    return RecordingState.UNKNOWN


def assert_quiesced(
    *,
    confirm_quiesced: bool,
    probe: Callable[[], RecordingState] | None = None,
    log: Log = print,
) -> None:
    """Refuse the migration while a recording is in flight (a late completion
    sentinel from a pre-U2 engine can land a flat blob after enumeration).

    ACTIVE -> hard stop. UNKNOWN -> require an explicit operator attestation
    (``--confirm-quiesced``). IDLE / attested-UNKNOWN -> proceed.
    """
    # Resolve the probe at call time (not as a bound default) so a monkeypatch of
    # ``probe_recording_state`` takes effect and shim tests need not shell out.
    probe = probe or probe_recording_state
    state = probe()
    if state is RecordingState.ACTIVE:
        raise MigrationError(
            "a screencap recording is ACTIVE on this machine — refusing to "
            "migrate; a late completion sentinel could land a flat blob after "
            "enumeration. Stop all recordings and retry."
        )
    if state is RecordingState.UNKNOWN and not confirm_quiesced:
        raise MigrationError(
            "could not confirm no recording is in flight (screencap status "
            "unavailable). Confirm the flat write path is closed and no engine is "
            "running, then re-run with --confirm-quiesced (see the runbook)."
        )
    log(f"quiesce check: {state.value} (confirm_quiesced={confirm_quiesced})")


# --------------------------------------------------------------------------
# Copy outcomes + the stage / promote orchestration
# --------------------------------------------------------------------------


@dataclass
class CopyOutcome:
    src: str
    dst: str
    # planned (dry-run) | copied | recopied | skipped-verified | failed
    action: Literal["planned", "copied", "recopied", "skipped-verified", "failed"]
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.action in ("copied", "recopied", "skipped-verified")


@dataclass
class StageResult:
    outcomes: list[CopyOutcome]
    manifest: dict

    @property
    def failures(self) -> list[CopyOutcome]:
        return [o for o in self.outcomes if o.action == "failed"]

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass
class PromoteResult:
    outcomes: list[CopyOutcome]
    promoted: list[str]
    stripped_markers: list[str]
    missing_from_staging: list[str]
    invalid_names: list[str]
    # Allow-listed names present in staging only as marker blobs (no promotable
    # content) — neither promoted nor "missing"; surfaced distinctly (fix #8).
    marker_only: list[str]

    @property
    def failures(self) -> list[CopyOutcome]:
        return [o for o in self.outcomes if o.action == "failed"]

    @property
    def ok(self) -> bool:
        return not self.failures


def _copy_one(
    bucket: GCSBucketProtocol,
    src_blob: GCSBlobProtocol,
    dst_name: str,
    *,
    dry_run: bool,
    log: Log,
) -> CopyOutcome:
    """Idempotent, checksum-gated copy of one blob (shared by stage + promote).

    Trusts a checksum match to skip, re-copies on mismatch (never on existence
    alone), verifies the fresh copy, and never deletes anything. A GCS error on
    any single object is captured as a ``failed`` outcome (not propagated) so the
    caller's loop continues and still writes a manifest with an accurate failure
    count.
    """
    # google.api_core is imported lazily so core stays SDK-free at import scope.
    from google.api_core.exceptions import GoogleAPICallError

    try:
        # One GET fetches existence + metadata (None when absent) — no separate
        # exists()+reload() round-trip.
        dst = bucket.get_blob(dst_name)
        already = dst is not None
        mismatch = verify_match(src_blob, dst) if already else None
        if already and mismatch is None:
            log(f"  = {src_blob.name} -> {dst_name} (already present, verified) skip")
            return CopyOutcome(src_blob.name, dst_name, "skipped-verified")

        if dry_run:
            why = f"re-copy ({mismatch})" if already else "copy"
            log(f"  + [dry-run] {src_blob.name} -> {dst_name} ({why})")
            return CopyOutcome(src_blob.name, dst_name, "planned")

        fresh = copy_blob(bucket, src_blob, dst_name)
        mismatch = verify_match(src_blob, fresh)
    except GoogleAPICallError as exc:
        log(f"  ! {src_blob.name} -> {dst_name} FAILED (GCS error): {exc}")
        return CopyOutcome(src_blob.name, dst_name, "failed", f"error: {exc}")
    if mismatch is not None:
        log(f"  ! {src_blob.name} -> {dst_name} FAILED verify: {mismatch}")
        return CopyOutcome(src_blob.name, dst_name, "failed", mismatch)
    action = "recopied" if already else "copied"
    log(f"  + {src_blob.name} -> {dst_name} ({action}, verified)")
    return CopyOutcome(src_blob.name, dst_name, action)


def run_stage(
    *,
    client: GCSClientProtocol,
    bucket_name: str,
    dry_run: bool,
    remove_public_iam: bool = False,
    confirm_quiesced: bool = False,
    probe: Callable[[], RecordingState] | None = None,
    on_outcome: Callable[[CopyOutcome], None] | None = None,
    log: Log = print,
) -> StageResult:
    """U8 stage: copy flat ``recordings/`` into private ``import-review/``.

    Runs the bucket-IAM pre-check FIRST, then the UBLA / default-object-ACL
    pre-check (the second public door — SCR-146), then the quiesce guard, then a
    per-object verified rewrite. ``sessions/`` is EXCLUDED by design (demo is a
    recordings gallery; its index references retired flat paths). No source is ever
    deleted.

    ``on_outcome`` (optional) is invoked with each :class:`CopyOutcome` AS the
    copy loop runs, so a long interrupted run can leave incremental provenance
    (the shim appends a ``.partial.jsonl`` sidecar). The final ``.json`` manifest
    is unchanged.
    """
    bucket = client.bucket(bucket_name)

    # 1) Bucket-IAM pre-check — MUST run before anything touches objects.
    public = find_public_iam_bindings(bucket)
    if public:
        detail = ", ".join(f"{member}:{role}" for role, member in public)
        if remove_public_iam and not dry_run:
            log(f"bucket-IAM pre-check: public bindings present ({detail}) — removing:")
            remove_public_iam_bindings(bucket, log=log)
            log("bucket-IAM is now private (no allUsers/allAuthenticatedUsers).")
        else:
            hint = (
                "re-run with --remove-public-iam to strip them"
                if not dry_run
                else "dry-run does not mutate IAM; re-run staging with "
                "--remove-public-iam (without --dry-run) to strip them"
            )
            raise MigrationError(
                f"refusing to stage: bucket {bucket_name!r} has public read "
                f"bindings ({detail}). 'Private staging' cannot be guaranteed "
                f"while these exist — {hint}, or remove them out-of-band first."
            )
    else:
        log("bucket-IAM pre-check: no allUsers/allAuthenticatedUsers bindings. OK.")

    # 1b) UBLA / object-ACL pre-check — the SECOND public door (SCR-146). Read-only;
    # refuses fail-closed if a public default object ACL would expose staged objects
    # while UBLA is off. Runs on dry-run too (it never mutates anything).
    assert_no_public_object_acl(bucket, bucket_name, log=log)

    # 2) Quiesce guard.
    assert_quiesced(confirm_quiesced=confirm_quiesced, probe=probe, log=log)

    # 3) Enumerate flat recordings/ and stage each (sessions/ EXCLUDED).
    outcomes: list[CopyOutcome] = []
    objects: dict[str, dict] = {}
    for src_blob in iter_blobs(client, bucket_name, FLAT_RECORDINGS_PREFIX):
        dst_name = map_key(src_blob.name, FLAT_RECORDINGS_PREFIX, STAGING_PREFIX)
        outcome = _copy_one(bucket, src_blob, dst_name, dry_run=dry_run, log=log)
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)
        objects[src_blob.name] = {
            "dst": dst_name,
            "action": outcome.action,
            "verified": outcome.verified,
            "crc32c": getattr(src_blob, "crc32c", None),
            "reason": outcome.reason,
        }

    manifest = {
        "bucket": bucket_name,
        "src_prefix": FLAT_RECORDINGS_PREFIX,
        "dst_prefix": STAGING_PREFIX,
        "dry_run": dry_run,
        "object_count": len(objects),
        "objects": objects,
    }
    result = StageResult(outcomes=outcomes, manifest=manifest)
    log(
        f"stage summary: {len(outcomes)} objects, {len(result.failures)} "
        f"failed verify, dry_run={dry_run}"
    )
    return result


def run_promote(
    *,
    client: GCSClientProtocol,
    bucket_name: str,
    allow_list: Iterable[str],
    dry_run: bool,
    log: Log = print,
) -> PromoteResult:
    """U8 promote: copy founder-cleared recordings from ``import-review/`` into the
    public ``demo/`` namespace, stripping ``_unlisted``/``show_on_website`` markers.

    Only recordings whose name is in ``allow_list`` are promoted. Marker blobs are
    dropped (never copied) so the recording appears in the marker-blind
    ``demo-list``. No source is deleted.

    Unlike ``run_stage``/``run_decommission``, promotion runs **no quiesce or
    IAM gate**, by design: it reads the already-staged ``import-review/`` namespace
    (which the flat-write quiesce race cannot touch) and writes the deliberately
    public ``demo/`` namespace (so a public-IAM precondition does not apply). The
    review/consent gate that DOES guard promotion is the operator-supplied
    ``allow_list`` itself.
    """
    bucket = client.bucket(bucket_name)
    allow = set(allow_list)
    outcomes: list[CopyOutcome] = []
    promoted: set[str] = set()
    stripped: list[str] = []
    invalid: set[str] = set()
    # Names seen at all in staging (any blob, marker or not) vs names seen with at
    # least one PROMOTABLE (non-marker) content blob. A recording present only as
    # marker blobs is "seen" (so not "missing") but produces no demo object, so it
    # must be surfaced distinctly rather than silently dropped (fix #8).
    seen_names: set[str] = set()
    content_seen: set[str] = set()

    for src_blob in iter_blobs(client, bucket_name, STAGING_PREFIX):
        rem = _remainder(src_blob.name, STAGING_PREFIX)  # {name}/{suffix}; never None
        parts = rem.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue  # bare-name placeholder, no file suffix
        name, suffix = parts
        # Hard guard: never promote the retired ``sessions`` namespace into public
        # ``demo/``, even if an allow-list typo names it. ``sessions`` has no demo
        # representation; promoting it would publish retired session data (fix #11).
        if name == "sessions":
            log(f"  ! refusing to promote reserved name 'sessions' ({src_blob.name})")
            continue
        seen_names.add(name)  # name present in staging (marker or content)
        if name not in allow:
            continue
        if not is_valid_demo_name(name):
            # The demo handlers reject this name, so a promoted copy would be
            # hidden-but-public. Skip it rather than create an unservable object.
            if name not in invalid:
                invalid.add(name)
                log(f"  ! {name!r} fails the demo name guard — NOT promoting (would be hidden-but-public)")
            continue
        if suffix in STRIPPED_MARKERS:
            stripped.append(src_blob.name)
            log(f"  - {src_blob.name} (stripping superseded marker '{suffix}')")
            continue
        content_seen.add(name)  # a real, promotable (non-marker) blob for this name
        dst_name = map_key(src_blob.name, STAGING_PREFIX, DEMO_PREFIX)
        outcome = _copy_one(bucket, src_blob, dst_name, dry_run=dry_run, log=log)
        outcomes.append(outcome)
        if outcome.verified:  # excludes dry-run "planned" — matches run_decommission
            promoted.add(name)

    missing = sorted(allow - seen_names - invalid)
    for name in missing:
        log(f"  ! allow-listed recording not found in staging: {name!r}")
    # Allow-listed, name-valid, present in staging, but ONLY as marker blobs — no
    # demo object was produced. Neither "promoted" nor "missing"; surfaced so the
    # shim can exit non-zero (same treatment as missing).
    marker_only = sorted(
        n for n in (allow & seen_names) - invalid if n not in content_seen
    )
    for name in marker_only:
        log(f"  ! allow-listed recording has only marker blobs in staging (nothing to promote): {name!r}")
    result = PromoteResult(
        outcomes=outcomes,
        promoted=sorted(promoted),
        stripped_markers=stripped,
        missing_from_staging=missing,
        invalid_names=sorted(invalid),
        marker_only=marker_only,
    )
    log(
        f"promote summary: {len(promoted)} recordings, {len(stripped)} markers "
        f"stripped, {len(missing)} missing, {len(marker_only)} marker-only, "
        f"{len(invalid)} invalid-name skipped, "
        f"{len(result.failures)} failed verify, dry_run={dry_run}"
    )
    return result


# --------------------------------------------------------------------------
# Decommission (U9) — the irreversible step
# --------------------------------------------------------------------------


@dataclass
class DeleteOutcome:
    src: str
    # planned (dry-run) | deleted | kept
    action: Literal["planned", "deleted", "kept"]
    reason: str = ""


@dataclass
class RescanResult:
    remaining: list[str]
    new_blobs: list[str]  # appeared post-enumeration (missed-write race) — surfaced

    @property
    def empty(self) -> bool:
        return not self.remaining


@dataclass
class DecommissionResult:
    outcomes: list[DeleteOutcome]
    rescan: RescanResult | None

    @property
    def deleted(self) -> list[DeleteOutcome]:
        return [o for o in self.outcomes if o.action == "deleted"]

    @property
    def kept(self) -> list[DeleteOutcome]:
        return [o for o in self.outcomes if o.action == "kept"]

    @property
    def kept_on_error(self) -> list[DeleteOutcome]:
        """Sources KEPT because a GCS error (not a clean gate refusal) interrupted
        their delete — the run is partial/incomplete and the shim exits non-zero."""
        return [o for o in self.kept if o.reason.startswith("error: ")]


def run_decommission(
    *,
    client: GCSClientProtocol,
    bucket_name: str,
    include_sessions: bool,
    dry_run: bool,
    sessions_backup_confirmed: bool = False,
    confirm_quiesced: bool = False,
    probe: Callable[[], RecordingState] | None = None,
    log: Log = print,
) -> DecommissionResult:
    """U9: delete the flat source blobs after the website cutover is verified live.

    Each ``recordings/`` delete is gated on a FRESH live re-verify of its
    ``import-review/`` staging copy (crc32c + content_type) — the manifest is a
    hint, the live re-confirm is proof; a source whose staging copy is missing or
    mismatched is KEPT, never deleted. The verified delete is additionally
    generation-pinned (``if_generation_match``) to the generation captured at
    enumeration, so a racing overwrite between verify and delete is refused (the
    source is KEPT) rather than silently destroying newer bytes. A GCS error on a
    single delete also KEEPS that source (recorded as ``kept`` with reason
    ``error: …``) so the run still produces a complete deleted/kept accounting.

    ``sessions/`` is retired data with no staging copy, so it is deleted only
    behind an explicit ``include_sessions`` opt-in, AND only once the operator has
    attested (``sessions_backup_confirmed``) that the zkairdrop session archive is
    accessible — there is no staging copy to fall back on. A post-run re-scan
    asserts the prefixes are empty and surfaces any new flat blob (a missed-write
    race) rather than deleting it.
    """
    # Lazy SDK imports — keep core ``google.cloud``-free at import scope.
    from google.api_core.exceptions import GoogleAPICallError, PreconditionFailed

    bucket = client.bucket(bucket_name)
    assert_quiesced(confirm_quiesced=confirm_quiesced, probe=probe, log=log)

    # Sessions are irreplaceable (no staging copy). Refuse the sessions delete loop
    # unless the operator has explicitly attested the zkairdrop archive is
    # accessible — fail-closed, BEFORE any delete touches the bucket.
    if include_sessions and not sessions_backup_confirmed:
        raise MigrationError(
            "pass --sessions-backup-confirmed after verifying the zkairdrop archive "
            "is accessible — sessions/ has no staging copy to fall back on"
        )

    outcomes: list[DeleteOutcome] = []
    handled: set[str] = set()

    # recordings/ — gated per-object on a verified staging copy.
    for src_blob in iter_blobs(client, bucket_name, FLAT_RECORDINGS_PREFIX):
        handled.add(src_blob.name)
        # Pin the source generation observed at enumeration; the delete below
        # refuses (PreconditionFailed) if the source was overwritten since.
        gen = getattr(src_blob, "generation", None)
        staging_name = map_key(src_blob.name, FLAT_RECORDINGS_PREFIX, STAGING_PREFIX)
        # One GET fetches the staging copy + its metadata (None when absent).
        staging = bucket.get_blob(staging_name)
        if staging is None:
            outcomes.append(DeleteOutcome(src_blob.name, "kept", "no staging copy"))
            log(f"  ✗ KEEP {src_blob.name} (no staging copy at {staging_name})")
            continue
        mismatch = verify_match(src_blob, staging)
        if mismatch is not None:
            outcomes.append(
                DeleteOutcome(src_blob.name, "kept", f"staging unverified: {mismatch}")
            )
            log(f"  ✗ KEEP {src_blob.name} (staging unverified: {mismatch})")
            continue
        if dry_run:
            outcomes.append(DeleteOutcome(src_blob.name, "planned", "verified-in-staging"))
            log(f"  - [dry-run] DELETE {src_blob.name} (verified-in-staging)")
            continue
        try:
            src_blob.delete(if_generation_match=gen)
        except PreconditionFailed:
            # The source changed between verify and delete (generation mismatch).
            # The staging copy we verified no longer matches the live source —
            # KEEP it; deleting would destroy un-staged newer bytes.
            outcomes.append(
                DeleteOutcome(
                    src_blob.name, "kept", "source changed since enumeration (generation mismatch)"
                )
            )
            log(f"  ✗ KEEP {src_blob.name} (source changed since enumeration — generation mismatch)")
            continue
        except GoogleAPICallError as exc:
            outcomes.append(DeleteOutcome(src_blob.name, "kept", f"error: {exc}"))
            log(f"  ✗ KEEP {src_blob.name} (GCS error during delete: {exc})")
            continue
        outcomes.append(DeleteOutcome(src_blob.name, "deleted", "verified-in-staging"))
        log(f"  - DELETE {src_blob.name} (verified-in-staging)")

    # sessions/ — retired, not staged anywhere; opt-in + backup-attestation only.
    if include_sessions:
        for src_blob in iter_blobs(client, bucket_name, FLAT_SESSIONS_PREFIX):
            handled.add(src_blob.name)
            if dry_run:
                outcomes.append(DeleteOutcome(src_blob.name, "planned", "retired session"))
                log(f"  - [dry-run] DELETE {src_blob.name} (retired session)")
                continue
            try:
                src_blob.delete()
            except GoogleAPICallError as exc:
                outcomes.append(DeleteOutcome(src_blob.name, "kept", f"error: {exc}"))
                log(f"  ✗ KEEP {src_blob.name} (GCS error during delete: {exc})")
                continue
            outcomes.append(DeleteOutcome(src_blob.name, "deleted", "retired session"))
            log(f"  - DELETE {src_blob.name} (retired session)")

    rescan = None
    if not dry_run:
        rescan = _rescan(client, bucket_name, include_sessions, handled=handled, log=log)
    return DecommissionResult(outcomes, rescan)


def _rescan(
    client: GCSClientProtocol,
    bucket_name: str,
    include_sessions: bool,
    *,
    handled: set[str],
    log: Log,
) -> RescanResult:
    """Post-decommission re-scan: report leftovers and flag any blob not seen at
    delete time (a write that raced past U8's quiesce)."""
    prefixes = [FLAT_RECORDINGS_PREFIX]
    if include_sessions:
        prefixes.append(FLAT_SESSIONS_PREFIX)
    remaining: list[str] = []
    new_blobs: list[str] = []
    for prefix in prefixes:
        for blob in iter_blobs(client, bucket_name, prefix):
            remaining.append(blob.name)
            if blob.name not in handled:
                new_blobs.append(blob.name)
                log(f"  ⚠ NEW flat blob since enumeration (not deleted): {blob.name}")
    if remaining:
        log(
            f"re-scan: {len(remaining)} object(s) still under the flat prefixes "
            f"({len(new_blobs)} new since enumeration); R11 not fully satisfied "
            "until these are resolved."
        )
    else:
        scope = "recordings/ + sessions/" if include_sessions else "recordings/"
        log(f"re-scan: flat {scope} EMPTY. R11 satisfied.")
    return RescanResult(remaining, new_blobs)


# --------------------------------------------------------------------------
# Manifest IO + live-client construction (kept thin; the SDK import is lazy)
# --------------------------------------------------------------------------


def write_manifest(path: str | Path, manifest: dict, *, log: Log = print) -> None:
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    log(f"wrote manifest: {path} ({manifest.get('object_count', 0)} objects)")


def load_allow_list(path: str | Path) -> list[str]:
    """Read a founder allow-list file: one recording name per line, ``#`` comments
    and blank lines ignored."""
    names: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def add_common_args(parser) -> None:
    """Add the ``--bucket`` / ``--project`` / ``--dry-run`` flags every shim shares."""
    parser.add_argument("--bucket", required=True, help="GCS bucket holding the namespace")
    parser.add_argument("--project", default=None, help="GCP project for the client (optional)")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; mutate nothing")


def build_client(project: str | None = None):
    """Construct a live GCS client. The ``google.cloud`` import is deferred to here
    so the module stays SDK-free for the offline test suite."""
    from google.cloud import storage

    return storage.Client(project=project) if project else storage.Client()
