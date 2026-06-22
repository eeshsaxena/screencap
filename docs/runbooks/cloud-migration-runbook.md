---
title: "Runbook: flat → staging → demo cloud migration"
date: 2026-06-16
type: runbook
plan: docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
origin: docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md
status: ready
project: proteus-photos
region_compute: southamerica-east1
unit: U8/U9
scripts:
  - scripts/migrate_flat_to_staging.py
  - scripts/promote_staging_to_demo.py
  - scripts/decommission_flat_namespace.py
  - scripts/cloud_migration/core.py
---

# Runbook: flat → staging → demo cloud migration (U8/U9)

Operational source of truth for migrating the legacy **flat, public** recording
namespace into the v1 isolated layout. The scripts are committed; **the live runs
are operator steps** — this runbook is the order, the gates, and the rollback.

```
recordings/{name}/...      ── U8 stage ──►  import-review/{name}/...   (PRIVATE, no public handler)
                                                   │ U8 promote (founder allow-list; markers stripped)
                                                   ▼
                                            demo/{name}/...            (PUBLIC, served by demo-* function)

recordings/ , sessions/    ── U9 decommission ──►  (deleted LAST, after U7 verified live)
```

Everything happens **within the one shared recordings bucket** the signing
function reads. `import-review/` has no public handler in the Cloud Function
(`scripts/cloud-function/main.py` dispatches only `demo-*` + the token-gated
`users/` actions), so staging is private **provided the bucket has no public IAM
binding** — Step 0 enforces that.

> The three scripts are SDK-injectable and unit-tested offline
> (`tests/test_cloud_migration.py`); the shared logic lives in
> `scripts/cloud_migration/core.py`. Run them from an admin machine with
> Application Default Credentials for a principal holding
> `roles/storage.objectAdmin` (object copy/delete) **and** `roles/storage.admin`
> for the Step 0 pre-checks. Step 0 reads the bucket IAM policy, its
> `iam_configuration` (UBLA + Public Access Prevention), and — when UBLA is off —
> its **default object ACL**; `roles/storage.admin` is the simplest grant that
> covers all three. The narrower `storage.buckets.{get,setIamPolicy}` is **not**
> sufficient on its own — it omits default-object-ACL read, so the SCR-146 ACL
> check would fail with a permission error on a UBLA-off bucket. Install the SDK:
> `pip install google-cloud-storage>=3.1.1`.

---

## Pre-conditions (do not start until all hold)

| Gate | Why | Verify |
|---|---|---|
| **U2 shipped** — the flat write path is *removed*, not just token-gated | A writer that still lands `recordings/{name}/…` re-populates the namespace under the migration | `scripts/cloud-function/` deployed (SCR-137 done); no client writes a flat path |
| **SCR-137 deploy done** — the live function serves `demo-*` | Promotion targets `demo/`, which the website reads | `POST {"action":"demo-list"}` → HTTP 200 (empty `recordings` is fine pre-promotion) |
| **U7 shipped** (SCR-138, PR merged) | The website renders `demo/`; **U9 must not run until U7 is verified live** | `screencap-website` PR #14 merged; gallery reachable |
| **You know the bucket** | Single-bucket model: stage/promote/decommission all target the bucket the live function reads | `gcloud functions describe get-upload-urls --gen2 --region southamerica-east1 --format='value(serviceConfig.environmentVariables.SCREENCAP_BUCKET)'` |

> **Bucket identity is load-bearing.** The migration is single-bucket: `demo/`
> must land in the *same* bucket `get-upload-urls` reads (currently
> `screencap-recordings-staging`) or the website will not see it. Confirm the
> legacy flat `recordings/` lives in that bucket
> (`gsutil ls gs://<bucket>/recordings/ | head`). If the flat data is in a
> *different* legacy bucket, that is a cross-bucket case these single-bucket
> scripts do not handle — resolve it (copy the flat data into the function's
> bucket first, or re-point the function) before proceeding, and record the
> decision in the log below.

Set the bucket once for the commands below:

```bash
BUCKET=screencap-recordings-staging   # the value of SCREENCAP_BUCKET on get-upload-urls
```

---

## Step 0 — Bucket-IAM pre-check (private-staging precondition) 🚦

`import-review/` is only private if the bucket has **no**
`allUsers`/`allAuthenticatedUsers` read binding (the legacy public-gallery posture
may have one). This is the SCR-139 first pre-check.

```bash
# Inspect current bucket IAM:
gcloud storage buckets get-iam-policy "gs://$BUCKET" --format=json \
  | jq '.bindings[] | select(.members[]|test("allUsers|allAuthenticatedUsers"))'
```

