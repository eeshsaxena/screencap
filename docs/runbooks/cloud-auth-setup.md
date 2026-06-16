---
title: "Runbook: Firebase / Identity Platform provisioning for per-user cloud storage isolation"
date: 2026-05-29
type: runbook
plan: docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
origin: docs/brainstorms/2026-05-29-per-user-cloud-storage-isolation-requirements.md
status: done
project: proteus-photos
region_compute: southamerica-east1
unit: U1
---

# Runbook: auth provider provisioning (U1)

Operational source of truth for **provisioning the Firebase / Identity Platform
tenant** that backs per-user cloud storage isolation. The repo has no IaC, so this
file is the durable record of *what was provisioned, where, and which values the
client (U4) and the signing function (U1/U2) consume*.

Everything lives in **`proteus-photos`** — the single project that already holds the
`screencap-recordings` bucket and the `get-upload-urls` signing function (see
`docs/runbooks/gcp-migration-proteus-photos.md`). This is a single-project build; no
cross-project sequencing.

> **Captured values go in the "Live execution log" at the bottom.** The OAuth client
> id and Firebase Web API key produced here are the inputs to U4's client config.

---

## Deploy procedure (SCR-137) — restore the website

> **Status (2026-06-16): the earlier "do not deploy" gate is LIFTED.** SCR-138 (the
> website repoint) shipped, so `screencap-website` now calls the tokenless `demo-*`
> actions — but the live prod `get-upload-urls` still runs the OLD pre-isolation code
> (verified: `POST {"action":"demo-list"}` → **HTTP 400**), so the public gallery is
> broken. Deploying the merged `scripts/cloud-function/` code to prod is now the **fix**,
> not a hazard to defer. The old options (hold / `get-upload-urls-v2` / a
> `SCREENCAP_AUTH_ENABLED` flag) are **void** — see "Why deploying is now safe".

### Why deploying is now safe (the old "401s all clients" risk is moot)

The token-gated `upload` / `list` / `sign-download` actions require a Firebase bearer
token, but **no shipped client can produce one**: `src/screencap/auth.py` still carries
the `REPLACE_WITH_PROVISIONED_*` placeholders, and no released tag carries `authed_post`
(verified absent through v0.20.0). So there is **no working user-upload flow to break**.
Deploying the new code:

- turns the tokenless `demo-*` actions live (the website fix), and
- lights up the token-gated `users/{uid}/…` path for *future* token-carrying releases
  (enabled by the SCR-137 client-upload track — build-time cred injection, U1/U2).

The website uses only the tokenless `demo-*` path, so the website fix does **not** depend
on the client-upload track.

> Note on the retired `get-index`: the new dispatcher 400s `{"action":"get-index"}`. The
> repointed website no longer calls it (SCR-138 dropped the `sessions/` routes), and no
> token-carrying client ships, so nothing live depends on it. `download.py` still defines
> `fetch_session_index` / `list_remote_sessions` (which degrade to an empty index) — their
> removal rides with the deferred token-carrying client release, not this deploy.

### Live targets (verified 2026-06-16 via `gcloud functions describe`)

| | Function | URL | Bucket | SA |
|---|---|---|---|---|
| **prod** | `get-upload-urls` | `get-upload-urls-ld7izzjvga-rj.a.run.app` | `screencap-recordings-staging` | `screencap-signer@proteus-photos` |
| **dev** | `get-upload-urls-dev` | `get-upload-urls-dev-ld7izzjvga-rj.a.run.app` | `screencap-recordings-dev-staging` | `screencap-signer@proteus-photos` |

Both are gen2 / python312 / region `southamerica-east1`. Neither currently sets
`SCREENCAP_PROJECT_ID` (old code) — **set it explicitly on every deploy**
(`_resolve_project_id()` defaults to the literal `proteus-photos`, correct here, but
pinning it is the documented guard against an off-project deploy).

### ⚠️ `demo/` content pre-check (blocking for *videos*, not for *stops-erroring*)

