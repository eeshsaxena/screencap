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

## Pre-deploy gate (CRITICAL — read before deploying the function)

The U2 function code **requires a Firebase bearer token** on `upload` / `list` /
`sign-download`, **removes** the `get-index` action, and reshapes `gcs_prefix` to
`users/{uid}/…`. The matching token-carrying clients (U5 remainder) and the website
demo-repoint (U7) are deferred to follow-ups. So deploying this code **over the live
`get-upload-urls`** would break every currently-shipped client:

- The current CLI (`upload.py` / `download.py`) and website send **no** bearer token →
  every real-user action returns **401**.
- `download.py`'s `fetch_session_index()` / `list_remote_sessions()` still POST
  `{"action": "get-index"}`; the dispatcher now 400s it. (`fetch_session_index`
  degrades to an empty index, so `list --remote` shows "no sessions" rather than
  erroring — but `download --sessions` and all token-gated actions hard-fail.)

The code is correct; this is a **deploy-sequencing hazard**, not a code defect. Pick
ONE and do not deploy to the live function until it holds:

1. **Sequence (default):** do not deploy over the live `get-upload-urls` until U5 (token
   threading in `upload.py` / `download.py`) and U7 (website → `demo-*`) have shipped
   and a token-carrying client is released.
2. **New function name:** deploy as a *separate* function (e.g. `get-upload-urls-v2`)
   and cut clients over only once they send tokens; retire the old function afterward.
3. **Feature flag:** add a `SCREENCAP_AUTH_ENABLED` env so the deployed code falls
   through to the legacy unauthenticated path until clients are ready.

Also: when U5 removes the `--remote` / `--sessions` surface, remove
`fetch_session_index` / `list_remote_sessions` from `download.py` so no shipped client
calls the retired `get-index` action.

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
> (they ship in release binaries); for a shipped client they move into `auth.py`'s
> defaults. Function deploy remains **gated** — see the Pre-deploy gate above.

| Item | Value | Notes |
|------|-------|-------|
| Identity Platform enabled | ✅ done (2026-06-04) | console shows "Authentication with Identity Platform" |
| Google provider enabled | ✅ done (2026-06-04) | the only v1 provider |
| OAuth client id (desktop) | ✅ `screencap-cli-desktop` (Desktop type, 2026-06-04) | value in project-local `.env`; not committed; not a secret |
| Firebase Web API key | ✅ created (2026-06-04) | value in project-local `.env`; not committed |
| Web API key restrictions | ✅ done (2026-06-04) | restricted to Identity Toolkit API + Token Service API (step 6) |
| Disabled methods | ✅ email/pw, phone, anonymous left disabled | only Google was enabled |
| `serviceAccountTokenCreator` confirmed | ✅ done (2026-06-04) | self-binding on the signer SA present (step 7 `gcloud` check) |

**Provisioning (steps 1–7) is complete.** The only remaining item is the
deliberately-**gated function deploy** (step 8 / Pre-deploy gate above) — deploying
the token-verifying code over the live `get-upload-urls` would 401 current clients,
so it waits on a token-carrying client release / a new function name / an auth flag.
Client sign-in (`screencap login` / `whoami`) works today without the deploy once the
two `.env` values are set.

---

## Rollback

Provisioning is additive and non-destructive to existing storage. To back out:
disable the Google provider, delete the OAuth client, and revert the function to a
`requirements.txt` without `firebase-admin` (the pre-U2 handlers ignore tokens). No
recording data is touched by anything in this runbook.