> **IAM bindings are not the only public door.** GCS also serves objects publicly
> through legacy **object / default-object ACLs**, which are active whenever
> **Uniform Bucket-Level Access (UBLA)** is disabled (the default on older buckets).
> A public *default object ACL* would make every staged object world-readable even
> with zero public IAM bindings. **As of SCR-146 the stage script asserts this
> automatically** — it reads `iam_configuration` and, when UBLA is off, enumerates
> the default object ACL for `allUsers`/`allAuthenticatedUsers` and **refuses to
> stage (fail-closed)** if a public grant exists. Unlike the IAM check there is no
> `--remove-public-*` auto-fix: enable UBLA out-of-band first. Confirm the posture
> yourself before the run and record it in the log below:
>
> ```bash
> gcloud storage buckets describe "gs://$BUCKET" \
>   --format='value(uniform_bucket_level_access.enabled, public_access_prevention)'
> # Expect: True  enforced
> # If UBLA is off, also check the default object ACL for public grants:
> gsutil defacl get "gs://$BUCKET"
> ```
>
> If UBLA is off, enable it (`gcloud storage buckets update "gs://$BUCKET"
> --uniform-bucket-level-access`) and set Public Access Prevention to `enforced`
> before the first stage run.

`migrate_flat_to_staging.py` runs the IAM-binding check itself and **refuses to
stage** while a public binding exists. Remove it in one of two ways:

```bash
# Option A — let the stage script strip it (logged, then re-verified clean):
python scripts/migrate_flat_to_staging.py --bucket "$BUCKET" --remove-public-iam --dry-run   # preview is IAM-safe
python scripts/migrate_flat_to_staging.py --bucket "$BUCKET" --remove-public-iam             # live: strips, then stages

# Option B — strip out-of-band first, then stage normally:
gcloud storage buckets remove-iam-policy-binding "gs://$BUCKET" \
  --member=allUsers --role=roles/storage.objectViewer
```

Record the **verified IAM posture** (before → after) in the log below. After this
step the bucket must be signed-URL-only; the public gallery is served by the
function's `demo-sign-download`, not by direct bucket reads.

---

## Step 1 — Scale pre-check (measurement)

```bash
gsutil du -s "gs://$BUCKET/recordings/" "gs://$BUCKET/sessions/"   # total bytes per prefix
gsutil ls -lr "gs://$BUCKET/recordings/**" | sort -k1 -n | tail -5 # largest objects
gsutil ls -r  "gs://$BUCKET/recordings/**" | wc -l                 # object count
```

At friend-trial scale (< ~5k objects) the Python `rewrite()` loop in these scripts
is the right tool — it gives per-object `crc32c`/`content_type` control and follows
`rewriteToken` to completion for large video objects. Only reconsider
`gsutil -m cp` / Storage Transfer Service if the count is materially larger.
Record the measured numbers in the log.

---

## Step 2 — Quiesce (no concurrent writers)

A recording that started **before U2** still holds pre-isolation upload config and
writes its completion sentinel late — it can land a flat blob *after* enumeration.
Do not rely on cohort quiet time.

- Confirm the flat write path is closed (U2) — already true post-SCR-137.
- Confirm no daemon recording is in flight. The scripts auto-check via
  `screencap status --json` and refuse while a recording is **ACTIVE**. On an
  admin box without `screencap`, the check returns *unknown*; pass
  `--confirm-quiesced` to attest you have confirmed no engine is running.

---

## Step 3 — Stage: dry-run, then live (U8, no deletes)

```bash
# Dry-run: plans every recordings/{name}/{file} -> import-review/{name}/{file}, mutates nothing.
python scripts/migrate_flat_to_staging.py --bucket "$BUCKET" --dry-run \
  --manifest cloud-migration-staging-manifest.json
# (writes cloud-migration-staging-manifest.json.dryrun.json)

# Live stage: rewrite + per-object crc32c/content_type verify; idempotent; NO source deletes.
python scripts/migrate_flat_to_staging.py --bucket "$BUCKET" \
  --manifest cloud-migration-staging-manifest.json
```

What it does and guarantees:

- Enumerates `recordings/` only — **`sessions/` and `sessions/_index.json` are
  excluded** (demo is a recordings gallery; the index references retired flat
  paths). 
- Copies with full nested suffix preserved (`{name}/screenshots/0.png`), verifying
  each object's `crc32c` **and** `content_type` (count/size alone would miss a
  truncated or content-type-mangled copy).
- Idempotent: a re-run skips objects whose checksum already matches and re-copies
  any truncated one (never trusts `exists()` alone).
- Exit non-zero if any object fails verification — staging is then incomplete; do
  **not** promote or decommission.

Spot-check after the live run:

```bash
gsutil ls -r "gs://$BUCKET/import-review/**" | head
# pick one object and compare hashes end-to-end:
gsutil hash -h "gs://$BUCKET/recordings/<name>/video.mp4"
gsutil hash -h "gs://$BUCKET/import-review/<name>/video.mp4"
```

---

## Step 4 — Review gate (content + title + consent) 🚦

**This is the public-exposure consent gate** (plan Open Questions). The staged
recordings are real friend-trial screen captures: their **content** may contain
PII/credentials a scrub missed, and their **names become public path segments**.

Promotion is **opt-in per recording**. The founder reviews each staged recording
(`import-review/{name}/…`) for content and title, and writes an **allow-list** of
cleared names:

```bash
# cleared-recordings.txt — one cleared recording name per line; '#' comments allowed.
demo-onboarding-walkthrough
quick-capture-example
# left out: anything with visible credentials, third-party data, or a revealing title
```

If consent cannot be obtained for any recording, the allow-list is empty and the
gallery is seeded from purpose-built/synthetic content instead — U7 already ships
the empty-gallery placeholder, so the cutover is not blocked on content.

---

## Step 5 — Promote: dry-run, then live (U8)

```bash
python scripts/promote_staging_to_demo.py --bucket "$BUCKET" \
  --allow-list cleared-recordings.txt --dry-run
python scripts/promote_staging_to_demo.py --bucket "$BUCKET" \
  --allow-list cleared-recordings.txt
```

- Copies **only allow-listed** recordings `import-review/{name}/…` → `demo/{name}/…`.
- **Strips** the superseded `_unlisted` / `show_on_website` marker blobs so each
  recording appears in the marker-blind `demo-list`.
- Per-object verify; idempotent; no deletes. Warns on any allow-listed name absent
  from staging (typo / not staged).

Verify the gallery via the function (not direct bucket reads):

```bash
FN=$(gcloud functions describe get-upload-urls --gen2 --region southamerica-east1 \
  --format='value(serviceConfig.uri)')
curl -s -X POST "$FN" -H 'Content-Type: application/json' \
  -d '{"action":"demo-list"}' | jq '.recordings[].name'
```

---

## Step 6 — Cutover verification (U7 live) 🚦 — gate for U9

Before any deletion, confirm in **production**:

- The `screencap-website` gallery lists the promoted recordings and **plays** them
  (video + screenshots render; `demo-sign-download` returns working URLs).
- The founder confirms promotion is complete (the allow-list is final).

Only when both hold may you proceed to U9. Until then, the flat namespace is the
rollback source of truth — **do not delete it**.

---

## Step 7 — Decommission the flat namespace (U9) ⛔ irreversible

```bash
# Dry-run: lists exactly the flat blobs slated for deletion; deletes nothing.
python scripts/decommission_flat_namespace.py --bucket "$BUCKET" --dry-run

# Live: requires --confirm. Add --include-sessions to also retire sessions/.
python scripts/decommission_flat_namespace.py --bucket "$BUCKET" --confirm --include-sessions
```

Guarantees:

- Each `recordings/` delete is gated on a **fresh live re-verify** of its
  `import-review/` staging copy (`crc32c` + `content_type`). A source whose staging
  copy is missing or mismatched is **KEPT**, never deleted — a crash mid-run leaves
  the remainder intact and the run is safely re-runnable.
- `sessions/` has **no** staging copy (it was never migrated), so it is deleted
  only behind the explicit `--include-sessions` opt-in. Sessions are retired data;
  the legacy `zkairdrop` frozen backup (see U10 / SCR-112) is their last-resort
  archive.
- A **post-run re-scan** asserts `recordings/` (and `sessions/`, if included) are
  empty (R11) and **surfaces any new flat blob** that raced past the quiesce —
  exit is non-zero and the blob is reported, not deleted.

> **Deliberate window note.** Between Step 5 (promotion) and Step 7
> (decommission), the flat namespace is still publicly listable via any surviving
> legacy path. Keep this window **short and intentional**; R11 is fully satisfied
> only after Step 7's re-scan shows the prefixes empty.

Post-run confirmation:

```bash
gsutil ls "gs://$BUCKET/recordings/**" 2>&1 | head   # expect: no matches
gsutil ls "gs://$BUCKET/sessions/**"   2>&1 | head   # expect: no matches (if --include-sessions)
```

---

## Rollback

| Situation | Action |
|---|---|
| Stage produced a bad copy | Re-run stage (idempotent; re-copies any checksum-mismatched object). No source was touched. |
| Wrong recording promoted to `demo/` | Delete it from `demo/` (`gsutil rm -r gs://$BUCKET/demo/<name>/`); the source and staging copies are intact. |
| Gallery broken after promotion, **before U9** | The flat namespace is untouched — investigate/fix the website (U7) or re-promote; nothing to roll back in storage. |
| Need a recording back **after U9** | Restore from the `zkairdrop` frozen backup buckets (real flat data is retained there until U10 revokes their public access — it is a *backup*, not deleted). Re-stage and re-promote. |

---

## Live execution log

> Fill in during the live runs (mirrors `cloud-auth-setup.md`'s log convention).

- **Date / operator:** _…_
- **Bucket (SCREENCAP_BUCKET on get-upload-urls):** _…_
- **Step 0 IAM posture — before:** _…_ **after:** _…_
- **Step 1 scale:** _N objects, X bytes under recordings/; Y bytes under sessions/; largest object …_
- **Step 3 stage:** _N staged, 0 failed verify; manifest path …_
- **Step 4 review:** _allow-list = … (or empty → synthetic gallery)_
- **Step 5 promote:** _N recordings in demo/; markers stripped …_
- **Step 6 cutover:** _website verified live on demo/ at … by …_
- **Step 7 decommission:** _N deleted; sessions included? …; re-scan empty? …_