`gs://screencap-recordings-staging/demo/` is **empty** (verified 2026-06-16; the bucket
holds only `recordings/` + `sessions/`). So after the prod deploy the website will stop
erroring but render the **empty-gallery placeholder**, not videos. Showing videos needs
the `demo/` promotion — **SCR-139** (no migration scripts in-repo yet). When reporting the
result, state it explicitly: "deploy OK, `demo/` content pending (SCR-139)" so an empty
gallery is not mistaken for a failed deploy.

### Sequence

1. **Dev-validate first** (U4) — deploy to `get-upload-urls-dev` (non-prod bucket), then
   confirm `demo-*` dispatch + the tokenless-401 invariant + the token round-trip. Do not
   let prod be the first live test of the new code. Recorded in the Live execution log.
2. **Deploy to prod** (U5) — deploy over `get-upload-urls`, then verify the website + the
   tokenless-401 invariant. Recorded in the Live execution log.

The deploy command is the one in the `scripts/cloud-function/main.py` header; both the dev
and prod invocations + their verification/rollback are captured in the **Live execution
log** at the bottom of this runbook.

### Verification (both dev and prod)

- `firebase_admin initialized for project proteus-photos` in the function logs.
- `POST {"action":"demo-list"}` returns **200** with a (possibly empty) list — not 400.
- Tokenless `upload` / `list` / `sign-download` return **401** (the in-code gate; a CI
  contract test asserts no tokenless request reaches a `users/` path).
- A same-project Firebase token mints + verifies → uid; a foreign-project token → 401
  (origin AE2 — a cross-user/foreign read is indistinguishable from "not found").
- (prod) the website gallery loads via `demo-list` and a `demo-sign-download` plays —
  *iff* `demo/` is populated (else the empty-gallery placeholder, per the pre-check).

### Rollback

Redeploy the previous (pre-isolation) function revision if `demo-*` or signing misbehaves
(`gcloud run revisions list --region southamerica-east1`, then redeploy the prior source).
**No recording data is mutated by the function**, so rollback is safe and stateless.

### Client-upload track operator setup (required before the NEXT release of any kind)

Add two **repository Secrets** (GitHub → Settings → Secrets and variables → Actions):
`SCREENCAP_OAUTH_CLIENT_ID` and `SCREENCAP_FIREBASE_API_KEY` (values in the gitignored
`.env`; non-secret by design). These let a shipped binary sign in (`screencap login`).

**This is not optional for token-carrying releases only.** The release workflow's "Generate
provisioned credentials" step runs **unconditionally** on every tag, and it **fails the
entire release build** (writing nothing) if either Secret is absent. So both Secrets must be
present in the repo before the **next release tag of any kind** — not just a token-carrying
one — or the build breaks. After injection, the fail-closed guard (`_auth-config-check`)
blocks a placeholder binary. For local-release, export the same two values before building
(see `.claude/skills/local-release/SKILL.md`).

---

## Preconditions

- [ ] `gcloud` authenticated as an owner/editor of `proteus-photos`
      (`gcloud config set project proteus-photos`).
- [ ] Billing is **enabled** on the project. Enabling Identity Platform requires the
      **Blaze (pay-as-you-go)** plan. Auth is *not charged* at small scale (free to
      ~50k MAU) — billing-enabled ≠ billed. Phone/SMS auth costs money and is **not
      used**; leave it disabled.
- [ ] The signing function's service account is known:
      `screencap-signer@proteus-photos.iam.gserviceaccount.com`.

---

## Steps

### 1. Enable Identity Platform + Firebase Authentication

1. Console → **Identity Platform** → *Enable* (or `gcloud services enable
   identitytoolkit.googleapis.com --project proteus-photos`).
2. This upgrades the project's auth to Identity Platform while keeping the Firebase
   Auth REST surface (`identitytoolkit.googleapis.com`, `securetoken.googleapis.com`)
   the client uses.

### 2. Configure the Google sign-in provider

