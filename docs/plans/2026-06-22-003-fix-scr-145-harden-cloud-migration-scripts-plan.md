---
title: "fix: Harden cloud-migration scripts (GCS error breadth, timeouts, rewrite cap, decommission audit log)"
type: fix
status: completed
date: 2026-06-22
deepened: 2026-06-22
---

# fix: Harden cloud-migration scripts (SCR-145)

## Summary

Hardens the SCR-139 cloud-migration admin scripts (`scripts/cloud_migration/core.py` + the three CLI shims) along the reliability lens surfaced by the SCR-139 code review and deferred to this ticket. Four robustness changes: (1) widen the per-object transient-error catch so a `RetryError` from exhausted SDK retries no longer aborts a multi-thousand-object loop, and close the one copy/verify call still left unwrapped; (2) pass an explicit `timeout=` to every per-object GCS call so none can hang indefinitely; (3) bound the `copy_blob` rewriteToken loop with a generous iteration cap; (4) give the irreversible decommission step a durable, crash-safe `.jsonl` audit log. The scripts are already idempotent and re-runnable, so this is robustness/provenance hardening, not a correctness fix — no migration behavior or safety invariant changes.

---

## Problem Frame

The migration scripts are correct and safe today (a mid-run crash is recoverable because every step is idempotent and re-runnable), but the SCR-139 reliability review found rough edges that hurt large-run robustness and provenance:

- A single transient GCS error (429/503) in a copy or delete loop can abort the whole run, discarding the in-memory outcomes so the stage manifest is never written — and the existing per-object catch is narrower than the failure it is meant to absorb.
- Per-object GCS calls other than `list_blobs` carry no timeout and can hang forever on an unresponsive endpoint.
- The `copy_blob` rewriteToken loop is unbounded — a stuck token would hang the process.
- The irreversible decommission step records deletions only in memory, so a crash mid-run leaves no on-disk record of what was already deleted.

