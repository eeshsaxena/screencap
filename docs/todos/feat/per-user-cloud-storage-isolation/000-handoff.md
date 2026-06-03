---
title: "Handoff: continuing per-user cloud storage isolation after PR #210"
type: handoff
status: open
created: 2026-06-03
related_plans:
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
related_runbooks:
  - docs/runbooks/cloud-auth-setup.md
---

# Handoff: per-user cloud storage isolation

PR #210 (`feat/per-user-cloud-storage-isolation`) has merged into `main`. It shipped
the **backend security boundary + client auth foundation** (plan units U1–U4 + the U5
self-capture sub-scope) plus all 20 code-review findings fixed. Read the plan's
**`## Execution Status`** section first
(`docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md`) — it lists
what's done and what's left.

## Already in `main`

- **U1** `scripts/cloud-function/{auth,paths}.py` — `verify_bearer` + `resolve_prefix`
  (the single key builder enforcing the demo/users invariant).
- **U2** `scripts/cloud-function/main.py` — per-user `users/{uid}/` isolation + in-code
  auth gate + a CI contract test; `get-index` removed.
- **U3** — public `demo/` namespace dispatched before the auth gate.
- **U4** `src/screencap/auth.py` + CLI `login`/`logout`/`whoami` + `get_id_token`.
- **U5 (partial)** `src/screencap/network/` — auth hosts blocked from `--network`
  (override-proof) + proxy fail-closed gate.

## Decisions already resolved (do not re-litigate)

- Single `--allow-unauthenticated` function + CI contract test (NOT split deployments).
- Demo gallery = real friend-trial recordings promoted **after a content/title review
  gate** (U8).

## 🚨 CRITICAL — do not deploy the function yet

The merged function code requires a Firebase bearer token on
`upload`/`list`/`sign-download`, but no shipped client sends one — deploying over the
live `get-upload-urls` 401s every current client. The pre-deploy gate is in
`docs/runbooks/cloud-auth-setup.md` (and the `main.py` deploy header): hold until U5 +
U7 ship, OR deploy under a new function name, OR add a `SCREENCAP_AUTH_ENABLED` flag.

## Remaining work (dependency order)

1. **U5 remainder** (next, highest value): thread the bearer token through `upload.py` /
   `download.py` (`request_signed_urls`, `list_remote_recordings`) with refresh-retry-once
   on 401; the **fail-closed `chunk_processor`** live-upload invariant (auth fail / 503 /
   `KeyringError` → `core_ok=False`, never sentinel/stub/delete — **characterization-test-
   first** per `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md`);
   the **daemon out-of-band token seam** (daemon reads the token in its ACL context and
   passes a short-lived ID token to the engine via **env var, NOT `_worker_args`/argv**
   which is base64'd into argv and `ps`-visible; re-mint on a timer). Also remove
   `fetch_session_index` / `list_remote_sessions` + the `--remote`/`--sessions` CLI
   surfaces, and the public `screencap.sh` viewer URLs (`cli/__init__.py` ~line 2971).
   - **Test-churn warning:** `test_upload.py` / `test_download.py` / `test_chunk_processor.py`
     patch `screencap.upload.requests.post` directly — attach the auth header *in-module*
     (not via a separate posting function) so that mocking keeps working; tests calling
     `request_signed_urls` will need to mock `auth.get_id_token`.
   - Re-test the upload-checksum path (`firebase-admin` forced `google-cloud-storage` to
     3.x, which changes crc32c defaults).
   - Carry-forward (from the review): `AuthUnavailable` (503) must map to
     `_upload_disabled_reason`, never `success=True`, in `chunk_processor`. Add an
     end-to-end mitmdump EFFECT test for `REQUIRED_AUTH_IGNORE_HOSTS` (see
     `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`).
2. **U6** — macOS Swift sign-in surface (`macos/`; needs an Xcode build to verify).
3. **U7** — repoint the **`screencap-website`** repo (sibling path) at the `demo-*`
   actions; remove the `sessions/` routes; add an empty-gallery placeholder.
4. **U8 / U9** — migration scripts (`migrate_flat_to_staging.py`,
   `promote_staging_to_demo.py`, `decommission_flat_namespace.py`) +
   `docs/runbooks/cloud-migration-runbook.md`. Live runs are operator steps.
5. **U10** — legacy `zkairdrop` decommission runbook.

## Misc

- Linear SCR-113 / SCR-114 (`login --json` / `whoami --json` envelope) are **already
  implemented** in #210 — close them.
- Tests: cloud function → `cd scripts/cloud-function && python -m pytest` (needs
  `firebase-admin` installed); client → `pip install -e ".[dev]"` then `pytest`, or in a
  worktree `PYTHONPATH=src python -m pytest`.
- If you work in a git worktree, Write/Edit paths **must** include the
  `.claude/worktrees/<name>/` segment and don't `cd` to the bare main-repo path (both
  silently land work in the wrong checkout).
- The 11 resolved review todos in this directory record each finding's fix commit + test.

Start by reading the plan's Execution Status + the U5 section, then tackle U5 remainder.