1. Identity Platform → **Providers** → *Add provider* → **Google** → enable.
2. Google is the only v1 provider (the seam is provider-agnostic but only one provider
   is wired). Do **not** enable Apple/email-password/phone here.

### 3. Disable unused auth methods (credential-stuffing / enumeration surface)

The Web API key ships in every client binary, so close the methods we don't use:

- [ ] **Email/Password** — disabled.
- [ ] **Phone** — disabled (also avoids SMS cost).
- [ ] **Anonymous** — disabled.
- [ ] Email enumeration protection — leave Identity Platform's default
      ("protect against account enumeration") **on**.

### 4. Create a Desktop/native (public) OAuth client

The CLI uses system-browser loopback OAuth (RFC 8252) + PKCE — a **public** client with
**no client secret**.

1. Console → **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Desktop app** (native/public client).
3. Name it e.g. `screencap-cli-desktop`.
4. **Capture the client id.** A Desktop client may show a "client secret" in the console,
   but for a public native client per RFC 8252 we treat it as **non-secret and do NOT
   embed it as a secret**: PKCE (`code_challenge`) is the protection, not the secret.
   - [ ] **Confirm no `client_secret` is committed to source or shipped in a binary.**
         Only the client id (and the Web API key) ship in the client.

### 5. Capture the Firebase Web API key

1. Console → **Project settings → General → Web API Key** (a.k.a. the
   `apiKey` / "browser key").
2. This key authorizes calls to `identitytoolkit.googleapis.com`
   (`accounts:signInWithIdp`) and `securetoken.googleapis.com` (token refresh).
   - [ ] **Capture the Web API key.**

### 6. Restrict the Web API key

The key ships in every binary, so scope it down:

1. Console → **Credentials →** the Web API key → *Edit*.
2. **API restrictions** → restrict to **Identity Toolkit API** and **Token Service API**
   (`securetoken`) only — nothing else.
3. **Application restrictions** — apply the tightest restriction the desktop flow tolerates
   (referrer/app restriction where applicable). Record what was chosen.

### 7. Confirm the signer can sign v4 URLs

The signing function already self-binds `roles/iam.serviceAccountTokenCreator` (see the
deploy header in `scripts/cloud-function/main.py`). Re-confirm it is present:

```bash
SIGNER=screencap-signer@proteus-photos.iam.gserviceaccount.com
gcloud iam service-accounts get-iam-policy "$SIGNER" --project proteus-photos \
  --format='table(bindings.role, bindings.members)' | grep serviceAccountTokenCreator || \
  gcloud iam service-accounts add-iam-policy-binding "$SIGNER" \
    --project proteus-photos --member "serviceAccount:$SIGNER" \
    --role roles/iam.serviceAccountTokenCreator
```

No Admin-API credentials are needed for token verification — `check_revoked=False` is a
pure JWT + public-cert check.

### 8. Deploy the function with `firebase-admin`

The function's `requirements.txt` now pins `firebase-admin>=7.4.0` and
`google-cloud-storage>=3.1.1` (a forced major bump). Re-deploy per the header in
`main.py`. On init the function logs `firebase_admin initialized for project
proteus-photos` — grep the logs to confirm the pinned project is correct.

```bash
gcloud functions logs read get-upload-urls --project proteus-photos \
  --region southamerica-east1 --limit 20 | grep "firebase_admin initialized"
```

---

## Verification

- [ ] `firebase_admin initialized for project proteus-photos` appears in the function logs.
- [ ] A valid same-project ID token → `verify_bearer` returns a uid (proven by the U2
      handler tests / a manual signed call once U2 lands).
- [ ] A token minted for any other project → 401 (aud/iss mismatch).
- [ ] The OAuth client is **Desktop/native**; no `client_secret` is in source or binaries.
- [ ] The Web API key is restricted to Identity Toolkit + Token Service only.
- [ ] Email/password, phone, and anonymous sign-in are disabled.

---

## Live execution log

