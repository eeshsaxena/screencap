---
date: 2026-06-30
topic: foolproof-macos-permission-onboarding
---

# Foolproof macOS Permission Onboarding for the ScreenCap Helper

## Summary

The recording daemon will auto-register its Screen Recording and Accessibility rows at helper install — under the name **"ScreenCap"** — remove the decoy/orphan rows so exactly one correct row exists per pane, and block setup with a Retry if the row ever fails to appear. A non-technical user only ever flips one recognizable toggle and can never grant the wrong identity.

---

## Problem Frame

Recording runs in a background daemon (`com.screencap.daemon`) shipped as a hidden helper bundle *inside* `ScreenCap.app`. macOS TCC grants are per-identity, so the user must grant the **daemon**, not the visible app (`com.screencap.macos`).

Today the daemon's Privacy row is created **only on-demand**, when the user clicks "Grant" in the walkthrough. When that hasn't happened, the Settings pane has **no helper row**, so macOS forces a manual **"+" add** — and a normal user browses to the visible `/Applications/ScreenCap.app` and grants the *app*. The daemon stays ungranted and recording silently loses window/keystroke attribution.

Two things compound it: even when the row *is* registered, macOS labels it by the bundle **filename** ("ScreencapDaemon"), which doesn't match the onboarding copy ("ScreenCap Helper"); and the panes accumulate look-alike **decoy rows**. On a real machine the Accessibility pane showed three near-identical entries:

| Row macOS shows | Real identity | Counts for recording? |
|---|---|---|
| `screencap` (orphan) | pre-SCR-196 bare binary (no longer ships) | No |
| `ScreenCap` | the app (`com.screencap.macos`) | No |
| `ScreencapDaemon` | the daemon (`com.screencap.daemon`) | **Yes — and it was off** |

The net effect: a non-technical user cannot reliably grant the permission, and the failure is **silent** — recording appears to run but is degraded.

---

## Actors

- A1. **Non-technical user** — completes setup; wants to flip toggles, not navigate Finder or distinguish app-vs-helper.
- A2. **ScreenCap app** (`com.screencap.macos`) — the GUI shell; presents onboarding, triggers daemon registration, reads the daemon's reported grant state, opens Settings panes. Not the permission-holding identity.
- A3. **ScreenCap daemon** (`com.screencap.daemon`) — the capture process and TCC subject; the only actor that can register itself in System Settings and authoritatively report its own grant state.

---

## Key Flows

- F1. **First-run grant**
  - **Trigger:** the helper is installed / approved (first run or upgrade).
  - **Actors:** A1, A2, A3.
  - **Steps:** daemon self-registers its Screen Recording + Accessibility rows at install → decoy/orphan rows are removed → the walkthrough shows each permission with the helper icon and the exact row name "ScreenCap" → the user opens the pane and finds a single pre-populated "ScreenCap" row already there → the user flips it on → the app reads the daemon's updated grant state and advances.
  - **Outcome:** Screen Recording + Accessibility granted to the daemon; no "+" add; no wrong-identity grant.
  - **Covered by:** R1–R6, R8.
- F2. **Registration-failure fallback**
  - **Trigger:** after install-time registration, the expected row is still absent.
  - **Actors:** A1, A2, A3.
  - **Steps:** the app detects the row didn't appear → shows a "couldn't set this up" state with a Retry (re-runs registration) and a diagnostics hint → does not open a manual-add flow and does not silently proceed.
  - **Outcome:** the user is never sent to Finder; either Retry succeeds or the user sees a clear, honest blocked state.
  - **Covered by:** R7.

---

## Requirements

**Pre-populated rows**
- R1. The daemon registers its Screen Recording and Accessibility Privacy rows proactively at helper install / approval — before any onboarding permission step — so a row always exists for the user to toggle. This is *in addition to and earlier than* the existing on-demand registration.
- R2. The onboarding never requires the System Settings "+" / manual-add control. The user's only action per permission is flipping an already-present toggle.

**Recognizable identity**
- R3. The daemon's Privacy-pane row reads **"ScreenCap"** — the name a non-technical user recognizes — and the onboarding copy names that exact row.
- R4. The walkthrough visually identifies the row with the helper's **icon** and the exact row name, so the user can match it unambiguously.

**Single correct row (decoy removal)**
- R5. Exactly **one** "ScreenCap" row exists per relevant pane. The orphaned pre-SCR-196 identity row and any legacy app-identity rows in Screen Recording / Accessibility are removed (or never created), so there is no look-alike to enable by mistake.
- R6. The app (`com.screencap.macos`) never appears in the Screen Recording or Accessibility panes — only the daemon's "ScreenCap" row does.

**Failure handling**
- R7. If the expected row fails to appear after registration, setup **blocks** with a clear "couldn't set this up" state and a **Retry** that re-runs registration. It never silently proceeds and never falls back to a manual-add flow.

**Revoke**
- R8. The user can revoke a granted permission by toggling off the **same single** "ScreenCap" row — the off path is as unambiguous as the on path.

