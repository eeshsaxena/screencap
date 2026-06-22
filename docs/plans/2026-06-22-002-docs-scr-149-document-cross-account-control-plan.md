---
title: "docs: Document the SCR-116 cross-account control in SECURITY.md"
type: docs
status: completed
date: 2026-06-22
---

# docs: Document the SCR-116 cross-account control in SECURITY.md

## Summary

Add one short `###` subsection to `SECURITY.md` documenting the SCR-116 cross-account recording control shipped in PR #234. SECURITY.md is the threat-model source of truth but currently says nothing about the account-ownership gate that keeps a recording from fragmenting into a second user's cloud namespace after a mid-lifecycle account switch. This is a documentation-only change: no behavior changes, and the behavior being described is already pinned by existing tests.

---

## Problem Frame

PR #234 (SCR-116) added a client-side belt-and-suspenders control: a cloud recording is pinned at start to the Firebase `uid` that owned it (`.cloud_owner_uid`), and the terminal stage refuses cloud convergence if a *different* account is signed in at finalize time. The ce-security reviewer flagged that `SECURITY.md` — the documented threat model and the place a reader looks to understand the cloud trust boundary — never mentions this control, its fail-closed/fail-open design, or its one accepted residual. The gap is a documentation gap, not a code gap.

---

## Requirements

- R1. Document **"server is the namespace authority"**: `auth.id_token_uid()` decodes the Firebase `uid` UNVERIFIED and is only ever used to **refuse** a cross-account action (fail closed), never to authorize one or to pick a `users/{uid}` prefix; the signing Cloud Function stays the verifier of record.
- R2. Document `.cloud_owner_uid` as **local-only ownership metadata** — same sensitivity class as `.recording_intent`, excluded from upload by the dotfile filter in `upload.list_recording_files`, never uploaded.
- R3. Document **both sides of the gate**: (a) the **enforcement outcome** — on a *determined, different* signed-in account the gate refuses all cloud ops (no produce, no upload, no sentinel, no eviction) and keeps the recording intact locally; for a `both`-destination recording this means it does not reach its cloud half until the user signs back into the owning account; and (b) the **deliberate fail-open degradation** — unknown ownership (legacy/local recordings) or an undeterminable current account degrade to prior behavior, never a false refusal. Name the accepted residual — the transient/undeterminable-uid window in `terminal_stage._route_cloud` is the one path where the "never fragment" invariant relaxes (the PR chooses "never false-refuse" over "never fragment") — and note that a test in `tests/test_terminal_stage.py` pins it.
- R4. Every claim in the new prose is accurate against the cited code as of this commit (symbol names, file locations, and fail-open/fail-closed direction all verified, not paraphrased loosely).
- R5. The change is confined to `SECURITY.md` and matches the file's existing subsection house style; it introduces no new control and re-documents no behavior the file already covers.

---

## Scope Boundaries

- Not changing any SCR-116 behavior, symbol, or test — this plan only documents what already shipped.
- Not re-documenting the per-user storage isolation that the existing "Cloud auth trust boundary" section already covers (the new subsection references it, does not duplicate it).
- Not adding a SECURITY.md content-assertion test — the documented behavior is already covered by `tests/test_terminal_stage.py::TestSCR116AccountOwnershipGate`; a prose-pinning test would be over-engineering for a doc edit (see Key Technical Decisions).
- Not editing `CLAUDE.md` — its Security section already points at `SECURITY.md` generically and needs no per-subsection update.

---

## Context & Research

### Relevant Code and Patterns

The new prose must trace to these (all verified present at plan time):

- **The gate** — `_route_cloud` in `src/screencap/terminal_stage.py` (the SCR-116 block, step "0a"): reads `read_owner_uid(recording_dir)`; only when a pin exists *and* `current_uid` is determined *and* differs does it set `result.upload_warning` and return early (no produce, no upload, no sentinel, no eviction). An exception from `auth.get_id_token()` is swallowed to `current_uid = None` (no refusal).
- **The unverified uid helper** — `id_token_uid()` in `src/screencap/auth.py`. Its docstring already states the exact framing for R1: "The token is NOT verified here — the server remains the verifier of record — so this is only ever used to *refuse* a cross-account action (fail closed, SCR-116), never to authorize one." Returns `None` for empty/malformed/uid-less tokens.
- **The local-only pin** — `OWNER_UID_FILE = ".cloud_owner_uid"`, `write_owner_uid()`, `read_owner_uid()` in `src/screencap/catalog.py`. The module comment already documents the dotfile-filter rationale (R2). `.recording_intent` (`INTENT_FILE`) lives in the same module — the "same sensitivity class" anchor.
- **The dotfile upload filter** — `list_recording_files()` in `src/screencap/upload.py` skips any `p.name.startswith(".")`, which is what keeps `.cloud_owner_uid` (and `.recording_intent`) off every uploaded set.
- **Where the pin is written** — `_write_engine_token_file` / the token-staging path in `src/screencap/daemon/supervisor.py` calls `write_owner_uid(capture_dir, uid)` once at start (the daemon is the only component that reads the Keychain / mints the token). The re-mint guard in the same file refuses to restage on a determined account switch (the complementary in-memory guard the terminal-stage gate backstops).
- **The pinning tests** — `tests/test_terminal_stage.py::TestSCR116AccountOwnershipGate` (4 tests): `test_account_mismatch_refuses_cloud_convergence`, `test_matching_account_proceeds_to_upload`, `test_no_owner_pin_legacy_recording_not_refused`, and `test_undeterminable_current_uid_does_not_refuse` (the last is the accepted-residual pin for R3).

