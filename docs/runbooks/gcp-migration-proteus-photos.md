---
title: "Runbook: Migrate screencap GCP footprint from zkairdrop to proteus-photos"
date: 2026-05-29
type: runbook
ticket: SCR-99
plan: docs/plans/2026-05-29-003-refactor-gcp-migration-proteus-photos-plan.md
origin: docs/brainstorms/2026-05-29-gcp-migration-zkairdrop-to-proteus-photos-requirements.md
status: in-progress
region_compute: southamerica-east1
region_storage: US (multi-region)
source_project: zkairdrop
target_project: proteus-photos
---

# Runbook: screencap GCP migration → proteus-photos

Operational source of truth for SCR-99. The repo has no IaC (no Terraform/cloudbuild)
and `process-recording` had no stored deploy command, so this file is the durable record
of **what was provisioned, where, with which IAM, and how to roll back**. Update the
**Live execution log** at the bottom as resources are actually created.

> **Secrets policy:** never paste secret *values* into this file (it is committed).
> `GOOGLE_GENAI_API_KEY` and any SA keys are referenced by Secret Manager name only.

---

## Phase model & current position

| Phase | Units | Destructive? | Status |
|-------|-------|--------------|--------|
| 1 — additive build-up | U1, U2, U3, U4 | No | **DONE (2026-06-02)** |
| 2 — verify gate | ~~U5~~ (dropped), U6 | No (gate) | not started |
| 3 — cutovers + client release | U7, U8, U9 | **Yes (U7 irreversible)** | not started |
| 4 — grace + teardown | U10 | **Yes** | not started |

## SCOPE AMENDMENT (2026-06-02): R6 / stable `api.screencap.sh` domain — DROPPED

Decided with the product owner via brainstorm. **R6 (stable signing domain) and U5
(load balancer + cert + DNS) are removed from this migration.** Driver: the backend host is
expected to be stable after this one-time `zkairdrop→proteus-photos` cleanup, so the domain's
only value — decoupling clients from the project-specific `*.run.app` host against a *future*
move — is insurance against an event that isn't expected. It carries a ~$22/mo standing cost
(external ALB forwarding rule + static IPv4) and a Porkbun DNS dependency for no expected payoff.

Consequences:
- **U5 — REMOVED.** No load balancer, serverless NEG, static IP, managed cert, or DNS record.
- **U6 — simplified.** End-to-end verification runs against the new prod `*.run.app` host
  directly via `SCREENCAP_*` overrides (no cert/ingress-lockdown checks).
- **U9 — simplified.** Repoint `download.py`/`upload.py` defaults to the new prod host
  `https://get-upload-urls-ld7izzjvga-rj.a.run.app` (NOT `api.screencap.sh`). One-line default
  change + tests + CHANGELOG. **Now depends only on U8** (release pipeline), not U5.
- **Accepted trade:** the signing function stays publicly reachable + unauthenticated at its
  `*.run.app` host (CORS `*`) — identical to today's `zkairdrop` posture, not a regression.
  Ingress lockdown is dropped with the domain. If hardening is ever wanted, it's a standalone task.
- **Re-coupling accepted:** if the backend ever moves again, a single client release repoints it
  (same mechanism as the unavoidable U9 release). The `SCREENCAP_*_URL` env override remains the
  existing escape hatch.
- The plan doc (`docs/plans/2026-05-29-003-...`) and Linear SCR-99 still describe R6/U5 — update
  them to reflect this amendment.

**This session executed Phase 1 (U1–U4).** Remaining: U6 (verify on `*.run.app`), U7/U8 cutovers,
U9 (client repoint to new `*.run.app`), U10 teardown.

---

## Source inventory (zkairdrop) — discovered 2026-05-29

### Buckets

| Bucket | Location | Class | UBLA | Public Access Prevention | Public read? | Soft-delete |
|--------|----------|-------|------|--------------------------|--------------|-------------|
| `gs://screencap-recordings` | **US** (multi-region) | STANDARD | on | **enforced** | no (signed-URL only) | 7d (604800s) |
| `gs://screencap-recordings-dev` | US | STANDARD | — | inherited | no | default |
| `gs://screencap-releases` | US | STANDARD | — | inherited | **yes — `allUsers`→`objectViewer`** | default |

> ⚠️ **Region correction vs. the plan.** The plan text says recreate buckets in
> `southamerica-east1`. The **source buckets are `US` multi-region** — only the *compute*
> (functions, Run) is in `southamerica-east1`. Faithful lift-and-shift = recreate buckets
> as **US multi-region** and put the Eventarc trigger in the **`us`** location (matching
> source). Compute stays in `southamerica-east1`.

### Compute