These were deliberately deferred from SCR-139 (the scripts are safe to crash and re-run); SCR-145 is the follow-up that closes them. See origin: [Linear SCR-145](https://linear.app/zk-email/issue/SCR-145/harden-cloud-migration-scripts-gcs-error-handling-timeouts-rewrite-cap).

---

## Requirements

- R1. Transient GCS errors (including `RetryError`, raised when the SDK exhausts its own retries on persistent 429/503) on **any single** copy, delete, or staging-GET are caught **per-object**, recorded as a `failed`/`kept` outcome, and the loop continues — so the run completes, writes its manifest, and surfaces the failure via `result.ok` / `result.kept_on_error`. (ticket item #1, incl. the breadth widening confirmed in planning)
- R2. Every per-object GCS call — `rewrite()`, `reload()`, `get_blob()`, `delete()` — passes an explicit `timeout=`, so no call can hang indefinitely. (ticket item #2)
- R3. The `copy_blob` rewriteToken loop is bounded by a **generous** iteration cap **and a wall-clock deadline** that cannot false-trigger on legitimate large cross-region / cross-storage-class rewrites; a stuck or slow token fails that **one** object (recorded `failed`, re-run retries) rather than hanging the process indefinitely. (ticket item #3)
- R4. The decommission step writes a **durable, crash-safe** audit log (one JSON line per delete/keep, flushed per line) so a crash mid-run leaves an auditable record of exactly what was deleted. (ticket item #4)
- R5. Re-runs stay idempotent and every existing safety invariant is preserved unchanged: generation-pinned delete, fresh-live-reverify gate before delete, quiesce/IAM pre-checks, sessions backup attestation, and the JSON manifest format.
- R6. The offline in-memory GCS fake is extended to inject raises / record timeouts / force a stuck rewriteToken, so all of the above are provable without the `google-cloud-storage` SDK or any GCP credential.
- R7. The runbook and the decommission shim docs document the new audit-log artifact and the reliability behavior.

---

## Scope Boundaries

- **No change to migration semantics or safety gates.** The copy/verify/delete state machine, the crc32c+content_type integrity model, the IAM/quiesce pre-checks, the generation-pinned delete, and the sessions backup attestation are all unchanged. This ticket only changes *how robustly* errors and hangs are absorbed and recorded.
- **`list_blobs` / enumeration errors are out of scope.** Listing already carries `_LIST_TIMEOUT`. A list failure means enumeration cannot proceed at all — there is nothing partial to preserve, so aborting (then re-running idempotently) is the correct behavior, not a per-object `failed`. Not changed.
- **No new retry logic.** The SDK already retries idempotent operations internally; this ticket does not add a custom retry/backoff layer. It makes the *exhaustion* of that retry survivable per-object, not retried harder.
- **No CLI contract changes** beyond one additive optional flag on the decommission shim (`--audit-log`, with a sensible default). Existing flags, exit codes, and manifest formats are untouched.

### Deferred to Follow-Up Work

- IAM pre-check call timeouts (`get_iam_policy` / `set_iam_policy` in `find_public_iam_bindings` / `remove_public_iam_bindings`): one-shot gates that fail fast before any object work, so a hang there is far less damaging than a mid-loop hang. If desired, add `timeout=` there in a separate small follow-up — outside this ticket's item-#2 scope, which names `rewrite`/`get_blob`/`delete` (this plan additionally times out `reload()`, which shares the copy round-trip — see U2).
- Hardening the existing stage `.partial.jsonl` sidecar's file permissions: it has the same umask-dependent exposure this plan fixes for the new decommission audit log (see U4), but it is pre-existing and not in this ticket's scope — fix it in a small separate follow-up if desired.
- Progress-stall detection for the rewrite loop (failing only when *rewritten bytes* stop advancing, rather than a flat iteration cap): a more precise guard than R3's generous cap. Noted as an optional refinement; the cap satisfies the ticket.

---

## Context & Research

### Relevant Code and Patterns

- `scripts/cloud_migration/core.py` — the SDK-injectable migration core. Key symbols this plan touches: `_copy_one` (the shared per-object copy, already wraps its body in `try/except GoogleAPICallError`), `copy_blob` (the `while True:` rewriteToken loop + `reload()`), `run_decommission` (the two delete loops + the staging `get_blob`), and the module constant `_LIST_TIMEOUT = 60`.
- **Existing `on_outcome` + `.partial.jsonl` pattern (the template for R4).** `run_stage` already accepts `on_outcome: Callable[[CopyOutcome], None] | None` and invokes it as the copy loop runs; the stage shim `scripts/migrate_flat_to_staging.py` (≈ lines 63–92) opens a `.partial.jsonl` sidecar, defines `_record(outcome)` that writes `json.dumps(dataclasses.asdict(outcome))` + `flush()` per line, passes it as `on_outcome`, and closes it in a `finally`. R4 mirrors this exactly for `run_decommission` / the decommission shim.
- **Existing per-object GCS-error test (the template for R1/R6).** `tests/test_cloud_migration.py::test_decommission_keeps_source_on_gcs_error_and_continues` monkeypatches `FakeBlob.delete` to raise `GoogleAPICallError` and asserts the object is KEPT with an `error:` reason while the loop continues. The new error-injection seam is additive and must keep this test green.
- **The offline GCS fake** (`tests/test_cloud_migration.py`, `FakeStore`/`FakeBlob`/`FakeBucket`/`FakeClient`): already models multi-call `rewrite()` via `rewrite_plan`, crc corruption via `corrupt_on_rewrite`, and `PreconditionFailed` on a generation mismatch in `delete`. `FakeClient.list_blobs` already accepts `timeout=`; `FakeBlob.rewrite/reload/delete` and `FakeBucket.get_blob` do **not** yet accept `timeout=` (R2 must add it or those calls will `TypeError`).
- The three thin CLI shims: `scripts/migrate_flat_to_staging.py`, `scripts/promote_staging_to_demo.py` (note its `_gcs_call_error()` helper, also narrowed to `GoogleAPICallError`), `scripts/decommission_flat_namespace.py`.

### Institutional Learnings

- No `docs/solutions/` entry covers GCS migration error handling specifically. The repo-wide data-loss discipline in `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md` (never-delete-without-fresh-remote-confirm, tri-state outcomes) is the same philosophy these scripts already encode; this ticket preserves it.

### External References

- **Verified directly against the installed SDK** (`google-cloud-storage`, `google-api-core`):
  - Exception hierarchy: `RetryError(GoogleAPIError)` is **NOT** a subclass of `GoogleAPICallError`. `GoogleAPICallError`, `ServerError`, `ClientError`, `ServiceUnavailable` (503), `TooManyRequests` (429), `DeadlineExceeded` all are. So `except GoogleAPICallError` catches a transient 503/429 itself but **misses the `RetryError`** the SDK raises once it has exhausted its *own* retries on a persistent 503/429 — precisely the multi-thousand-object failure mode the ticket targets. Widening to `except GoogleAPIError` covers both. `PreconditionFailed` is a `GoogleAPICallError` subclass, so its `except` must stay **ordered before** the broad catch.
  - All four methods already accept `timeout=` (default `60`): `Blob.rewrite(..., timeout=60, retry=…)`, `Blob.reload(..., timeout=60, …)`, `Blob.delete(..., timeout=60, …)`, `Bucket.get_blob(..., timeout=60, …)`. R2 is a parameter pass, not a signature workaround.

---

## Key Technical Decisions

- **Widen `GoogleAPICallError` → `GoogleAPIError` everywhere the per-object loops catch (not a custom retry layer).** Rationale: `RetryError` (the post-retry-exhaustion signal) is a `GoogleAPIError` but not a `GoogleAPICallError`; the ticket's own wording says `GoogleAPIError`. `GoogleAPIError` is still a specific base — it will not swallow programming errors like `AttributeError`/`KeyError`. Keep `except PreconditionFailed` ordered first in `run_decommission` so generation-mismatch keeps its distinct "source changed since enumeration" reason and is not mislabeled `error:`.
- **A stuck rewriteToken fails *one object*, not the run.** The cap-exceeded condition must be caught by `_copy_one` and recorded as a `failed` outcome (so the manifest is still written and a re-run retries), **not** raised as a `MigrationError` (which is reserved for fail-closed gate refusals that *should* abort). Use a small dedicated exception (e.g. `RewriteLimitExceeded(RuntimeError)`) added to `_copy_one`'s `except (GoogleAPIError, RewriteLimitExceeded)`. (Directional — an implementer may instead subclass a GCS error; the binding requirement is per-object-failed-not-abort.)
- **Single op-timeout knob.** Add one module constant (e.g. `_OP_TIMEOUT = 60`, mirroring `_LIST_TIMEOUT`) and pass it to all four call types, making the timeout explicit and tunable in one place even though it equals the current SDK default.
- **Reuse the stage shim's audit pattern for decommission** rather than inventing a new format: add `on_outcome` to `run_decommission`, write a flushed `.jsonl` from the shim. Consistency with `migrate_flat_to_staging.py` is the point — same `dataclasses.asdict` + per-line flush shape, applied to `DeleteOutcome`.
- **Generous cap, documented — bounded in time, not just iterations.** Each rewrite iteration copies one server-side chunk; a multi-GB cross-region object completes in far fewer than a four-figure iteration count. Set the cap well above any realistic object (e.g. `_REWRITE_MAX_ITERS = 10_000`) and comment the reasoning so it reads as a hang-guard, not a size limit. Because each iteration can itself take up to `_OP_TIMEOUT`, the iteration cap alone does **not** bound elapsed time (`10_000 × 60s` ≈ 7 days) — pair it with a wall-clock deadline (U3) so the "can never hang" guarantee holds against a slow-token endpoint, not only a fast-spinning one.

---

## Open Questions

### Resolved During Planning

- **Is item #1 already done (commit `d1d476e2`)?** Partially — the per-object catch exists but is narrowed to `GoogleAPICallError` (misses `RetryError`) and one call (`run_decommission`'s staging `get_blob`) is still unwrapped. Resolved with the user: **widen to `GoogleAPIError` across all loops + the promote shim, and wrap the staging `get_blob`.**
- **Do the GCS methods support `timeout=`?** Yes, all four accept it (verified against the installed SDK). Item #2 is a clean parameter pass plus a fake-signature update.
- **What audit format / wiring for item #4?** Mirror the existing `run_stage` `on_outcome` + `.partial.jsonl` sidecar pattern; emit `DeleteOutcome` rows.

### Deferred to Implementation

- **Exact `_REWRITE_MAX_ITERS` and `_REWRITE_MAX_WALL` values** (and whether to additionally track byte-progress): pick a generous iteration cap and an acceptable wall-clock budget during implementation; progress-tracking is an optional refinement (see Deferred to Follow-Up Work).
- **Whether the decommission audit log writes on `--dry-run`** (to a `.dryrun.jsonl` sidecar, for format preview) or live-only: lean to writing a dry-run sidecar for symmetry with the stage shim; confirm against the fake test once the shim is wired. If written, ensure an operator cannot mistake the dry-run `planned` rows for an actual deletion ledger — the `.dryrun.jsonl` suffix plus the `action: "planned"` field distinguish them.
- **Audit-log open mode on re-run — append/timestamp vs. truncate.** The stage shim opens its sidecar with `"w"` (truncate), which is safe there because staging is fully re-creatable. For the *irreversible* decommission audit, a re-run after a partial failure must not destroy the prior deletion record: lean to a per-run timestamped filename or `O_APPEND` so each run's deletions are preserved. Decide when wiring the shim.
- **Exact `RewriteLimitExceeded` type/placement** vs. reusing an existing exception: decide when wiring `_copy_one`'s `except` (it must be listed explicitly — a bare `except RuntimeError` would also swallow `MigrationError`, which is reserved for fail-closed aborts).

---

## Implementation Units

### U1. Widen the transient-error catch to `GoogleAPIError` and close the unwrapped `get_blob`

**Goal:** A `RetryError` (or any `GoogleAPIError`) on any single copy, delete, or staging-GET is absorbed per-object and the loop continues; no per-object GCS error can abort the whole run.

**Requirements:** R1, R5, R6

**Dependencies:** None

**Files:**
- Modify: `scripts/cloud_migration/core.py` (`_copy_one`, `run_decommission`)
- Modify: `scripts/promote_staging_to_demo.py` (`_gcs_call_error` → `GoogleAPIError`)
- Test: `tests/test_cloud_migration.py`

**Approach:**
- `_copy_one`: change the lazy import and `except` from `GoogleAPICallError` to `GoogleAPIError`. Its `try` already spans `get_blob` + `copy_blob` + `verify_match`, so no structural change.
- `run_decommission`: import `GoogleAPIError` (keep `PreconditionFailed`). Wrap the staging `bucket.get_blob(staging_name)` (currently unwrapped, ≈ core.py:737) so a `GoogleAPIError` there records `DeleteOutcome(src, "kept", f"error: {exc}")` and `continue`s (consistent with the delete-error path → counts as `kept_on_error` → shim exits non-zero). In the recordings/ delete: keep `except PreconditionFailed` **first**, then `except GoogleAPIError`. In the sessions/ delete: widen its `except` to `GoogleAPIError`.
- `promote_staging_to_demo.py`: `_gcs_call_error()` returns `GoogleAPIError` (rename to `_gcs_error()` and update the one call site, optional but clearer); update its docstring.
- **Fake error-injection seam (R6):** add to `FakeStore` keyed-by-name maps such as `raise_on_get_blob`, `raise_on_rewrite`, `raise_on_delete` (`dict[str, BaseException]`); have `FakeBucket.get_blob`, `FakeBlob.rewrite`, `FakeBlob.delete` raise the configured exception when their name is present. Additive — must not break the existing monkeypatch-based test.

**Execution note:** Add the fake error-injection seam and the `RetryError` tests first (they fail against the current narrow catch), then widen the catch to green them.

**Patterns to follow:**
- `tests/test_cloud_migration.py::test_decommission_keeps_source_on_gcs_error_and_continues` (per-object keep-and-continue assertion shape).
- The existing `corrupt_on_rewrite` / `rewrite_plan` injection maps on `FakeStore` (same additive-seam style).

**Test scenarios:**
- Error path: `RetryError` raised during a stage `_copy_one` rewrite → that object recorded `failed`, loop continues, manifest written, `result.ok` is False. (Regression the widening fixes — previously `RetryError` escaped.)
- Error path: `RetryError` raised during a `recordings/` `delete()` → source KEPT with `error:` reason, loop continues, `result.kept_on_error` non-empty.
- Error path: `GoogleAPIError` (e.g. `ServiceUnavailable`) raised by the staging `get_blob` in decommission → source KEPT with `error:` reason; the next object is still processed. (New: closes the unwrapped gap.)
- Edge case: `PreconditionFailed` during a `recordings/` delete is still caught distinctly — outcome reason is "source changed since enumeration (generation mismatch)", **not** `error:` (proves the `except` ordering).
- Error path (promote): a `GoogleAPIError` during a promote copy → `_copy_one` records `failed` → `result.ok` False → shim exits 1; and a top-level `GoogleAPIError` is caught by the shim's widened `_gcs_error()`.
- Regression: `test_decommission_keeps_source_on_gcs_error_and_continues` (raises `GoogleAPICallError`) stays green under the widened catch.

**Verification:**
- `grep -n "GoogleAPICallError" scripts/cloud_migration/core.py scripts/promote_staging_to_demo.py` returns no per-object catch (only `PreconditionFailed` remains as the deliberately-specific case).
- A `RetryError` on any single object no longer aborts the loop; the run completes with accurate `failed`/`kept` accounting.

---

### U2. Pass explicit `timeout=` to every per-object GCS call

**Goal:** No per-object GCS call can hang indefinitely on an unresponsive endpoint.

**Requirements:** R2, R6

**Dependencies:** U1 (same functions; sequencing after U1 keeps the diffs clean)

**Files:**
- Modify: `scripts/cloud_migration/core.py` (`copy_blob`, `_copy_one`, `run_decommission`)
- Test: `tests/test_cloud_migration.py`

**Approach:**
- Add a module constant `_OP_TIMEOUT = 60` next to `_LIST_TIMEOUT`.
- Pass `timeout=_OP_TIMEOUT` to: `copy_blob`'s `dst_blob.rewrite(src_blob, token=token, timeout=…)` and `dst_blob.reload(timeout=…)`; `_copy_one`'s `bucket.get_blob(dst_name, timeout=…)`; `run_decommission`'s `bucket.get_blob(staging_name, timeout=…)` and both `src_blob.delete(..., timeout=…)` calls (alongside the existing `if_generation_match=gen`).
- **Fake (R6):** add `timeout=None` to the signatures of `FakeBlob.rewrite`, `FakeBlob.reload`, `FakeBlob.delete`, and `FakeBucket.get_blob` (else they `TypeError` once core passes the kwarg). Optionally record the received `timeout` (e.g. a `FakeStore.timeouts` list) so a test can assert it is propagated. Note `reload()` is included even though the ticket lists only rewrite/get_blob/delete — it is part of the same copy round-trip and would otherwise still hang.
- **In-file Protocol parity:** add a `timeout` parameter to the structural Protocols that type these calls — `GCSBlobProtocol.rewrite` / `.reload` and `GCSBucketProtocol.get_blob` in `core.py` — so the injection seam's declared surface matches the new call sites. No type checker enforces this today (cosmetic, won't fail CI), but it keeps the Protocol honest for readers and future tooling.

**Patterns to follow:**
- `iter_blobs` already passes `timeout=_LIST_TIMEOUT` to `list_blobs` — mirror that constant + call-site style.

**Test scenarios:**
- Behavior: extend the fake to record the `timeout` per call; assert `rewrite`, `reload`, `get_blob`, and `delete` each receive a non-None `timeout` equal to `_OP_TIMEOUT` on a normal stage + decommission run. (Targets the exact changed code path.)
- Happy path / regression: the full existing offline suite stays green — every existing test now exercises the fake's new `timeout=` kwarg acceptance, proving the parameter add didn't break the copy/verify/delete flows.

**Verification:**
- `grep -nE "\.rewrite\(|\.reload\(|\.get_blob\(|\.delete\(" scripts/cloud_migration/core.py` shows every call carrying `timeout=`.
- `PYTHONPATH=src python -m pytest tests/test_cloud_migration.py -q` is green.

---

### U3. Bound the `copy_blob` rewriteToken loop

**Goal:** A stuck or pathologically-slow rewriteToken can never hang the process indefinitely; it fails that one object (bounded in both iterations *and* wall-clock time) and the run continues.

**Requirements:** R3, R5, R6

**Dependencies:** U1 (relies on `_copy_one` catching the cap-exceeded error as a per-object `failed` outcome)

**Files:**
- Modify: `scripts/cloud_migration/core.py` (`copy_blob`, `_copy_one`, new constants/exception)
- Test: `tests/test_cloud_migration.py`

**Approach:**
- Add `_REWRITE_MAX_ITERS = 10_000` (generous; commented as a hang-guard, not a size cap — each iteration copies one server-side chunk).
- In `copy_blob`'s `while True:` loop, count iterations; if the token has not cleared after `_REWRITE_MAX_ITERS`, raise a small dedicated exception (e.g. `RewriteLimitExceeded(RuntimeError)`) whose message names `dst_name` and the cap.
- **Bound wall-clock too, not just iterations.** With U2's per-call `timeout=_OP_TIMEOUT` (60s), a slow-but-responsive endpoint that returns a non-`None` token just under each deadline would keep the loop spinning for `_REWRITE_MAX_ITERS × _OP_TIMEOUT` (~7 days) before the iteration cap fires — which does not satisfy "can never hang." Add a wall-clock deadline checked inside the loop (e.g. `_REWRITE_MAX_WALL = 30 * 60` seconds, captured at loop entry via `time.monotonic()`), raising `RewriteLimitExceeded` when **either** bound trips. (Exact budget is a deferred decision — see Open Questions; the byte-progress-stall option in Deferred to Follow-Up Work is the more precise alternative.)
- In `_copy_one`, broaden the `except` to `(GoogleAPIError, RewriteLimitExceeded)` so a stuck token becomes a per-object `failed` outcome (manifest still written; idempotent re-run retries) rather than aborting the run. Do **not** route it through `MigrationError` (that is for fail-closed gates that should abort).
- **Fake (R6):** force a never-clearing token — e.g. a `FakeStore.rewrite_never_completes: set[str]` that makes `FakeBlob.rewrite` always return a non-None token, or reuse `rewrite_plan` with a value above the cap.

**Technical design:** *(directional, not implementation spec)*

```
copy_blob(bucket, src, dst_name):
    dst = bucket.blob(dst_name); token = None; iters = 0
    deadline = time.monotonic() + _REWRITE_MAX_WALL
    while True:
        token, _, _ = dst.rewrite(src, token=token, timeout=_OP_TIMEOUT)
        iters += 1
        if token is None: break
        if iters >= _REWRITE_MAX_ITERS or time.monotonic() > deadline:
            raise RewriteLimitExceeded(f"{dst_name}: rewriteToken not cleared (iters={iters})")
    dst.reload(timeout=_OP_TIMEOUT); return dst
```

**Patterns to follow:**
- `tests/test_cloud_migration.py::test_stage_large_object_multi_call_rewrite` (multi-call rewrite via `rewrite_plan`) — extend for both the no-false-trigger and the cap-exceeded cases.

**Test scenarios:**
- Error path: a blob whose token never clears → `copy_blob` raises after the iteration cap; in a stage run, that object is `failed`, the loop continues for other objects, the manifest is written, `result.ok` is False.
- Error path (wall-clock): with the iteration cap left high, a monkeypatched `time.monotonic` advanced past the deadline makes `copy_blob` raise `RewriteLimitExceeded` after the next iteration → object `failed`, run continues. Proves the time bound fires independently of the iteration bound.
- Edge case (no false-trigger): a legitimately large object needing many (but under-cap) rewrite calls — e.g. `rewrite_plan` set to a few hundred — still completes `copied` and verified. Confirms the cap is generous.
- Edge case (boundary, optional if cheap with the fake): exactly `_REWRITE_MAX_ITERS` completes; one more raises.

**Verification:**
- `copy_blob` cannot loop unboundedly; a stuck token fails that single object while the run still completes and writes its manifest.

---

### U4. Durable, crash-safe decommission audit log (`.jsonl`)

**Goal:** The irreversible decommission step leaves an on-disk, per-line-flushed record of every delete/keep, so a crash mid-run leaves an auditable trail of exactly what was deleted.

**Requirements:** R4, R5, R6

**Dependencies:** U1 (the audit log should record the widened error/kept outcomes)

**Files:**
- Modify: `scripts/cloud_migration/core.py` (`run_decommission`: add `on_outcome`)
- Modify: `scripts/decommission_flat_namespace.py` (open `.jsonl`, wire `on_outcome`, flush per line, `try/finally`, add `--audit-log`)
- Test: `tests/test_cloud_migration.py`

**Approach:**
- `run_decommission`: add `on_outcome: Callable[[DeleteOutcome], None] | None = None`. Invoke it immediately after **every** `outcomes.append(...)` — across the `kept` (no-staging), `kept` (unverified), `planned` (dry-run), `kept` (PreconditionFailed), `kept` (`error:`), and `deleted` branches, in both the recordings/ and sessions/ loops. For a `deleted` outcome the append already happens after `src_blob.delete()` returns, so the callback fires only once the delete is durable — exactly the crash-safety property R4 needs. Mirror `run_stage`'s existing `on_outcome` placement.
- Decommission shim: add `--audit-log` (default e.g. `cloud-migration-decommission-audit.jsonl`; suffix `.dryrun.jsonl` on `--dry-run`). Before the run, open the file; define `_record(outcome)` writing `json.dumps(dataclasses.asdict(outcome), sort_keys=True) + "\n"` then `flush()`; pass as `on_outcome`; close in a `finally` (mirror `migrate_flat_to_staging.py` lines 63–92). Add `import dataclasses, json, os` to the shim.
- **Restrict the audit-log file to `0o600`.** The log records GCS object names, which `SECURITY.md` classifies as the same sensitivity class as the recordings themselves (filenames can reveal that something was recorded). Mirroring the stage shim's plain `open(path, "w")` would leave perms at the operator's umask (world-readable under umask `022`). Open via `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` wrapped in `os.fdopen(...)`, matching the `0o600` precedent used for `~/.screencap/content_index.db` and `auto-serve.log`. (The existing stage `.partial.jsonl` sidecar shares this umask exposure — pre-existing, noted as a follow-up in Scope Boundaries, not fixed here.)

**Execution note:** Write the core `on_outcome` ordering test (callback fires per outcome, before return, deleted-after-delete) before wiring the shim file IO.

**Patterns to follow:**
- `run_stage`'s `on_outcome` parameter + invocation site, and `scripts/migrate_flat_to_staging.py`'s `_record` + `.partial.jsonl` + `try/finally` block (the audit log is the decommission analogue).
- `tests/test_cloud_migration.py::test_stage_cli_dry_run_writes_sidecar_manifest` (sidecar-file CLI assertion shape).

**Test scenarios:**
- Integration: a live decommission run with a list-appending `on_outcome` → the callback receives one entry per outcome (every deleted + kept), in loop order, and `deleted` entries correspond exactly to `store.deleted`.
- Integration (crash-safety): inject an error on the Nth object (via U1's seam) → the outcomes emitted to `on_outcome` before the failure point are exactly the objects handled before it (proves partial durability up to the crash).
- CLI: `decommission_flat_namespace.main(["--bucket", …, "--confirm"])` writes the `.jsonl`; every line parses as JSON with `action`/`src`/`reason`; line count == number of outcomes. `--dry-run` writes the `.dryrun.jsonl` variant and deletes nothing.
- Edge case (perms): after a live decommission run, the audit `.jsonl` is mode `0o600` (owner-only). Assert via `os.stat(...).st_mode & 0o777 == 0o600` on the written file.
- Edge case: file handle is closed even when `run_decommission` raises a `MigrationError` (e.g. `--include-sessions` without `--sessions-backup-confirmed`) — the `finally` closes it; assert no leak / file exists and is well-formed (or empty) after the early refusal.

**Verification:**
- A decommission run leaves a per-line-flushed `.jsonl` audit log; the deleted/kept accounting in `DecommissionResult` is unchanged; a simulated mid-run crash leaves the prior deletions recorded on disk.

---

### U5. Document the audit log and reliability behavior

**Goal:** Operators can find and rely on the new audit artifact and understand the hardened failure behavior.

**Requirements:** R7

**Dependencies:** U4 (documents the artifact U4 introduces)

**Files:**
- Modify: `docs/runbooks/cloud-migration-runbook.md`
- Modify: `scripts/decommission_flat_namespace.py` (module docstring / Usage)

**Approach:**
- Runbook **Step 7**: document the `--audit-log` `.jsonl` (path, one row per delete/keep, that it is the crash-safe record, created mode `0o600`), add a post-run confirmation line that points at it, and add a one-line note that transient GCS errors now KEEP the object and exit non-zero (so an incomplete run is never read as clean), that per-object calls are timeout-bounded, and that the rewrite loop is bounded.
- Add an **operator-guidance line**: an *all-objects-failed* `error:` pattern signals a systemic outage, not transient single-object noise — a sustained 503/throttle that exhausts the SDK's retries surfaces as a `RetryError` on *every* object. Investigate the endpoint/quota before blindly re-running.
- Add the audit-log path to the **Live execution log** Step 7 entry.
- Update the decommission shim's docstring Usage block to show `--audit-log`.

**Patterns to follow:**
- The existing runbook Step 3 wording for the stage `.partial.jsonl`/manifest sidecar; the "Live execution log" line style.

**Test scenarios:**
- Test expectation: none — documentation + docstring only, no behavioral change.

**Verification:**
- Runbook Step 7 and the shim Usage mention the audit log; the Live execution log has a slot for its path.

---

## System-Wide Impact

- **Interaction graph:** `_copy_one` and `copy_blob` are shared by **both** `run_stage` and `run_promote`, so U1's catch-widening and U2/U3's changes affect staging and promotion identically (intended — same robustness everywhere). `run_decommission` is standalone. The promote shim's `_gcs_error()` is the API-surface parity point for U1.
- **Error propagation:** per-object GCS errors and stuck-token failures stay **local** (recorded `failed`/`kept`, loop continues); `MigrationError` (fail-closed gates) and `PreconditionFailed` (generation mismatch) keep their distinct, deliberately non-absorbed semantics. The broad `except GoogleAPIError` is still specific enough not to swallow `AttributeError`/`KeyError`.
- **State lifecycle risks:** the decommission audit log must flush per line and emit a `deleted` row only **after** the delete returns, so a `kill -9` leaves a truthful (never over-stated) record. No change to the generation-pinned, fresh-reverify delete gate.
- **API surface parity:** `core` and the promote shim both widen to `GoogleAPIError`; the decommission shim gains `on_outcome` wiring mirroring the stage shim.
- **Integration coverage:** the offline fake's new raise/timeout/stuck-token seams are what make R1–R4 provable without the SDK; the CLI-level tests prove the shim wiring (audit file, exit codes) that unit tests of `core` alone would miss.
- **Unchanged invariants:** crc32c+content_type integrity gate, idempotent checksum-gated copy, never-delete-without-fresh-remote-confirm, generation-pinned delete, quiesce + IAM pre-checks, sessions backup attestation, and the JSON manifest format are all explicitly preserved.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Widened `except GoogleAPIError` mislabels a generation mismatch as a transient `error:` | Keep `except PreconditionFailed` ordered **before** the broad catch in `run_decommission`; explicit test asserts the distinct "source changed since enumeration" reason. |
| Rewrite cap false-triggers on a legitimately huge cross-region object | Generous `_REWRITE_MAX_ITERS` (≫ any realistic chunk count) + a no-false-trigger test with many under-cap iterations; progress-stall detection noted as an optional follow-up. |
| Iteration cap alone doesn't bound wall-clock; a slow-token endpoint hangs for days | Pair the iteration cap with a wall-clock deadline inside `copy_blob` (U3) so whichever bound trips first ends the loop; the worst-case hang window becomes the budget, not `iters × timeout`. |
| Stuck-token error accidentally aborts the whole run | The cap raises a dedicated `RewriteLimitExceeded` caught by `_copy_one` as a per-object `failed` outcome — not a `MigrationError`; test asserts the run continues + writes its manifest. |
| Audit log overstates deletions after a crash | `deleted` rows are emitted only after `delete()` returns, flushed per line; crash-safety test asserts only pre-crash outcomes are recorded. |
| Dry-run audit log clobbers a real one | `--audit-log` uses a `.dryrun.jsonl` suffix on `--dry-run` (mirrors the stage manifest's dry-run suffixing). |
| New audit log leaks sensitive recording names if world-readable | Create it `0o600` via `os.open` + `os.fdopen` (U4), matching the `content_index.db` / `auto-serve.log` precedent; U5 runbook documents the restricted mode. |
| Fake-signature drift breaks existing tests | U2 adds `timeout=None` to the fake methods in the same unit as the core change; the full suite is the regression guard. |

---

## Documentation / Operational Notes

- U5 covers the runbook (Step 7 + Live execution log) and the decommission shim docstring. No other docs reference these scripts' internals.
- No rollout/monitoring/flag concerns — these are operator-run admin scripts, not a deployed service. The audit `.jsonl` is a local artifact (created mode `0o600`) the operator retains alongside the existing manifest.

---

## Sources & References

- **Origin (ticket):** [Linear SCR-145 — Harden cloud-migration scripts](https://linear.app/zk-email/issue/SCR-145/harden-cloud-migration-scripts-gcs-error-handling-timeouts-rewrite-cap) (related to [SCR-139](https://linear.app/zk-email/issue/SCR-139))
- Parent plan: `docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md` (U8/U9)
- Runbook: `docs/runbooks/cloud-migration-runbook.md`
- Code under change: `scripts/cloud_migration/core.py`, `scripts/migrate_flat_to_staging.py`, `scripts/promote_staging_to_demo.py`, `scripts/decommission_flat_namespace.py`
- Tests: `tests/test_cloud_migration.py` (offline in-memory GCS fake)
- SDK behavior verified against the installed `google-cloud-storage` / `google-api-core` (exception hierarchy + method `timeout=` support).
