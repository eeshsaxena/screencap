---
title: "Handoff: Linear board sync — close shipped work + create the genuinely-outstanding cloud-isolation tail"
type: handoff
status: done
created: 2026-06-15
executed: 2026-06-15  # see "## Execution result" at the end
author_session_note: >
  Written by a session that could NOT reach the Linear MCP connector (sustained
  outage on 2026-06-15). All actions below are grounded in git/PR history and the
  repo's own docs/todos + docs/plans, NOT in a live read of the Linear board.
  The executing session MUST read the live board first and reconcile before
  writing — treat every "close"/"create" below as a proposal to verify, not a
  blind command.
related:
  - docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md   # source of truth for U7–U10
  - docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md
---

# Linear board sync — handoff for a Linear-capable session

## Purpose

Another session (with a working Linear MCP) should execute the board changes
below for the **Screencap** team. The originating session built this plan but
hit a full Linear connector outage and could not read or write the board.

## How to use this file

1. **Read the live board first.** List In Progress / In Review / Todo / Backlog
   and reconcile against the proposals here. Tickets may already be in the right
   state.
2. **Verify the reference IDs** (below) with `list_issue_statuses` /
   `list_issue_labels` before using — they came from a 28-day-old note and may
   have drifted.
3. Execute §1 (create), §2 (close), §3 (reconcile). **Do not** act on §4.
4. Show the user a read-back before finalizing.

## Reference IDs (Screencap team — VERIFY before use)

- **Team:** `Screencap` — `f3bbac41-0ec3-4f2e-ae0b-96fed6ff624f`
- **Workspace URL prefix:** `https://linear.app/zk-email/issue/SCR-…`
- **Statuses:** Backlog `c825fa67-c31e-421c-a5f9-ab2aef7ef5f5` · Todo
  `cecb19a8-79e3-480c-b4c0-3e19ec8432d5` · Planning
  `86961642-6da6-4d94-9b4c-e87ec902f613` · In Progress
  `a39dab47-ea30-4a5e-b126-0f4c326de2fc` · In Review
  `6c60ed6b-f595-411d-92e8-757be8c3f947` · Done
  `59aa79f8-2cd6-4512-b803-99cc96d1839d` · Released
  `9fb0cf19-fcb7-4a24-afaa-c901a4965bb1`
- **Category labels (use Capitalized):** Bug
  `fc126e8e-a621-4acb-b326-05f1c047dbf4` · Improvement
  `3ed3c3d5-6ede-4ecd-8140-be2fff4a75f3` · Feature
  `0948a8cf-bb9b-4df4-a190-96b70ea3e905`. (Do NOT use the lowercase duplicates.)
- **Priority field:** 1=Urgent, 2=High, 3=Medium, 4=Low, 0=None.

---

## §1 — CREATE (genuinely outstanding work)

> Source: `docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md`
> (the U7→U10 remainder) + its U5 carry-forwards. **Check for an existing ticket
> before creating each** — this track is active and some may already exist.

### Recommended focus (create these first)

**A. [Urgent] Provision cloud-auth credentials + gate the function deploy**
- Label: Feature (ops/setup). Priority: Urgent (blocks the entire cloud-upload path).
- Body: `src/screencap/auth.py` still has `REPLACE_WITH_PROVISIONED_*` for the
  OAuth client id + Web API key, so `screencap login` cannot complete a live
  browser round-trip. The deployed signing function already requires a Firebase
  bearer token on `upload`/`list`/`sign-download`; deploying over the live
  `get-upload-urls` before clients carry tokens 401s every current client. Pre-deploy
  gate: hold until U7 ships + a provisioned client release is out, OR deploy under a
  new function name, OR add a `SCREENCAP_AUTH_ENABLED` flag. Provision per
  `docs/runbooks/cloud-auth-setup.md`.

**B. [High] U7 — repoint the `screencap-website` repo at the `demo-*` actions**
- Label: Feature. Priority: High (unblocks migration + the deploy).
- Body: In the sibling `screencap-website` repo (own branch + PR): point the site at
  the `demo-*` function actions, remove the `sessions/` routes, add an empty-gallery
  placeholder (U8 promotion may yield zero recordings). This is the keystone that
  unblocks U8/U9/U10.

### Lower priority (create if the team wants them tracked, else leave in docs/todos)

**C. [Medium] U8/U9 — cloud migration scripts + runbook**
- Label: Feature. Body: `migrate_flat_to_staging.py`, `promote_staging_to_demo.py`,
  `decommission_flat_namespace.py` + `docs/runbooks/cloud-migration-runbook.md`. Live
  runs are operator steps; U8's first pre-check removes any
  `allUsers`/`allAuthenticatedUsers` bucket-IAM binding.

**D. [Medium] U10 — decommission the legacy `zkairdrop` namespace**
- Label: Improvement (security). Body: Revoke public access on the frozen backup
  buckets — a second public door to the same data (R7/R9). Needs a runbook.

**E. [Medium] U5 carry-forwards before the function deploys**
- Label: Improvement. Body: (1) mitmdump EFFECT test for `REQUIRED_AUTH_IGNORE_HOSTS`
  proving `--network` self-recording can't capture the bearer/refresh-token exchange
  end-to-end; (2) upload-checksum re-test after the `google-cloud-storage` 3.x bump
  (crc32c defaults changed). Minor cleanups: vestigial `config.get_sessions_dir`;
  daemon prune of a stale `~/.screencap/run/engine-token-*.jwt` on startup.