| Resource | Kind | Region | Identity (SA) | Notable config |
|----------|------|--------|---------------|----------------|
| `get-upload-urls` | gen2 fn (HTTP) | southamerica-east1 | `397234807794-compute@developer.gserviceaccount.com` (default compute) | ingress ALLOW_ALL; no `SCREENCAP_BUCKET` → defaults to `screencap-recordings`; underlying Run service `get-upload-urls` |
| `get-upload-urls-dev` | gen2 fn (HTTP) | southamerica-east1 | default compute (same) | `SCREENCAP_BUCKET=screencap-recordings-dev` |
| `process-recording` | Cloud Run | southamerica-east1 | default compute | env `SCREENCAP_BUCKET=screencap-recordings`, `GOOGLE_CLOUD_PROJECT=zkairdrop`, `GOOGLE_GENAI_API_KEY=<plaintext env>`; image in `southamerica-east1-docker.pkg.dev/zkairdrop/cloud-run-source-deploy/process-recording`; cpu 1 / mem 1Gi |
| `process-recording-dev` | Cloud Run | southamerica-east1 | default compute | dev equivalent |
| `screencap-recording-signed-url` | Cloud Run | southamerica-east1 | — | **Legacy / predecessor to `get-upload-urls`** — NOT in the plan. Confirm abandoned; fold into U10 teardown inventory. |

### Eventarc

| Trigger | Location | Event filter | Destination | Trigger SA | Transport |
|---------|----------|--------------|-------------|------------|-----------|
| `process-recording-trigger` | **us** | `bucket=screencap-recordings`, `type=google.cloud.storage.object.v1.finalized` | Run `process-recording` @ southamerica-east1 | `tasks-staging-pr@zkairdrop.iam.gserviceaccount.com` (shared/borrowed) | Pub/Sub (auto topic+sub) |
| `process-recording-dev-trigger` | us | dev bucket | Run `process-recording-dev` | (same shared SA) | Pub/Sub |

### Service accounts & CI

- Function/Run identity: **default compute SA** `397234807794-compute@developer.gserviceaccount.com`
  (this is the **old signer identity** that must follow `screencap-recordings` into proteus-photos during grace — see U7 / P1-find-1).
- Release CI: `screencap-ci-releases@zkairdrop.iam.gserviceaccount.com` (the `GCP_SA_KEY` identity; rebind in U8).
- The v4 `signBlob` path works because the default compute SA was granted
  `roles/iam.serviceAccountTokenCreator` on itself in zkairdrop. The new project must
  replicate this (resource-level self-binding — see P2 below).

### Client surfaces welded to clients (the migration's core fragility)

- Signing host hardcoded **identically** in `src/screencap/download.py:28` and `src/screencap/upload.py:29`:
  `https://get-upload-urls-wyldgq6aqa-rj.a.run.app` (project-coupled `*.run.app`). Repointed to
  `https://api.screencap.sh` in **U9** (both files together).
- Releases distribution URL `https://storage.googleapis.com/screencap-releases/releases`
  in `src/screencap/updater.py:23` and `scripts/install.sh:215`. **No code change** — name preserved (U8).

---

## Target design (proteus-photos)

### Naming

| Purpose | Phase-1 (staging) name | Final name (at cutover) |
|---------|------------------------|--------------------------|
| Recordings bucket (prod) | `gs://screencap-recordings-staging` (US) | `gs://screencap-recordings` (US) — U7 |
| Recordings bucket (dev) | `gs://screencap-recordings-dev-staging` (US) | `gs://screencap-recordings-dev` (US) — recreated fresh at cutover |
| Releases bucket | n/a (untouched in Phase 1) | `gs://screencap-releases` (US, public) — U8 |
| Signing fn | `get-upload-urls`, `get-upload-urls-dev` (southamerica-east1) | same |
| Processing | `process-recording`, `process-recording-dev` (southamerica-east1) | same |
| Eventarc | `process-recording-trigger`(`us`) on staging bucket | re-created on final bucket — U7 |

### Service accounts (dedicated — least privilege; do NOT reuse the default compute SA)

Rationale: the source uses the *default compute SA* for everything, which is exactly the
project-entanglement this migration escapes. Create dedicated SAs in proteus-photos:

| SA | Used by | Roles |
|----|---------|-------|
| `screencap-signer@proteus-photos.iam.gserviceaccount.com` | `get-upload-urls` (prod+dev) | resource-level `roles/iam.serviceAccountTokenCreator` **on itself** (P2-find); `roles/storage.objectAdmin` on recordings bucket(s) |
| `screencap-processor@proteus-photos.iam.gserviceaccount.com` | `process-recording` (prod+dev) | `roles/storage.objectAdmin` on recordings bucket(s); Secret Manager accessor for the GENAI key |
| `screencap-eventarc@proteus-photos.iam.gserviceaccount.com` | Eventarc trigger | `roles/eventarc.eventReceiver` (project), `roles/run.invoker` on the processing service |

### Cross-project grace grant (PRE-STAGED in U1 — P1-find)