> Provisioned 2026-06-04 in `proteus-photos`. The OAuth client id + Web API key are
> **not committed** here — they live in the project-local `.env` (gitignored,
> auto-loaded by `load_dotenv()` in `screencap/cli/__init__.py`) as
> `SCREENCAP_OAUTH_CLIENT_ID` / `SCREENCAP_FIREBASE_API_KEY`. They are not secrets
> (they ship in release binaries). For a shipped client they are **injected at build
> time** into a gitignored `screencap._provisioned` module (SCR-137 U1/U2) — source
> keeps the placeholders, nothing credential-looking is committed. The function deploy
> is **no longer gated** — see "Deploy procedure (SCR-137)" above; dev + prod deploy
> entries are logged below.

| Item | Value | Notes |
|------|-------|-------|
| Identity Platform enabled | ✅ done (2026-06-04) | console shows "Authentication with Identity Platform" |
| Google provider enabled | ✅ done (2026-06-04) | the only v1 provider |
| OAuth client id (desktop) | ✅ `screencap-cli-desktop` (Desktop type, 2026-06-04) | value in project-local `.env`; not committed; not a secret |
| Firebase Web API key | ✅ created (2026-06-04) | value in project-local `.env`; not committed |
| Web API key restrictions | ✅ done (2026-06-04) | restricted to Identity Toolkit API + Token Service API (step 6) |
| Disabled methods | ✅ email/pw, phone, anonymous left disabled | only Google was enabled |
| `serviceAccountTokenCreator` confirmed | ✅ done (2026-06-04) | self-binding on the signer SA present (step 7 `gcloud` check) |

