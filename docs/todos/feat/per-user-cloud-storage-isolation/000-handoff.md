---
title: "Handoff: continuing per-user cloud storage isolation after U5"
type: handoff
status: open
created: 2026-06-03
updated: 2026-06-03
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_runbooks:
  - docs/runbooks/cloud-auth-setup.md
---

# Handoff: per-user cloud storage isolation

The **backend security boundary + the full client auth path** are now done. Units
U1–U5 have shipped:

- **U1–U4 + the U5 self-capture sub-scope** merged into `main` via PR #210.
- **The U5 remainder** shipped on branch `feat/per-user-cloud-storage-isolation-u5`
  (8 commits, pending merge). Read the plan's **`## Execution Status`** first
  (`docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md`) — it
  lists every unit's state and the commit SHAs.

The remaining work is **U6 onward** (client + website + migration). Nothing below
blocks on more client auth plumbing.

## Already shipped (U1–U5)

- **U1–U3** `scripts/cloud-function/` — `verify_bearer` + `resolve_prefix` (the
  demo/users key-builder invariant), per-user `users/{uid}/` isolation + in-code
  auth gate + CI contract test, and the public unauthenticated `demo/` namespace.
- **U4** `src/screencap/auth.py` + CLI `login`/`logout`/`whoami` + `get_id_token`.
- **U5** `src/screencap/{auth,upload,download,cli,engine,daemon}` — every client
  cloud call now carries a Firebase bearer token (via the shared
  `auth.authed_post`, 401-refresh-retry-once); the live-upload path is **fail-closed**
  (auth failure → `ChunkStatus.FAILED`, never sentinel/stub/delete — characterized
  in `tests/test_chunk_processor.py::TestAuthFailureFailClosed`); the daemon delivers
  a short-lived ID token to the engine **out-of-band** (0600 file + env path, never
  argv) with a re-mint timer; `screencap upload` refuses with a sign-in prompt when
  not signed in and touches nothing on disk; the retired `--remote`/`--sessions`
  session surfaces and the public `screencap.sh` viewer URLs are gone.

## Decisions already resolved (do not re-litigate)

- Single `--allow-unauthenticated` function + CI contract test (NOT split deployments).
- Demo gallery = real friend-trial recordings promoted **after a content/title review
  gate** (U8).
- Owner id = the verified Firebase `uid` as `users/{uid}/…` (never the email).
- Engine token transport = a 0600 file whose **path** travels via env
  (`SCREENCAP_ENGINE_TOKEN_FILE`); never `_worker_args`/argv (base64'd into argv and
  `ps`-visible). The long-lived refresh token never enters the all-day daemon.

## 🚨 CRITICAL — do not deploy the function yet

The merged function code requires a Firebase bearer token on
`upload`/`list`/`sign-download`. The U5 client now sends one, but **no shipped
release of the client carries it yet, and the OAuth client id + Web API key in
`src/screencap/auth.py` are still `REPLACE_WITH_PROVISIONED_*` placeholders**
(provision per `docs/runbooks/cloud-auth-setup.md`). The pre-deploy gate (see the
runbook + the `main.py` deploy header): **hold until U7 (website cutover) ships and a
provisioned client release is out, OR deploy under a new function name, OR add a
`SCREENCAP_AUTH_ENABLED` flag.** Deploying over the live `get-upload-urls` today 401s
every current client.

## Remaining work (dependency order)

1. **U6** — macOS Swift sign-in surface (`macos/`): shell out to `screencap login`
   (async, cancellable) + `whoami --json`; gate the Upload affordance on auth state;
   needs an Xcode build to verify. Depends on U4/U5 (both done).
2. **U7** — repoint the **`screencap-website`** repo (sibling path) at the `demo-*`
   actions; remove the `sessions/` routes; add an empty-gallery placeholder (U8
   promotion may legitimately yield zero recordings).
3. **U8 / U9** — migration scripts (`migrate_flat_to_staging.py`,
   `promote_staging_to_demo.py`, `decommission_flat_namespace.py`) +
   `docs/runbooks/cloud-migration-runbook.md`. Live runs are operator steps; U8's
   first pre-check removes any `allUsers`/`allAuthenticatedUsers` bucket-IAM binding.
4. **U10** — legacy `zkairdrop` decommission runbook (revoke public access on the
   frozen backup buckets; it is a second public door to the same data — R7/R9).

### U5 carry-forwards (not blocking U6+, do before the function deploys)

- **mitmdump EFFECT test** for `REQUIRED_AUTH_IGNORE_HOSTS` — prove `--network`
  self-recording cannot capture the bearer/refresh-token exchange end-to-end (the
  blocklist logic + `missing_required_auth_hosts` already have unit tests). See
  `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`.
- **Upload-checksum re-test** on the cloud-function side after the `firebase-admin`-
  forced `google-cloud-storage` 3.x bump (crc32c defaults changed).
- Minor: `config.get_sessions_dir` is now unused (vestigial, deferred cleanup); the
  daemon could prune a stale `~/.screencap/run/engine-token-*.jwt` on startup after a
  crash.

## Misc

- Linear SCR-113 / SCR-114 (`login --json` / `whoami --json` envelope) shipped in
  #210 — close them.
- Tests: cloud function → `cd scripts/cloud-function && python -m pytest` (needs
  `firebase-admin`); client → `pip install -e ".[dev]"` then `pytest`, or in a
  worktree `PYTHONPATH=src python -m pytest`. Full suite is green (2770 passed).
- If you work in a git worktree, Write/Edit paths **must** include the
  `.claude/worktrees/<name>/` segment and don't `cd` to the bare main-repo path (both
  silently land work in the wrong checkout).
- The 11 resolved review todos in this directory record each #210 finding's fix
  commit + test.

Start by reading the plan's Execution Status, then tackle U6 (macOS sign-in) and U7
(website demo repoint) — they unblock the migration units and the eventual deploy.