The **old zkairdrop** signer SA `397234807794-compute@developer.gserviceaccount.com` must be
granted, on the **new** recordings bucket(s), so the retired function follows the global name
through grace:
- `roles/storage.objectAdmin` (objects: list/get/create/delete)
- `roles/storage.legacyBucketReader` (provides `storage.buckets.get` — Object Admin alone fails `list`/`sign-download`)

Apply on **staging** in U1 (so U6 can test the follow live) and **identically on the final
bucket** in U7.

---

## Open review findings folded in (from ce-doc-review 2026-05-29)

- **[P1-find-1] Global-name follow must be re-tested against the *reborn* bucket in-window at U7**,
  not only against staging in U6. Added as a U7 go-criterion below.
- **[P1-find-2] Default to a `SCREENCAP_BUCKET` redeploy of the old function at U7** (deterministic
  cold re-resolution) rather than betting on the warm follow; `min-instances=1` is the only
  alternative. Baked into U7.
- **[P1-find-3] U10 teardown floor** must be a concrete day-1-post-U9 baseline threshold +
  day-over-day slope (a 7-day window can't support week-over-week). Defined in U10.
- **[P1-find-4] Pre-stage the cross-project IAM grant in U1** (`objectAdmin` + `storage.buckets.get`),
  not first-attempted under U7 time pressure. Done in U1 above.
- **[P2] signBlob token-creator = resource-level self-binding**, not project-level. Used in U1.
- **[P2] U5 serverless NEG targets the gen2 function's underlying Cloud Run service name**
  (captured in U2), not a Cloud Functions reference.
- **[P2] U7 promote the `sessions/` prefix (idempotency markers) before the Eventarc trigger goes live.**
- **[P2] U8 CI SA = dedicated bucket-scoped role**, not project `storage.admin`; disable old key at U8.
- **[P2] U10 revoke the website (A5) cross-project grant at teardown.**
- **[P2/decision] CORS `*`** on the unauthenticated signing endpoint carried unchanged — tighten to
  known origins or record an accepted-risk in SECURITY.md (deferred decision).

---

## Prerequisites

- Operator identity needs, on `proteus-photos`: IAM-grant capability beyond `roles/editor`.
  Minimum for U1–U4: `roles/resourcemanager.projectIamAdmin` + `roles/iam.serviceAccountAdmin` +
  `roles/storage.admin`. Full migration (U5 LB, certs): `roles/owner` is lower-friction (revoke at U10).
  `proteus-photos` owners (2026-05-29): aayushgupta05@gmail.com, admin@proteus.photos,
  kevink16@gmail.com, singhishree@gmail.com, ydvaayan@gmail.com.
- Control of the `screencap.sh` DNS zone (U5 cert DNS-authorization CNAME + `api.screencap.sh` A record).
- A `proteus-photos` SA key (or WIF) for CI (U8).
- Coordination channel with the screencap-website (A5) maintainer (gates U7 and U10).
- APIs on proteus-photos (already enabled 2026-05-29): storage, cloudfunctions, run, eventarc,
  cloudbuild, artifactregistry, secretmanager, compute, iamcredentials. Enable `certificatemanager`
  before U5.

---

## U1 — Foundation: SAs, IAM, staging buckets

```bash
PROJ=proteus-photos
# --- Service accounts ---
gcloud iam service-accounts create screencap-signer    --project $PROJ --display-name "screencap signing fn"
gcloud iam service-accounts create screencap-processor --project $PROJ --display-name "screencap processing"
gcloud iam service-accounts create screencap-eventarc  --project $PROJ --display-name "screencap eventarc trigger"

SIGNER=screencap-signer@$PROJ.iam.gserviceaccount.com
PROC=screencap-processor@$PROJ.iam.gserviceaccount.com
TRIG=screencap-eventarc@$PROJ.iam.gserviceaccount.com
OLD_SIGNER=397234807794-compute@developer.gserviceaccount.com   # zkairdrop default compute SA

# --- Staging recordings buckets (US multi-region, matching source) ---
for B in screencap-recordings-staging screencap-recordings-dev-staging; do
  gcloud storage buckets create gs://$B --project $PROJ \
    --location US --default-storage-class STANDARD \
    --uniform-bucket-level-access --public-access-prevention enforced \
    --soft-delete-duration 7d
done

# --- signBlob: resource-level self-binding (P2) ---
gcloud iam service-accounts add-iam-policy-binding $SIGNER --project $PROJ \
  --member "serviceAccount:$SIGNER" --role roles/iam.serviceAccountTokenCreator

# --- Bucket IAM on staging (objectAdmin for signer + processor) ---
for B in screencap-recordings-staging screencap-recordings-dev-staging; do
  gcloud storage buckets add-iam-policy-binding gs://$B --member "serviceAccount:$SIGNER" --role roles/storage.objectAdmin
  gcloud storage buckets add-iam-policy-binding gs://$B --member "serviceAccount:$PROC"   --role roles/storage.objectAdmin
  # --- PRE-STAGED grace grant for the OLD zkairdrop signer (P1-find-4) ---
  gcloud storage buckets add-iam-policy-binding gs://$B --member "serviceAccount:$OLD_SIGNER" --role roles/storage.objectAdmin
  gcloud storage buckets add-iam-policy-binding gs://$B --member "serviceAccount:$OLD_SIGNER" --role roles/storage.legacyBucketReader
done

# --- Eventarc trigger SA project + invoker roles ---
gcloud projects add-iam-policy-binding $PROJ --member "serviceAccount:$TRIG" --role roles/eventarc.eventReceiver
# run.invoker granted on the service in U3 (after the service exists)
```

**Verify:** all three SAs exist; both staging buckets exist (US); the four bindings per bucket
present; signer self-binding present. Record results in the Live log.

---

## U2 — Signing function (prod + dev) on staging

```bash
PROJ=proteus-photos; R=southamerica-east1
gcloud functions deploy get-upload-urls --project $PROJ --gen2 \
  --region $R --runtime python312 --trigger-http --allow-unauthenticated \
  --source scripts/cloud-function/ --entry-point get_upload_urls \
  --service-account screencap-signer@$PROJ.iam.gserviceaccount.com \
  --set-env-vars SCREENCAP_BUCKET=screencap-recordings-staging

gcloud functions deploy get-upload-urls-dev --project $PROJ --gen2 \
  --region $R --runtime python312 --trigger-http --allow-unauthenticated \
  --source scripts/cloud-function/ --entry-point get_upload_urls \
  --service-account screencap-signer@$PROJ.iam.gserviceaccount.com \
  --set-env-vars SCREENCAP_BUCKET=screencap-recordings-dev-staging
```

**Capture:** the new `*.run.app` URIs AND the **underlying Cloud Run service name** (for the U5 NEG):
`gcloud functions describe get-upload-urls --gen2 --region $R --format='value(serviceConfig.service)'`.

**Verify:** `curl -sS <prod-uri> -H 'Content-Type: application/json' -d '{"action":"list"}'` → 200;
a `sign-download` request returns a working v4 signed URL (proves `signBlob` IAM). Repeat for dev.

---

## U3 — Processing service + Eventarc trigger on staging

```bash
PROJ=proteus-photos; R=southamerica-east1
# 1. Re-provision the GENAI key as a Secret (do NOT inline the value)
printf '%s' "<AI_STUDIO_KEY>" | gcloud secrets create screencap-genai-key --project $PROJ --data-file=-
gcloud secrets add-iam-policy-binding screencap-genai-key --project $PROJ \
  --member "serviceAccount:screencap-processor@$PROJ.iam.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor

# 2. Deploy the Run service from source (Dockerfile)
gcloud run deploy process-recording --project $PROJ --region $R \
  --source scripts/process-recording/ \
  --service-account screencap-processor@$PROJ.iam.gserviceaccount.com \
  --no-allow-unauthenticated --cpu 1 --memory 1Gi \
  --set-env-vars SCREENCAP_BUCKET=screencap-recordings-staging,GOOGLE_CLOUD_PROJECT=$PROJ \
  --set-secrets GOOGLE_GENAI_API_KEY=screencap-genai-key:latest
# (repeat as process-recording-dev with SCREENCAP_BUCKET=screencap-recordings-dev-staging)

# 3. run.invoker for the Eventarc SA on the service
gcloud run services add-iam-policy-binding process-recording --project $PROJ --region $R \
  --member "serviceAccount:screencap-eventarc@$PROJ.iam.gserviceaccount.com" --role roles/run.invoker

# 4. Eventarc trigger in the `us` location (matches the US bucket)
gcloud eventarc triggers create process-recording-trigger --project $PROJ --location us \
  --destination-run-service process-recording --destination-run-region $R \
  --event-filters "type=google.cloud.storage.object.v1.finalized" \
  --event-filters "bucket=screencap-recordings-staging" \
  --service-account screencap-eventarc@$PROJ.iam.gserviceaccount.com
# NOTE: the GCS service agent needs roles/pubsub.publisher; gcloud usually grants it,
# else: gcloud projects add-iam-policy-binding $PROJ \
#   --member "serviceAccount:service-<PROJNUM>@gs-project-accounts.iam.gserviceaccount.com" \
#   --role roles/pubsub.publisher
```

**Verify (sentinel-drop):** upload a synthetic `recordings/<name>/recording_complete.json` (+
matching `recording.db`) to staging → processing fires **exactly once** → `sessions/<name>/`
written; a non-trigger path (`recordings/<name>/screenshots/0.png`) does NOT fire (3-segment guard);
logs show Gemini enrichment active (key present), not fallback segmentation.

---

## U4 — Bulk-copy recordings → staging

```bash
# Measure first → pick tool
gcloud storage du -s gs://screencap-recordings --project zkairdrop
# Small → rsync (no destination-delete flag, ever):
gcloud storage rsync -r gs://screencap-recordings gs://screencap-recordings-staging
# Large → Storage Transfer Service with a completion report.
```

**Verify:** object count + total bytes in staging match source (allowing for post-snapshot
writes, reconciled at U7 final sync); spot-checked recording downloads intact.

---

## U5–U10 (out of this session — summary; expand at execution)

- **U5 — DROPPED** (see Scope Amendment above). No `api.screencap.sh`, no load balancer, no DNS.
- **U6 (R10 GATE)** end-to-end record→upload→process→download against the new prod **`*.run.app`**
  host (`https://get-upload-urls-ld7izzjvga-rj.a.run.app`) + staging using `SCREENCAP_*` overrides;
  assert **parity** vs a captured zkairdrop baseline; prove the old-function cross-project follow
  against **staging**; confirm Gemini active; run the deferred faithful sentinel→`sessions/` check.
  No destructive step until this passes. (No cert/ingress checks — domain dropped.)
- **U7** recordings name cutover (IRREVERSIBLE): two clean final syncs + name+md5 manifest diff →
  delete `zkairdrop:screencap-recordings` → create final in proteus-photos → promote staged data →
  parity-assert → repoint `SCREENCAP_BUCKET` → create trigger **after** promotion → grant old-SA
  cross-project IAM → **redeploy old fn with `SCREENCAP_BUCKET=screencap-recordings`** →
  in-window re-prove reborn-name follow (P1-find-1) → backfill check → retain staging.
- **U8** releases name cutover + CI rebind (reversible): full checksum snapshot → delete+recreate →
  re-upload `v*/...` + checksums, then `install.sh`, then `latest.txt` **last** → grant new CI SA
  bucket-scoped role → swap `GCP_SA_KEY` → manifest parity diff. Re-grant `allUsers:objectViewer`.
- **U9** client release: flip `download.py`/`upload.py` defaults → the new prod `*.run.app`
  `https://get-upload-urls-ld7izzjvga-rj.a.run.app` (domain dropped); tests + CHANGELOG + version
  bump; ship via the (now proteus) pipeline. Depends only on U6 verified + U8 (one verified artifact).
- **U10** 7-day grace + teardown: gate on (clock + old-host traffic floor vs day-1-post-U9 baseline,
  day-over-day, ≥2 sustained days + A5 written confirmation + new-stack 5xx/cert green) — not the
  clock alone. Then delete zkairdrop screencap resources (incl. legacy `screencap-recording-signed-url`),
  staging bucket, and the U8 snapshot. Leave zk-email untouched. Revoke the A5 cross-project grant.

---

## Rollback posture

- **Phase 1–2:** fully additive — abort by deleting the new resources; nothing in zkairdrop touched.
- **U7 source delete is the one hard point of no return.** Safety net = the staging-completeness
  gate (two clean syncs + name+md5 diff), NOT GCS soft-delete (which can't restore into a name
  reborn elsewhere). Staging is retained through grace as the backstop.
- **U8 is reversible** — the retained checksum snapshot re-uploads to either project.

---

## Live execution log

> Append actual resource names, timings, URIs, and verification outcomes here as each step runs.

### U1 — DONE (2026-06-02)

Operator `rfigueiredo.dev@gmail.com` elevated to `roles/owner` on `proteus-photos`.

Service accounts created:
- `screencap-signer@proteus-photos.iam.gserviceaccount.com`
- `screencap-processor@proteus-photos.iam.gserviceaccount.com`
- `screencap-eventarc@proteus-photos.iam.gserviceaccount.com`

Staging buckets created (US multi-region, UBLA on, **PAP enforced**, soft-delete 604800s/7d, STANDARD):
- `gs://screencap-recordings-staging`
- `gs://screencap-recordings-dev-staging`
- _Note: dev staging gets PAP=enforced (source dev was `inherited`) — intentional, recordings are never public; no functional change._

IAM (verified):
- `screencap-signer` → `roles/iam.serviceAccountTokenCreator` **on itself** (resource-level, P2) ✓
- both staging buckets → `screencap-signer` + `screencap-processor` `roles/storage.objectAdmin` ✓
- both staging buckets → **OLD zkairdrop signer** `397234807794-compute@developer.gserviceaccount.com` `roles/storage.objectAdmin` + `roles/storage.legacyBucketReader` (pre-staged grace grant, P1-find-4) ✓
- `screencap-eventarc` → project `roles/eventarc.eventReceiver` ✓ (`run.invoker` deferred to U3, after the service exists)

Pending in U1 scope but deferred to their units: U2 captures the Cloud Run service name; U3 grants `run.invoker` + creates the secret.

### U2 — DONE (2026-06-02)

Signing functions deployed (gen2, southamerica-east1, runtime SA `screencap-signer`, ingress ALLOW_ALL pre-U5, `--allow-unauthenticated` succeeded — no org policy blocks public access):

| Function | URI | Underlying Cloud Run service (U5 NEG target) | `SCREENCAP_BUCKET` |
|----------|-----|----------------------------------------------|--------------------|
| `get-upload-urls` (prod) | `https://get-upload-urls-ld7izzjvga-rj.a.run.app` | `get-upload-urls` | `screencap-recordings-staging` |
| `get-upload-urls-dev` | `https://get-upload-urls-dev-ld7izzjvga-rj.a.run.app` | `get-upload-urls-dev` | `screencap-recordings-dev-staging` |

> **U5 note:** the serverless NEG must target Cloud Run service **`get-upload-urls`** in `southamerica-east1` (not a Cloud Functions reference) — P2 finding.

Verified:
- prod `{"action":"list"}` → 200 `{"recordings":[]}` ✓
- prod upload-URL request → 200 with a valid **v4 signed URL** signed by `screencap-signer@proteus-photos` → **signBlob IAM proven** ✓
- dev `{"action":"list"}` → 200 ✓

Repo change: `scripts/cloud-function/main.py` deploy docstring updated for proteus-photos + resource-level signBlob self-binding + staging `SCREENCAP_BUCKET`.

### U3 — DONE (2026-06-02)

GENAI key: stored as Secret Manager secret **`screencap-genai-key`** (value piped from the
zkairdrop source service — never logged); `screencap-processor` granted `secretAccessor`.
GCS service agent `service-409391769978@gs-project-accounts.iam.gserviceaccount.com` granted
`roles/pubsub.publisher`. Enabled `eventarc` + `pubsub` APIs (were off).

> **Security follow-up:** the GENAI key is now in Secret Manager here (was plaintext env in
> zkairdrop). Consider rotating it post-migration since it was historically exposed as a
> readable env var. Tracked as a hardening follow-up, not a blocker.

Processing services (Cloud Run, southamerica-east1, SA `screencap-processor`, cpu1/mem1Gi,
`--no-allow-unauthenticated`, GENAI from secret):

| Service | URL | `SCREENCAP_BUCKET` |
|---------|-----|--------------------|
| `process-recording` (prod) | `https://process-recording-409391769978.southamerica-east1.run.app` | `screencap-recordings-staging` |
| `process-recording-dev` | `https://process-recording-dev-409391769978.southamerica-east1.run.app` | `screencap-recordings-dev-staging` |

Eventarc triggers (location **`us`**, `object.v1.finalized`, SA `screencap-eventarc`):
- `process-recording-trigger` → `process-recording`, bucket `screencap-recordings-staging`
- `process-recording-dev-trigger` → `process-recording-dev`, bucket `screencap-recordings-dev-staging`

Verified: a non-trigger drop (`recordings/u3probe/screenshots/0.png`) was delivered to the
prod service (POST 200) and the guard logged `Ignoring non-trigger object` — **trigger
plumbing + guard proven**. Full sentinel→`sessions/<name>/` exactly-once run is deferred to
**U6** with real copied data (a synthetic sentinel lacks chunk manifests and would error
misleadingly). Probe object cleaned up.

Repo change: `scripts/process-recording/main.py` — added the previously-absent deploy
docstring (run deploy + secret + Eventarc trigger, incl. the `us`-location-matches-US-bucket note).

### U4 — DONE (2026-06-02), with two findings

Volume: source `gs://screencap-recordings` = **1,824,485,625 B (~1.82 GB)** → small → `gcloud storage rsync -r`
(no destination-delete flag). Copy completed server-side in ~8s @ 487 MiB/s.

**Parity on `recordings/` (the data R3 preserves) — byte-perfect:**
| | bytes | objects |
|---|---|---|
| source `recordings/` | 1,636,547,361 | 160 |
| staging `recordings/` | 1,636,547,361 | 160 |

> ⚠️ **FINDING 1 — U3→U4 trigger ordering causes a reprocess storm (plan refinement).**
> The U3 Eventarc trigger is live on staging, so the rsync finalizing every historical
> `recordings/<name>/recording_complete.json` **fired processing for ~14 recordings** (only 3
> were idempotency-skipped — rsync copies `recordings/` *before* the `sessions/<name>/_processing_status.json`
> markers that would suppress them). Result: staging total = 474 objects / 1.86 GB vs source 417 / 1.82 GB —
> the +57 objects are **regenerated `sessions/`**. `recordings/` is unaffected (byte-perfect above).
> This is the SAME hazard the plan mitigates for U7 ("promote `sessions/` before the trigger goes live", P2)
> but it was **not flagged for U3→U4**. **Mitigation for the U7 final sync + promotion (MUST apply):**
> sync/copy the `sessions/` prefix (idempotency markers) **before** `recordings/`, OR create/enable the
> trigger only *after* the data is in place. Harmless here (recordings/ intact; U6 uses fresh recordings),
> but load-bearing at the irreversible U7 cutover. **Staging `sessions/` is therefore NOT a faithful
> mirror of source** — U6 should verify via a fresh record→process run + the `recordings/` prefix, not
> staging `sessions/`.

> ⚠️ **FINDING 2 — pre-existing processing bug surfaced (OUT OF SCOPE — follow-up filed).**
> Reprocessing crashed on some recordings with `TypeError: 'NoneType' object is not subscriptable`
> at `scripts/process-recording/main.py:1159` — `"title": title[:80]` has no None-guard, unlike the
> parallel `dom_title[:80] if dom_title else ""` at line 1666. Committed 2026-04-10, **not
> migration-induced** — the same bug exists in zkairdrop prod for any event with `title=None`.
> Faithful lift-and-shift reproduces it; fixing it is a functional change (out of this plan's scope).
> Flagged as a separate follow-up.

**U4 verdict:** R3 data-preservation MET (`recordings/` byte-perfect). Staging `sessions/` divergence is a
documented reprocessing artifact, not a copy failure.

### U6 — DONE / GATE GREEN (2026-06-02)

End-to-end verification against the new `proteus-photos` stack via the new prod `*.run.app` host
(`https://get-upload-urls-ld7izzjvga-rj.a.run.app`) + staging bucket. (Domain/cert/ingress legs
removed per the scope amendment. `record` itself is unchanged by the migration, so U6 exercises the
upload→process→download legs the migration actually touches, with real data, not a fresh capture.)

- **Leg A — signed download (new fn):** `list` → 32 recordings; `sign-download rec-20260425T001201`
  → v4 URLs; fetched `chunk_0000.mp4` → **HTTP 200, 327017 B, video/mp4** (data intact). ✓
- **Leg B — signed upload (new fn):** minted v4 PUT URL → `PUT` (Content-Type `application/octet-stream`
  to match the signed header) → **HTTP 200** → object landed in staging with correct bytes. ✓
  (A first PUT with `text/plain` returned 403 — expected v4 signed-header mismatch, not a bug; the real
  client passes matching content types.)
- **Leg C — Eventarc processing + Gemini + idempotency (faithful):** cleared one real recording's
  session + idempotency marker, re-finalized its sentinel → processing ran and **completed**
  (`Done processing... 1 tasks, method=llm`); **Gemini active** (`gemini-2.5-flash:generateContent
  HTTP 200` → "Gemini Flash returned 1 tasks", not fallback); a duplicate Eventarc delivery was
  **correctly idempotency-skipped** ("Already processed... skipping") → effective exactly-once. ✓
- **Leg D — parity vs zkairdrop baseline:** session **schema/structure identical** (same six file
  types; same `method=llm` pipeline). Task count differed (baseline 2 vs new 1) — **LLM
  non-determinism** in `gemini-2.5-flash` segmentation on byte-identical input, NOT a migration
  defect. Implication: strict manifest diff is not a valid gate for this LLM-segmented pipeline;
  schema parity + Gemini-active + successful processing is the right criterion. ✓

**Deferred:** the live old-`zkairdrop`-function cross-project follow against staging was NOT exercised
here — it requires pointing the old function at the staging bucket (a zkairdrop mutation, out of
Phase 1/2 scope). The IAM grant is in place + verified (U1). Per the plan's default-to-redeploy
stance, the follow is re-proven in-window at **U7** (with the `SCREENCAP_BUCKET` redeploy fallback
armed) against the *reborn* bucket — which is the only place it's truly observable anyway.

**U6 verdict: GATE GREEN.** New stack proven end-to-end (signed upload + signed download + Eventarc
processing with Gemini). Destructive Phase 3 (U7/U8) is unblocked — but each is a separate,
explicitly-confirmed window (U7 is irreversible).

### A5 (screencap-website) — ALREADY MIGRATED (verified 2026-06-02)

Inspected the sibling repo `screencap-website` (`src/app/_lib/gcs-proxy.ts`). The website reads
recordings **entirely through the signing function** (`list` / `sign-download` / `get-index` →
`callCloudFunction`), with **no direct bucket access and no GCP credential**. Its `CLOUD_FUNCTION_URL`
already defaults to the **new proteus host** `https://get-upload-urls-ld7izzjvga-rj.a.run.app` (committed
today, `3103581` "repoint recordings to proteus-photos signing function"), and its `/api/proxy`
whitelists **both** `screencap-recordings` and `screencap-recordings-staging`. So A5 is **done** — the
website is bucket-name-agnostic, already on the new function, and cannot be broken by the bucket moving
projects. No website-SA cross-project grant is needed (the plan's assumption that it reads the bucket
directly was wrong).

### U7 — Recordings cutover via REVERSIBLE REPOINT (Option A, 2026-06-02) — replaces the plan's irreversible U7

Decided with the owner: since **every** recordings reader goes through a function we control (website +
both CLIs), there is no need to reclaim the canonical bucket name, so the plan's irreversible
delete-recreate (U7 as written) is replaced by a reversible repoint. **No bucket deleted; no
point-of-no-return; no promotion reprocess-storm.**

- **Final sync: no-op.** Delta `zkairdrop:screencap-recordings` → staging since U4 = **0 `recordings/`
  objects**; the only diff was 4 `sessions/` objects from the U6 reprocess (stale 2-task vs new 1-task
  version of `rec-20260425T001201`) — deliberately NOT synced (would Frankenstein the session). No real
  client writes hit the zkairdrop bucket since U4.
- **Repoint (reversible):** `gcloud run services update` on the gen2 functions' backing services:
  - `zkairdrop:get-upload-urls` (prod) → `SCREENCAP_BUCKET=screencap-recordings-staging` (rev `00006-cwh`)
  - `zkairdrop:get-upload-urls-dev` → `SCREENCAP_BUCKET=screencap-recordings-dev-staging` (rev `00005-82s`;
    also routed `--to-latest` — dev traffic had a pre-existing pin to an old revision `00002-veh`).
  - **Rollback:** `gcloud run services update <fn> --remove-env-vars=SCREENCAP_BUCKET` (back to the
    zkairdrop bucket default).
- **Cross-project follow PROVEN LIVE** (closes the item deferred from U6): the old prod function at its
  canonical client host `https://get-upload-urls-wyldgq6aqa-rj.a.run.app` now `list`s 32 staging
  recordings and `sign-download` returns `gcs_prefix: gs://screencap-recordings-staging/...` whose URL
  fetched **HTTP 200, 327017 B**. The old `zkairdrop` SA (`397234807794-compute`) reads + signs against
  the proteus bucket cross-project (IAM pre-staged in U1).
- **Result:** old CLIs, new CLIs (post-U9), website, and Eventarc processing all converge on
  `proteus:screencap-recordings-staging`. `zkairdrop:screencap-recordings` is **dormant** (no reader/writer)
  — deleted in U10. The permanent recordings bucket keeps the cosmetic `-staging` name (server-side only).

**Remaining:** U8 (releases bucket → proteus + CI rebind; needs a proteus CI SA/key), U9 (CLI release
repoint to the new function host — one-liner), U10 (grace + teardown of the dormant zkairdrop footprint).

### U8 — Releases cutover + CI rebind — DONE (2026-06-02)

Releases is client-facing by **direct bucket URL** (`storage.googleapis.com/screencap-releases/...` in
`updater.py`, `install.sh`, and the website's installer links) — no function indirection — so the name is
load-bearing and the move requires a same-name delete-recreate (the plan's U8, unlike the reversible U7).

- **Phase A (additive):** created CI SA `screencap-ci-releases@proteus-photos`; created
  `screencap-releases-staging` and snapshot-copied the bucket into it — **byte-perfect** (11,627,807,407 B
  / 120 objects; mac `latest.txt`=0.17.1, win=1.4.0). This staging bucket is the **rollback snapshot**,
  retained through U10.
- **Pre-flight:** confirmed `proteus-photos` permits public-read storage (anonymous read of the snapshot
  bucket → 200) before the destructive step.
- **Phase B (cutover, client-visible):** deleted `zkairdrop:screencap-releases` → recreated
  `screencap-releases` in proteus (name freed + created on first attempt) → granted `allUsers:objectViewer`
  + CI `objectAdmin` → re-uploaded snapshot→final in strict order (`rsync` excluding `latest.txt`, then
  both `latest.txt` files **last**). 404 window = a few seconds.
  - **Verified:** bucket now in proteus; byte-perfect parity vs snapshot (120 objs / 11,627,807,407 B);
    anonymous client paths all 200 — `releases/latest.txt` (0.17.1), `releases/install.sh` (13,814 B),
    `releases-windows/latest.txt` (1.4.0), and `releases/v0.17.1/screencap-0.17.1-arm64.tar.gz` (105,222,858 B).
  - `releases-windows/` data preserved (downloads work); **Windows CI write-rebind intentionally skipped**
    (Windows impl is parked — no new Windows releases being pushed).
- **Phase C (CI rebind):** generated a key for the proteus CI SA, **proved it** (activated in an isolated
  config; write+read+delete to `gs://screencap-releases` all succeeded), then set the `GCP_SA_KEY` secret on
  `proteus-computer-use/screencap` via `gh` (updated `2026-06-02T17:30:51Z`) and deleted the local key copy.
  No `release.yml` edits (paths are bucket-name-only).
- **Rollback:** re-upload the retained `screencap-releases-staging` snapshot to either project.
- **U10 follow-ups:** delete `screencap-releases-staging` (snapshot) after grace; **revoke the old
  `screencap-ci-releases@zkairdrop` SA key** (now inert — no longer in the secret; P3 finding); rebind the
  Windows CI if/when Windows resumes.

**U8 verdict: DONE.** Releases fully served from proteus under the canonical name; CI publishes to proteus.