**Provisioning (steps 1–7) is complete.** The function deploy is no longer gated:
SCR-138 shipped and the live website is broken on old code, so deploying the merged
function is the fix (safe because no shipped client carries a token — see "Deploy
procedure (SCR-137)" above). Dev-validate first, then prod; both are logged in the
**Deploy log** below. Client sign-in (`screencap login` / `whoami`) works in dev today
with the two `.env` values set; in a shipped binary it works once U1/U2 inject them.

### Deploy log (SCR-137)

| Target | Date | Outcome |
|--------|------|---------|
| dev (`get-upload-urls-dev`, bucket `screencap-recordings-dev-staging`) | 2026-06-16 | ✅ deployed merged code. Automated checks: `demo-list`→**200** `{"recordings":[]}` (was 400 on old code); tokenless `upload`/`list`/`sign-download`→**401**; invalid bearer→**401** (proves `verify_bearer`/`firebase_admin` live + project-pinned — an init failure would 503/500); unknown action→**400**. `demo/` empty (SCR-139). Same-project-token→uid signed round-trip = interactive operator step (below); not yet run. |
| prod (`get-upload-urls`, bucket `screencap-recordings-staging`) | 2026-06-16 | ✅ deployed merged code (new revision `get-upload-urls-00002-faq`; rollback target `get-upload-urls-00001-xip`). Verified: `demo-list`→**200** `{"recordings":[]}` (was 400 on old code); tokenless `upload`/`list`/`sign-download`→**401**; invalid bearer→**401**; unknown action→**400**. Website `demo-*` calls now succeed; `demo/` empty → gallery renders the empty-gallery placeholder until SCR-139. |

Deploy command used (dev):

```bash
SIGNER=screencap-signer@proteus-photos.iam.gserviceaccount.com
gcloud functions deploy get-upload-urls-dev \
  --project proteus-photos --gen2 --runtime python312 \
  --trigger-http --allow-unauthenticated --region southamerica-east1 \
  --source scripts/cloud-function/ --entry-point get_upload_urls \
  --service-account "$SIGNER" \
  --update-env-vars SCREENCAP_BUCKET=screencap-recordings-dev-staging,SCREENCAP_PROJECT_ID=proteus-photos
```

(`--update-env-vars`, not `--set-env-vars`, so the platform-managed `LOG_EXECUTION_ID`
is preserved. `SCREENCAP_PROJECT_ID` is added explicitly per the `main.py` header.)

#### Interactive token round-trip (operator step — needs a browser)

The automated checks above cover `demo-*` dispatch + tokenless/invalid-token denial. The
remaining same-project-token → uid → signed round-trip needs a real Google sign-in, so run
it manually against the dev function from a **token-carrying client** (HEAD source already
threads bearer tokens via `auth.authed_post`; released binaries do so once U1/U2 inject the
creds). From the repo root:

```bash
set -a; . ./.env; set +a   # real SCREENCAP_OAUTH_CLIENT_ID + SCREENCAP_FIREBASE_API_KEY
# Point BOTH client knobs at dev — upload.py reads SCREENCAP_UPLOAD_URL, download.py reads
# SCREENCAP_DOWNLOAD_URL; setting only one leaves the other leg on prod:
export SCREENCAP_UPLOAD_URL=https://get-upload-urls-dev-ld7izzjvga-rj.a.run.app
export SCREENCAP_DOWNLOAD_URL=https://get-upload-urls-dev-ld7izzjvga-rj.a.run.app
screencap login            # browser → Google → Firebase; stores the refresh token
screencap whoami           # expect: signed in as <you>
screencap upload <name>    # authed PUT under users/{uid}/…
screencap download <name>  # authed list (own recordings only) + signed GET; checksum holds
```

Expect: login succeeds; upload lands under `users/{uid}/…`; the list returns only your own
recordings; download round-trips with matching checksums (google-cloud-storage 3.x crc32c).
Foreign-project / cross-user denial (AE2) is already proven by the invalid-bearer→401 above.

#### Deploy to prod (operator step — the website fix)

Once dev validates, deploy the SAME source over the live prod function. This restores the
website's `demo-*` actions. Run from the repo root:

```bash
# 1. PRE-CHECK demo/ content (videos vs empty-gallery placeholder):
gcloud storage ls "gs://screencap-recordings-staging/demo/"   # empty today → SCR-139 pending

# 2. DEPLOY (mirrors the dev command; prod bucket; explicit project pin):
SIGNER=screencap-signer@proteus-photos.iam.gserviceaccount.com
gcloud functions deploy get-upload-urls \
  --project proteus-photos --gen2 --runtime python312 \
  --trigger-http --allow-unauthenticated --region southamerica-east1 \
  --source scripts/cloud-function/ --entry-point get_upload_urls \
  --service-account "$SIGNER" \
  --update-env-vars SCREENCAP_BUCKET=screencap-recordings-staging,SCREENCAP_PROJECT_ID=proteus-photos

# 3. VERIFY (same checks as dev, against the prod URL):
PROD=https://get-upload-urls-ld7izzjvga-rj.a.run.app
curl -s -w '\n%{http_code}\n' -X POST "$PROD" -H 'Content-Type: application/json' -d '{"action":"demo-list"}'        # expect 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$PROD" -H 'Content-Type: application/json' -d '{"action":"list"}'  # expect 401
# Then load the website: the gallery should stop erroring (empty-gallery placeholder
# until SCR-139 populates demo/).
```

**Rollback (prod):** the pre-deploy serving revision is `get-upload-urls-00001-xip`
(2026-06-02, the old code). Restore it with:

```bash
gcloud run services update-traffic get-upload-urls \
  --project proteus-photos --region southamerica-east1 \
  --to-revisions get-upload-urls-00001-xip=100
```

No recording data is mutated by the function, so rollback is safe and stateless. After a
successful prod deploy, update the prod row above (date + outcome) and coordinate **SCR-139**
so the gallery shows videos, not just the empty-gallery placeholder.

---

## Rollback

Provisioning is additive and non-destructive to existing storage. To back out:
disable the Google provider, delete the OAuth client, and revert the function to a
`requirements.txt` without `firebase-admin` (the pre-U2 handlers ignore tokens). No
recording data is touched by anything in this runbook.