### Existing SECURITY.md structure (placement context)

`SECURITY.md` H2 sections in order: Trust boundary → (### Defense in depth around the boundary) → **Cloud auth trust boundary** → Cloud recording privacy → On-screen content index (SCR-118) → Threats in scope → Threats out of scope → Alternatives considered → Reporting. The "Cloud auth trust boundary" `##` already documents the server-side authority (the signing function verifies the token and scopes to `users/{uid}/…` via `resolve_prefix`). The "### Defense in depth around the boundary" subsection under the daemon "Trust boundary" `##` is the precedent for adding a `###` subsection under a `##` boundary section. Each existing subsystem section documents its own trade-offs/accepted residuals **inline** (e.g., the masked-video section's "The trade-off is explicit and accepted").

### Institutional Learnings

- None from `docs/solutions/` apply directly. The governing convention is `SECURITY.md` itself: honest framing (state what is *not* guaranteed), per-control trade-off paragraphs, and `code-symbol`/`path` references in prose.

---

## Key Technical Decisions

- **Placement: a new `### Cross-account recording isolation (SCR-116)` subsection under the existing `## Cloud auth trust boundary`.** Rationale: the control directly extends that section's "signing Cloud Function is the authorization boundary" claim — it is the client-side complement that *refuses* but never *authorizes*. Grouping it there keeps the server-authority and client-refusal halves adjacent, and matches the "### Defense in depth" precedent of a `###` under a `##` boundary section. Alternative (a standalone top-level `##`) is rejected — it would orphan the control from the server authority it depends on.
- **Document the accepted residual inline, with a recommended one-line cross-reference from "Threats out of scope."** Rationale: every subsystem section states its own trade-off inline (masked-video, narrowed-R7 index), so the full residual reasoning lives in the new subsection (house style). But "Threats out of scope" is the section a reader scans to enumerate *accepted gaps* (it already lists side-channel inference, same-user malware, etc.), so a one-line pointer there to the inline residual keeps it discoverable rather than buried under a boundary subsection. Recommend adding both; the inline text is authoritative.
- **No SECURITY.md content-assertion test.** Rationale: the behavior is already pinned by `TestSCR116AccountOwnershipGate`; the doc's job is to describe verified behavior, and a string-matching test on prose is brittle and adds no real guarantee. Verification is human/agent cross-check of each claim against the cited code.
- **Use `type: docs`.** Rationale: this is a documentation change; the repo already uses non-`feat/fix/refactor` plan types (e.g. `test`), and the eventual commit is `docs(scr-149): …`.

---

## Open Questions

### Resolved During Planning

- *Where does the subsection live?* → `###` under `## Cloud auth trust boundary` (see Key Technical Decisions).
- *Does the accepted residual go in "Threats out of scope"?* → Inline in the new subsection is authoritative (house style), **plus** a recommended one-line cross-reference from "Threats out of scope" so the accepted gap is discoverable from the section readers use to enumerate them.
- *Is a doc-assertion test needed?* → No; existing `TestSCR116AccountOwnershipGate` already pins the behavior.

### Deferred to Implementation

- Exact final wording of each sentence — the unit specifies the required claims; prose polishing happens at write time against the live code.

---

## Implementation Units

### U1. Add the SCR-116 cross-account isolation subsection to SECURITY.md

**Goal:** Document the SCR-116 account-ownership control as a new `###` subsection under "Cloud auth trust boundary," covering all three issue bullets and the accepted residual, accurately traced to the shipped code.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** None.

**Files:**
- Modify: `SECURITY.md` — (1) insert a `### Cross-account recording isolation (SCR-116)` subsection at the end of the `## Cloud auth trust boundary` section, before `## Cloud recording privacy …`; (2) add a recommended one-line cross-reference to the accepted residual under `## Threats out of scope`.

**Approach:**
- Insert after the last bullet of "Cloud auth trust boundary" (the "Daemon cloud-credential containment" bullet) and before the next `##` heading.
- Write a short intro sentence framing the control as the client-side complement to the server authority already described above, then the required claims below. Keep to the file's voice: terse, honest about limits, `code-symbol`/`path` references inline.
- Reference (do not duplicate) the existing per-user isolation: the Cloud Function + `resolve_prefix` remain the authorization boundary; this subsection covers the *client refusal* layer only.
- Add a one-line bullet under "Threats out of scope" pointing at the inline residual (e.g., "Cross-account fragmentation in the transient/undeterminable-uid window — accepted; see Cross-account recording isolation"), matching how that section already catalogs accepted gaps.

**Required content (the "test scenarios" of a doc — each claim must appear and be accurate):**
- *Server is the namespace authority (R1):* `auth.id_token_uid()` decodes the Firebase `uid` **UNVERIFIED**; it is used **only to refuse** a cross-account convergence (fail closed), never to authorize one or to choose a `users/{uid}` prefix. The signing Cloud Function stays the verifier of record. The pin is written by the daemon at recording start **only when the staged token carries a resolvable uid** (`catalog.write_owner_uid`; the daemon is the only component that reads the Keychain / mints the token) — a malformed token or a write error leaves the recording unpinned, degrading to prior behavior rather than failing the start (do not claim the pin always arms at start).
- *Local-only ownership metadata (R2):* `.cloud_owner_uid` is local-only, **same sensitivity class as `.recording_intent`**, excluded from every uploaded set by the dotfile filter in `upload.list_recording_files` (skips `.`-prefixed names) — never uploaded.
- *Enforcement outcome (R3a):* the gate in `terminal_stage._route_cloud` refuses **only** on a *determined, different* current uid. On that refusal it performs **no cloud op** — no produce, no upload, no sentinel, no eviction — and keeps the recording **intact locally**; for a `both`-destination recording, the recording therefore does not reach its cloud half until the user signs back into the owning account. (This refusal is the gate's primary purpose and the more common user-visible outcome — document it, not only the relaxation below.)
- *Deliberate fail-open degradation (R3b):* unknown ownership (legacy/local recording, no pin) or an undeterminable current account (not signed in / transient auth) **degrades to prior behavior — never a false refusal.** State the accepted residual explicitly: the transient/undeterminable-uid window is the one path where the "never fragment" invariant relaxes, because the PR deliberately chooses "never false-refuse" over "never fragment." Note that `tests/test_terminal_stage.py::TestSCR116AccountOwnershipGate` (specifically `test_undeterminable_current_uid_does_not_refuse`) pins this.
- Optionally name the complementary daemon re-mint guard in `daemon/supervisor.py` (refuses to restage a token on a determined account switch, with the **same determined-different-uid-only tolerance** — a `None`/undeterminable re-mint uid is not treated as a switch) as the in-memory guard the terminal-stage gate backstops at the resume / `screencap upload` entry points. The two layers read **different uid sources** (engine token in memory vs. the Keychain at resume/`screencap upload`), which is why the terminal-stage backstop exists.

**Execution note:** Documentation-only. Verify each claim against the cited code at write time rather than paraphrasing from this plan; if any symbol or behavior has drifted since this plan was written, document the code as it actually is.

**Patterns to follow:**
- `SECURITY.md` existing subsections — bullet-per-property style of the "Cloud auth trust boundary" section; inline trade-off paragraph like the masked-video and narrowed-R7 sections.
- The "### Defense in depth around the boundary" subsection as the structural precedent for a `###` under a `##` boundary section.

**Test scenarios:**
- Test expectation: none — documentation-only change with no behavioral effect. The documented behavior is already covered by `tests/test_terminal_stage.py::TestSCR116AccountOwnershipGate` (4 tests); no new or changed test is in scope.

**Verification:**
- All three issue bullets (R1–R3) are present in the new subsection and the accepted residual is named.
- Each prose claim cross-checks against the live code: `auth.id_token_uid` (unverified, refuse-only), `catalog.OWNER_UID_FILE`/`write_owner_uid`/`read_owner_uid`, the `name.startswith(".")` skip in `upload.list_recording_files`, the determined-different-uid-only condition in `terminal_stage._route_cloud`, and the four `TestSCR116AccountOwnershipGate` tests.
- The subsection is placed under `## Cloud auth trust boundary` and reads consistently with surrounding prose; no other section is altered; `markdownlint`/render is clean (no broken headings or links).

---

## Sources & References

- **Linear issue:** [SCR-149 — Document the SCR-116 cross-account control in SECURITY.md](https://linear.app/zk-email/issue/SCR-149/document-the-scr-116-cross-account-control-in-securitymd-scr-116)
- **Origin PR:** [proteus-computer-use/screencap#234 — fix(cloud): pin cloud recordings to their owner account (SCR-116)](https://github.com/proteus-computer-use/screencap/pull/234)
- **Related issue:** [SCR-116 — Cloud recording fragments across accounts if the user switches accounts mid-recording](https://linear.app/zk-email/issue/SCR-116/cloud-recording-fragments-across-accounts-if-the-user-switches)
- Code: `src/screencap/terminal_stage.py` (`_route_cloud`), `src/screencap/auth.py` (`id_token_uid`), `src/screencap/catalog.py` (`OWNER_UID_FILE`, `write_owner_uid`, `read_owner_uid`), `src/screencap/upload.py` (`list_recording_files`), `src/screencap/daemon/supervisor.py` (pin write + re-mint guard)
- Tests: `tests/test_terminal_stage.py::TestSCR116AccountOwnershipGate`
- Target doc: `SECURITY.md` (`## Cloud auth trust boundary`)
- Related plan: `docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md`