**Upgrade**
- R9. On upgrade from a build that already holds daemon grants, the change relabels/cleans rows **without forcing the user to re-grant** (existing grants are preserved).

---

## Acceptance Examples

- AE1. **Covers R1, R2, R3.** Given a fresh install where the user has taken no permission action, when the user opens the Accessibility pane from the walkthrough, then a single row named "ScreenCap" is already present and the user grants it by flipping its toggle — with no "+" add.
- AE2. **Covers R5, R6.** Given a machine upgraded from a pre-SCR-196 build (orphan `screencap` row present) plus prior app-identity rows, when setup completes, then only one "ScreenCap" row remains in each pane and no app/orphan look-alike rows remain.
- AE3. **Covers R7.** Given install-time registration that fails to produce the Accessibility row, when the walkthrough reaches that step, then it shows a "couldn't set this up" state with a Retry — and does not open a manual-add flow or silently continue.
- AE4. **Covers R8.** Given Accessibility granted to the daemon, when the user wants to revoke it, then turning off the single "ScreenCap" row in the Accessibility pane fully revokes it.

---

## Success Criteria

- A non-technical user — on a clean install **and** on an upgrade — grants Screen Recording + Accessibility to the daemon without ever using "+", without granting the app, and without choosing among look-alike rows.
- After setup, the daemon's reported grant state for Screen Recording + Accessibility is `granted`, and recording captures window/keystroke attribution.
- A downstream implementer can build without inventing product behavior: the registration trigger (install-time), the row name ("ScreenCap"), the decoy-removal rule (exactly one row), the failure behavior (block + Retry), and the revoke path are all specified here.

---

## Scope Boundaries

- **Input Monitoring** is excluded — already demoted to optional (PR #315); it can't be registered for the helper on macOS 26.x and is advisory (not capture-fatal).
- **Reversing the daemon-as-TCC-subject architecture** (making the app the permission identity) is out — reconsidered and rejected; the daemon stays the subject.
- The **system-dialog / avoid-per-row-toggling reframe** is out.
- Build/codesign mechanics of the helper rename, the exact registration code path, and exact error-message wording are **ce-plan's** job, not this doc.
- This work **absorbs SCR-199's** orphaned-row cleanup into R5 — SCR-199 should be folded into / closed by this effort rather than run separately.

---

## Key Decisions

- **Daemon stays the TCC subject; make its row foolproof rather than move the identity.** Consistent with SCR-196 and the Developer-ID distribution plan; the app cannot and should not become the capture identity.
- **Register at install / approval** (not on-demand-only, not every-boot). Guarantees the row exists before the user could look — the direct cause of the manual-add failure. Revisits SCR-196's on-demand-only choice by adding one proactive registration.
- **Name the row "ScreenCap"** (not "ScreenCap Helper"). Zero recognition friction; safe only because the app never appears in these panes (enforced by R6), so there is no duplicate "ScreenCap" row.
- **Block-with-Retry on failure** (not guided manual-add, not graceful proceed). Never send a non-technical user to Finder; never silently ship a half-granted state.

---

## Dependencies / Assumptions

- Naming the row "ScreenCap" requires the helper bundle's on-disk filename to be `ScreenCap.app` (macOS derives the row label from the filename — verified this session). This nests a same-named bundle inside the app (`ScreenCap.app/Contents/Library/LoginItems/ScreenCap.app`); build/codesign/notarization feasibility of that nesting, with the bundle id kept distinct (`com.screencap.daemon`), is **assumed and must be confirmed in planning**.
- Renaming the helper bundle preserves existing TCC grants (keyed to bundle-id + Developer-ID signature, not filename) — verified this session.
- The request APIs for Screen Recording (`CGRequestScreenCaptureAccess`) and Accessibility (`AXIsProcessTrustedWithOptions` prompt) do create toggleable rows from the daemon's launchd context — confirmed on-device this session.
- Removing decoy rows for identities *other than* the daemon (the orphan old identity and the app's rows in these panes) is achievable via the OS reset facility scoped to those identities; the daemon's own grants must not be touched. To be confirmed in planning.
- Clean-machine + upgrade verification (the U8 runbook, `docs/research/2026-06-30-daemon-helper-bundle-ondevice-validation.md`) is required to confirm the row renders as "ScreenCap" and appears at install on a fresh machine — this session's machine TCC state is polluted.

---

## Outstanding Questions

### Resolve Before Planning

- None — the product decisions are settled.

### Deferred to Planning

- [Affects R1][Technical] The exact install/approval hook where the daemon performs proactive registration, and how registration is re-asserted if it fails or the row is later removed.
- [Affects R3, R9][Needs research] Whether the nested same-name `ScreenCap.app` helper bundle builds, signs, and notarizes cleanly, and whether macOS reliably renders the row as "ScreenCap" on a fresh registration (vs. a cached label).
- [Affects R5][Technical] The precise mechanism and identity-scoping for removing decoy/orphan rows without disturbing the daemon's own grants.
- [Affects R7][Technical] How the app reliably detects "row failed to appear" to drive the block-with-Retry state.
