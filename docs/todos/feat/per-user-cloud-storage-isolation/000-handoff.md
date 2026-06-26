---
title: "Handoff: continuing per-user cloud storage isolation after U6"
type: handoff
status: open
created: 2026-06-03
updated: 2026-06-04
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_runbooks:
  - docs/runbooks/cloud-auth-setup.md
---

# Handoff: per-user cloud storage isolation

The **backend security boundary + the full client auth path + the macOS sign-in
surface** are now done. Units U1–U6 have shipped:

- **U1–U4 + the U5 self-capture sub-scope** merged into `main` via PR #210.
- **The U5 remainder** merged into `main` via PR #212.
- **U6** (macOS sign-in surface, `macos/`) shipped on branch
  `feat/per-user-cloud-storage-isolation-u6`. Read the plan's
  **`## Execution Status`** first
  (`docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md`) — it
  lists every unit's state and the commit SHAs.

The remaining work is **U7 onward** (website + migration + legacy decommission).
Nothing below blocks on more client/app work.

## Already shipped (U1–U6)

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
- **U6** `macos/ScreenCap/` — a `CloudAuthController` shells out to `screencap
  login`/`logout`/`whoami --json` (all token handling stays in Python). The menu
  bar shows the signed-in account + Sign In / Sign Out (Sign Out disabled while an
  upload is in flight); the review-window Upload affordance gates on auth state and
  presents an async, cancellable "Sign in to upload" sheet when signed out instead
  of an opaque CLI refusal. `whoami` decode is drift-resilient; local recording is
  never gated (R3). Tested in `macos/ScreenCapTests/CloudAuthControllerTests.swift`.

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

1. **U7** — repoint the **`screencap-website`** repo (sibling path) at the `demo-*`
   actions; remove the `sessions/` routes; add an empty-gallery placeholder (U8
   promotion may legitimately yield zero recordings). Separate repo → its own branch
   + PR.
2. **U8 / U9** — migration scripts (`migrate_flat_to_staging.py`,
   `promote_staging_to_demo.py`, `decommission_flat_namespace.py`) +
   `docs/runbooks/cloud-migration-runbook.md`. Live runs are operator steps; U8's
   first pre-check removes any `allUsers`/`allAuthenticatedUsers` bucket-IAM binding.
3. **U10** — legacy `zkairdrop` decommission runbook (revoke public access on the
   frozen backup buckets; it is a second public door to the same data — R7/R9).

### U6 known residuals (rare multi-window / lifecycle, deferred from the U6 PR)

These are non-blocking edge cases in the macOS sign-in flow, surfaced by code
review and deliberately deferred (the security boundary is unaffected — the upload
path fail-closes in Python regardless):

- **Concurrent sign-in from two open review windows.** `signInFlow` is a single
  app-wide `CloudAuthController`; a second review window's sheet shows the shared
  in-progress spinner but its `onSignedIn` does not fire on completion (only the
  first attempt's completion is retained), so it won't auto-dismiss/upload.
- **Window closed mid-sign-in.** Closing a review window while its sheet's login is
  in flight does not cancel it; the login subprocess self-heals at the U4 180s
  loopback timeout and the flow lands on `.failed`.
- **Menu-only stale status.** `auth.refresh()` runs on the main window's `.task`; if
  the user operates only via the menu bar after closing the main window, the account
  line can go stale until the main window reopens (no menu-open re-check).

### U5 carry-forwards — DONE (SCR-140)

All three discharged (still before any function deploy, per the pre-deploy hold above):

- **mitmdump EFFECT test** for `REQUIRED_AUTH_IGNORE_HOSTS` — DONE in
  `tests/network/test_required_auth_effect.py`. Spawns real mitmdump with the **production**
  `ignore_hosts` (built via `build_ignore_hosts_regex`, so it tracks config drift) and proves
  by TLS cert issuer that each auth host is CONNECT-tunneled — never TLS-intercepted — so
  `--network` self-recording cannot capture the bearer/refresh/ID-token exchange; a non-listed
  control host is asserted intercepted (and first asserted absent from the patterns) to rule
  out a vacuous all-tunnel pass. Gated as a **local pre-deploy proof**, not a CI gate: opt-in
  `SCREENCAP_NETWORK_EFFECT_TEST=1` + `mitmdump` on PATH + skip-on-unreachable (a normal
  `pytest` run skips it with no network). The *continuous* guard against a dropped host stays
  the always-on `missing_required_auth_hosts` unit test + the `proxy_runner` fail-closed gate.
  **Pre-deploy EFFECT run 2026-06-25:** control `example.com` intercepted (leaf issuer CN
  `mitmproxy` = the proxy CA); all 4 auth hosts tunneled (leaf issuer CN `WR2` = Google Trust
  Services, NOT the proxy CA). See
  `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`.
- **Upload-checksum re-test** — DONE in `scripts/cloud-function/test_signing_contract.py`
  (+ client companion `tests/test_upload.py::test_put_sends_content_type_and_no_checksum_header`).
  The function only **signs** v4 PUT URLs and the client raw-PUTs `Content-Type` only; the gcs
  3.x `crc32c="auto"` default applies to `upload_from_*`/`download_to_*` transfer methods, which
  neither path uses. The test exercises the **real** 3.x `generate_signed_url` and asserts the
  signed PUT URL is checksum-free (no `x-goog-hash`). Migration per-object crc32c verify is a
  separate path (SCR-145).
- **Minor cleanups — already done in `b3fc9853`:** `config.get_sessions_dir` removed (zero refs
  remain); stale `engine-token-*.jwt` startup prune implemented
  (`Supervisor._prune_stale_engine_token_files`) + tested
  (`tests/daemon/test_supervisor.py::test_reconcile_prunes_stale_engine_token_file`).

Plan: `docs/plans/2026-06-25-001-test-scr-140-u5-carryforward-effect-tests-plan.md`.

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
- **U6 verify note:** the macOS app/CLI sign-in path is wired, but `screencap login`
  cannot complete end-to-end until the OAuth client id + Web API key placeholders in
  `src/screencap/auth.py` are provisioned (`docs/runbooks/cloud-auth-setup.md`). The
  U6 Swift side is unit-tested against a faked CLI seam; a live browser round-trip
  needs provisioning first.

Start by reading the plan's Execution Status, then tackle U7 (website demo repoint,
in the sibling `screencap-website` repo) — it unblocks the migration units and the
eventual deploy. U8/U9 (migration) and U10 (legacy decommission) follow.