**F. [Low] U6 macOS sign-in residual edge cases**
- Label: Bug (non-blocking). Body: three deferred multi-window/lifecycle edge cases —
  concurrent sign-in from two review windows (second window's `onSignedIn` never
  fires), window closed mid-sign-in (login not cancelled, self-heals at 180s
  timeout), menu-only stale account status (no menu-open re-check). Security boundary
  unaffected (upload fail-closes in Python).

---

## §2 — CLOSE / move status (shipped, verify each is not already closed)

Merged this week (move → **Done**, or Released if that's the team's merged state):

| Ticket | PR | Title |
|---|---|---|
| SCR-110 | #228 | mask background windows even when OCR runs |
| SCR-118 | #229 | MCP agent-memory retrieval + content index |
| SCR-125 | #225 | unify the per-chunk upload pipeline (terminal-stage cutover) |
| SCR-126 | #227 | masked-video-upload fail-open hardening |
| SCR-129 | #226 | terminal stage uploads source chunk media |
| SCR-134 | #230 | close inline-write vs disable-purge content-index race |

In-flight (move → **In Review**):

| Ticket | PR | Title |
|---|---|---|
| SCR-121 | #231 (open) | reject stale/version-mismatched daemon |

Per the cloud-isolation handoff, also close if still open:

| Ticket | PR | Note |
|---|---|---|
| SCR-113 | #210 | `login --json` envelope — shipped |
| SCR-114 | #210 | `whoami --json` envelope — shipped |

## §3 — RECONCILE (verify against the live board; don't close blindly)

- **Prior-wave tickets** (merged Jun 1–5, may already be closed): SCR-28 (#217),
  SCR-71 (#200), SCR-98 (#203), SCR-99 (#208/#209), SCR-101 (#201), SCR-102 (#202),
  SCR-103 (#205), SCR-104 (#204), SCR-108 (#206). Also SCR-122/SCR-123/SCR-124
  (folded into #224's pipeline work — confirm scope before closing).
- **Stale plan epics** — these plans are marked `active` in `docs/plans/` but shipped;
  close/parent-update if there's a matching Linear epic: GCP migration (#208), daemon
  TCC permission visibility (#223), unified recording pipeline (#224/#225).
- **Keep OPEN:** the **Per-User Cloud Storage Isolation** epic — U1–U6 shipped but
  U7–U10 (§1) remain. This is the one big track that is genuinely unfinished.

## §4 — DO NOT recreate (already resolved/completed in docs/todos)

These review follow-ups are **resolved** (each has a `## Resolution` + fix commit) or
**completed**. They are NOT outstanding work — do not open tickets for them:

- `feat/native-redaction-review-upload/001–007` — all resolved.
- `feat/per-user-cloud-storage-isolation/001–011` — all resolved (incl. the former
  P0 deploy-gate `001` and the P1 refresh-token `002`; the *forward* deploy gate
  lives in §1-A above, which is the still-open provisioning step, not the closed
  review finding).
- `rutefig/scr-58-split-recordercontroller-…/001–003` — completed.

## §5 — Also for the user's standup

While reading the board, pull **Friday 2026-06-12** Linear activity (comments,
status changes, triage) for the Screencap team — the originating session found no
git/PR activity that day and the user wants to know if Friday was Linear/planning
work.

---

## Execution result (2026-06-15)

Executed against the **live** board via the new `plugin:product-management:linear`
connector (the old `/sse` server was dead — transport deprecated). Reference IDs
re-verified; only the handoff's own Feature label ID (§Reference) was stale
(`0948a8cf-a190-…` → correct is `0948a8cf-bb9b-4df4-a190-96b70ea3e905`).

**The board was already largely maintained.** Every "merged this week" and
prior-wave ticket was already **Done** (SCR-110/118/125/126/129/134 and
SCR-28/71/98/99/101–104/108/123/124). SCR-121 was already Done (handoff expected
In Review; it shipped, and its #231 review follow-ups already exist as SCR-135/136).

**§2 — closed (verified shipped against code + tests, not just #210):**
- **SCR-113** → Done (`login --json` failure envelope: `cli/__init__.py:1733` +
  `test_cli_login_json_failure`). `needs-triage` dropped.
- **SCR-114** → Done (`whoami` ok:false + stale envelope: `cli/__init__.py:1770` +
  `test_cli_whoami_json_stale_state`/`_error_envelope`). `needs-triage` dropped.

**§1 — created:**
- **SCR-137** [Urgent · Feature] — A: provision cloud-auth creds + gate the deploy.
- **SCR-138** [High · Feature] — B: U7 website repoint. Blocks SCR-139, SCR-112, SCR-137.
- **SCR-139** [Medium · Feature] — C: U8/U9 migration scripts + runbook.
- **SCR-140** [Medium · Improvement] — E: U5 carry-forwards.
- **SCR-141** [Low · Bug] — F: U6 sign-in residual edge cases.
- **Item D was NOT created** — it already exists as **SCR-112** (U10 zkairdrop
  decommission, Todo). Its stale `due 2026-06-09` could not be cleared via the
  connector (no null-clear path); left a comment — **needs a manual one-click clear**
  in the Linear UI, re-set to +7d from the actual U9 ship date.

**§3 — reconcile:** no Linear epics exist (only `Windows`/`MacOS` projects), so the
"close stale plan epics / keep the isolation epic open" items had nothing to act on.

**§4 — skipped** (all resolved), as instructed.

**§5 — standup:** **no Linear activity on Friday 2026-06-12** (no issue touched that
day; no cycles configured; both projects' timestamps are from April). Matches the
no-git-activity finding — Friday was quiet on both fronts.
