---
title: Remove the HUD "Draw" stub - Plan
type: refactor
date: 2026-07-28
topic: remove-hud-draw-stub
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Remove the HUD "Draw" stub - Plan

## Goal Capsule

- **Objective:** Remove the non-functional **Draw** control from the floating recording HUD pill, so the pill stops advertising a feature (SCR-217, draw-on-screen annotation) that does not exist.
- **Product authority:** This document. It narrows one specific decision from `docs/plans/2026-07-03-001-feat-screencap-prototype-ui-plan.md` — expressed there twice, as **KTD-8** ("render every roadmap affordance as a disabled stub, never hide") and as **R5** ("where the backing capability does not exist, the UI element renders per the design but disabled") — for this one control. That plan's own risk register already flagged the aggregate-stub problem and deferred the show-disabled-vs-omit call per surface; this is that call, made for the HUD's Draw stub. The removal is a deliberate per-surface exception to both KTD-8 and R5, not conformance drift.
- **Execution profile:** Small and surgical. One Swift view file plus one manual-QA runbook. Removing the only `stub(_:ticket:)` call site also strands its private helper, so the helper goes with it. **No state machine, recorder, panel-lifecycle, capture-exclusion, or Mute/Hide/Stop behavior changes.**
- **Stop conditions:** Stop and surface if `stub(_:ticket:)` turns out to have a second call site inside `RecordingHUDPanel.swift` (then the helper stays and only the Draw invocation goes), or if removing the control changes the pill's capture-exclusion or panel-sizing path in any way beyond its measured width.
- **Open blockers:** None.

---

## Problem Frame

`macos/Screencap/Views/Record/RecordingHUDPanel.swift` renders a **Draw** label inside the recording pill as a deliberately disabled stub, tagged with a "Coming soon — SCR-217" tooltip and a "Draw, coming soon" accessibility label. It is not a button — it has no action, no `Button` wrapper, and no backing state. Clicking it does nothing.

The stub was intentional under the prototype plan's KTD-8, which chose uniform "render every roadmap affordance as a visible disabled stub" over hiding unbuilt features. That plan's own Risks section named the cost of that policy in the aggregate — roughly fifteen disabled "Coming soon" affordances across the redesigned app reading as "unfinished product" — and explicitly deferred the per-surface show-disabled-vs-omit decision rather than deciding it unilaterally.

The recording HUD is the single highest-exposure surface in the app: it floats over whatever the user is actually doing, on every Space, for the entire duration of every recording. A dead control there is the most expensive dead control in the product. Its sibling stub already resolved the other way — Mute shipped as functional in SCR-254 — leaving Draw as the pill's only non-working element.

**In scope:** removing the Draw control from the pill, removing the helper it strands, and correcting the two places that describe the pill's contents (the file's own header comment and the manual QA runbook).

**Not in scope:** the SCR-217 roadmap item itself (untouched — this removes the placeholder, not the plan to build it), the other ~14 disabled stubs elsewhere in the app, and the KTD-8 policy as a general rule.

---

## Product Contract

### Requirements

- **R1.** The recording HUD pill no longer renders a **Draw** control. No label, no tooltip, no accessibility element.
- **R2.** Every other pill element is unchanged in behavior and relative order: pulsing amber elapsed clock, title chip, divider, Mute, Hide, divider, "Stop & save", and the "recording to this Mac" footer.
- **R3.** The pill's capture-exclusion (`sharingType = .none`), non-activating panel behavior, all-Spaces collection behavior, and bottom-center positioning are untouched.
- **R4.** No code remains in `RecordingHUDPanel.swift` whose only purpose was rendering the Draw stub.
- **R5.** `docs/runbooks/new-ui-manual-qa.md` no longer instructs a tester to verify a control that does not exist.

### Key Decisions

- **Omit rather than keep-disabled, for this control only.** *(session-settled: user-directed — chosen over leaving the stub in place per blanket KTD-8: the feature behind it is not implemented, so the control advertises something the product cannot do.)* The prototype plan deferred the per-surface call; the HUD is the surface where a dead control costs the most, and Draw is now the pill's only non-functional element. Governs R1.
- **The SCR-217 roadmap item survives the removal.** *(Rejected: treating stub removal as cancelling the feature.)* Removing a placeholder is not a product decision to drop draw-on-screen annotation. When SCR-217 is built, it adds a real control; it does not restore a stub. Governs R1, and is why no code comment is left behind pointing at a control that isn't there.

### Acceptance Examples

- **AE1.** Start a recording. The floating pill shows: amber dot + elapsed clock, title chip, divider, Mute, Hide, divider, "Stop & save". There is no **Draw** text anywhere on it.
- **AE2.** With the pill visible, hover where Draw used to sit. No "Coming soon — SCR-217" tooltip appears.
- **AE3.** Click Mute, Hide, and "Stop & save" in a recording. Each behaves exactly as before the change: Mute toggles and shows its rust filled state, Hide dismisses the pill while recording continues, Stop ends the recording and restores the main window on Library.
- **AE4.** The pill still does not appear in its own recording, and still floats over a full-screen app on another Space.

---

## Assumptions

Recorded because this plan was written headless, without a scoping confirmation:

1. **Removing the stranded `stub(_:ticket:)` helper is in scope.** Draw is its only call site inside the file (verified). Leaving a private helper with zero callers would be dead code introduced by this change, not tangential cleanup.
2. **The manual QA runbook is in scope.** Two of its checklist lines instruct a tester to verify Draw renders. Leaving them turns a passing checklist into a false negative on the very next QA pass.
3. **Historical plan documents are not rewritten.** `docs/plans/2026-07-03-001-feat-screencap-prototype-ui-plan.md`, `docs/plans/2026-07-06-001-feat-recording-hud-hide-and-shadow-plan.md`, `docs/plans/2026-07-06-002-feat-recording-toolbar-hide-controls-v2-plan.md`, and `docs/plans/2026-07-11-002-feat-mid-recording-mic-mute-plan.md` reference the Draw stub as a record of decisions made at the time. They are history, not live specification. **Consequence, recorded rather than fixed:** the prototype-UI plan's stub inventory still lists "Draw and Mute on the HUD" among the ~15 disabled affordances. Mute shipped functional in SCR-254 and Draw goes here, so that inventory is stale by two, and nothing at the KTD-8 / R5 policy site points at this exception. Whoever runs the deferred per-surface pass must re-derive the live stub list from the code rather than trusting that inventory.
4. **The design prototype (`docs/design/screencap-prototype/Screencap Prototype.dc.html`) is not edited.** It is an imported design export that gets re-synced from its source, so a hand-edit would be overwritten on the next sync. **Consequence, recorded rather than fixed:** the prototype is cited as the live UI reference by several recent plans and still renders a `Draw` span, so the design artifact and the shipped HUD diverge from this change forward.
5. **The divider preceding the controls group stays.** After Draw is gone it separates the info group (elapsed + title) from the controls group (Mute + Hide) — still its purpose.

---

## Planning Contract

### Key Technical Decisions

- **KTD1 — Delete the control, do not hide or disable it further.** Removing the `stub("Draw", ticket: "SCR-217")` line from the `pill` HStack is the whole behavioral change. No feature flag, no conditional, no commented-out line. A flag would be scaffolding for a decision already made, and a commented-out control is the same dead weight in a less visible form.
- **KTD2 — Delete `stub(_:ticket:)` with its last caller.** The private helper exists solely to render disabled stubs in this pill; Draw was its only invocation. Swift will not warn on an unused private method in a `View` extension, so leaving it means silent dead code. The sibling `ShellSidebar.stub(ticket:)` case is a **different, unrelated** construct in a different file — do not touch it.
- **KTD3 — Correct the two comments that enumerate the pill's contents.** The file header ("a Draw stub, a functional Mute control…") and the `hideButton` doc comment ("A plain 'Hide' label beside Draw and Mute") both describe a control that will not exist. Stale structural comments in a file this small are actively misleading to the next reader.
- **KTD4 — No new unit test; verification is compile + existing suite + manual QA.** The Draw stub had no `RecordingHUDModel` surface — it was a literal in the view body, with nothing to assert against. The repo's established HUD convention (stated verbatim in `docs/plans/2026-07-06-002-feat-recording-toolbar-hide-controls-v2-plan.md` U-level test expectations) is: pure presentation model gets unit tests, pill layout gets manual QA. Adding a view-introspection harness to prove a `Text` is absent would be net-negative machinery for a one-line deletion.

### Patterns to follow

- `macos/Screencap/Views/Record/RecordingHUDPanel.swift` — the pill's `HStack` composition and the existing `muteButton` / `hideButton` / `stopButton` computed-property style. Match the surrounding structure; do not reformat neighbors.
- `docs/runbooks/new-ui-manual-qa.md` §4 and §4a — existing checklist-line phrasing and the `- [ ]` + wrapped-continuation format.

---

## Implementation Units

### U1. Remove the Draw stub and its stranded helper from the HUD pill

- **Requirements:** R1, R2, R3, R4. Covers AE1, AE2.
- **Dependencies:** none.
- **Files:**
  - `macos/Screencap/Views/Record/RecordingHUDPanel.swift` (modify)
- **Approach:**
  1. In the `pill` computed property, delete the `stub("Draw", ticket: "SCR-217")` line from the `HStack`. Leave the surrounding `divider`, `muteButton`, `hideButton`, and `stopButton` and their order untouched (per Assumption 5, the preceding divider stays).
  2. Delete the private `stub(_:ticket:)` method and its doc comment — Draw was its only call site (KTD2). Before deleting, confirm no other invocation exists in the file; if one does, stop per the Goal Capsule stop condition and remove only the Draw invocation.
  3. Update the file header comment so it no longer lists "a Draw stub" among the pill's contents (KTD3).
  4. Update the `hideButton` doc comment so it no longer says the Hide label sits "beside Draw and Mute" (KTD3).
  5. Do not touch `RecordingHUDModel`, `RecordingHUDPanelController`, the panel configuration, or `Color.scHUDMuted` (still used by `muteButton` and `hideButton`).
- **Test scenarios:** `Test expectation: none — the Draw stub had no `RecordingHUDModel` representation to assert against, and pill layout is verified by manual QA per the repo's HUD convention (KTD4).` The existing `RecordingHUDModelTests` must continue to pass unchanged — it never referenced Draw, so any failure there signals an unintended edit.
- **Verification:** The macOS app target compiles. `RecordingHUDModelTests` and `HUDPanelTests` pass unchanged. Reading the file top to bottom, no occurrence of "Draw" or "SCR-217" remains, and no private method is left without a caller.

### U2. Correct the manual QA runbook so it stops testing a removed control

- **Requirements:** R5. Covers AE3 indirectly (the runbook is how AE3/AE4 get exercised).
- **Dependencies:** U1 (the runbook should describe the shipped pill, so land both together).
- **Files:**
  - `docs/runbooks/new-ui-manual-qa.md` (modify)
- **Approach:**
  1. In §4, the checklist line beginning "HUD Draw renders disabled with the SCR-217 tooltip; the **Mute** control is functional…" — drop the Draw clause and keep the rest of the line intact (the Mute/Stop/main-window-restore assertions are still valid and still needed).
  2. In §4a, the line "Pill shows a plain **'Hide'** button beside Draw and the Mute control (not a chevron icon)…" — reword so the Hide button's position is described against the controls that remain, keeping the "not a chevron icon" assertion, which is the actual point of that check.
  3. Change nothing else in the runbook. Preserve the existing `- [ ]` formatting and continuation-line indentation.
- **Test scenarios:** `Test expectation: none — documentation change with no executable surface.`
- **Verification:** No line in `docs/runbooks/new-ui-manual-qa.md` asks a tester to observe a Draw control or an SCR-217 tooltip. Every surviving assertion on those two lines is still true of the shipped pill. A reviewer following §4 and §4a against a real recording finds nothing that fails for a reason this change introduced.

---

## Verification Contract

1. **Compile.** The macOS app target builds. Per `project_xcodebuild_in_worktree_tcc_bricks_session`, this worktree lives under `~/dev` (not `~/Documents`), so a compile-only build here is brick-free:

```bash
cd macos && xcodegen generate && xcodebuild build-for-testing -scheme Screencap -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" -derivedDataPath /private/tmp/screencap-dd
```

2. **Existing tests stay green.** Run them — `build-for-testing` above only compiles the bundle:

```bash
cd macos && xcodebuild test-without-building -scheme Screencap -destination 'platform=macOS' -only-testing:ScreencapTests/RecordingHUDModelTests -only-testing:ScreencapTests/HUDPanelTests -derivedDataPath /private/tmp/screencap-dd
```

   Reusing item 1's `-derivedDataPath` means nothing recompiles. Running the test host is also brick-free from this `~/dev` worktree — the same memory records a full `xcodebuild test` run from `~/dev/screencap/<name>` with zero session damage; the launch-time hazard is bound to test hosts under `~/Documents`.

   `RecordingHUDModelTests` is the real gate: it covers the pill's presentation model, and neither it nor `HUDPanelTests` referenced Draw, so a failure means an unintended edit. **`HUDPanelTests` cannot fail from this diff** — it exercises the shared `HUDPanel.captureExcluded` factory (used by the peek and hint panels), while `RecordingHUDPanelController.ensurePanel` builds its `NSPanel` by hand and sets `sharingType = .none` itself. Run it as a cheap non-regression check, but R3's capture-exclusion assurance rests on AE4 in the manual pass, not on this test. Note the known-flaky `RecorderControllerDaemonTests/testDaemonTransportHappyPathUsesEventStreamForStateTransitions` — re-run it in isolation before attributing a failure to this diff.
3. **Grep proof.** No occurrence of `Draw` or `SCR-217` remains in `macos/Screencap/Views/Record/RecordingHUDPanel.swift`, and no occurrence of `Draw` or `SCR-217` remains in `docs/runbooks/new-ui-manual-qa.md`. Both tokens matter on both sides: the §4a runbook line carries "Draw" with no ticket number, so a `SCR-217`-only grep would pass while the stale text survives.
4. **No stranded code.** `stub(` has no remaining call site or definition in `RecordingHUDPanel.swift`. `ShellSidebar.swift`'s unrelated `.stub(ticket:)` case is untouched.
5. **Manual QA (AE1–AE4), deferred to the next QA pass.** Start a recording and confirm the pill reads amber clock · title · divider · Mute · Hide · divider · Stop & save with no Draw and no SCR-217 tooltip; confirm Mute, Hide, and Stop each still behave as before; confirm the pill still stays out of its own recording and still floats over a full-screen app on another Space. This is not a merge gate for a one-line view deletion, but it is the check that closes AE3/AE4.

---

## Scope Boundaries

**Non-goals:**

- Building SCR-217 (draw-on-screen annotation). The roadmap item is unaffected by removing its placeholder.
- Removing or re-deciding any of the app's other disabled stubs — the Collections sidebar section, the capture-mode cards, the camera and MCP rows, Clip on the timeline, the E2EE and private-window rows, the storage "Change…" button, the default-for-new-apps control. Each is a separate surface with its own cost/benefit.
- Revising KTD-8 in `docs/plans/2026-07-03-001-feat-screencap-prototype-ui-plan.md` as a general policy. This plan makes one per-surface call, which is exactly what that plan deferred.
- Rewriting historical plan documents or the design prototype HTML (Assumptions 3 and 4).
- Any change to the pill's panel lifecycle, sizing, capture exclusion, or positioning.

**Deferred to Follow-Up Work:**

- A deliberate pass over the remaining ~14 disabled stubs, deciding show-disabled vs. omit per surface — the aggregate concern the prototype plan raised and deferred at P1. **File this ticket as part of this change rather than gating it on a later judgment call.** Removing the single most-seen stub relieves exactly the symptom that would otherwise trigger the systematic pass, so a "still a live worry?" condition is one this change is designed to make read as false. The HUD Draw call is its first precedent, and the ticket should also record the KTD-8 / R5 exception so the policy site is reachable from the exception.

---

## Definition of Done

- [ ] The Draw control is gone from the HUD pill; every other pill element is unchanged (R1, R2).
- [ ] `stub(_:ticket:)` is deleted from `RecordingHUDPanel.swift` — no definition, no caller (R4).
- [ ] The file header comment and the `hideButton` doc comment no longer describe a Draw control (KTD3).
- [ ] `docs/runbooks/new-ui-manual-qa.md` §4 and §4a no longer ask a tester to verify Draw or the SCR-217 tooltip, and their surviving assertions are still accurate (R5).
- [ ] The macOS app target compiles; `RecordingHUDModelTests` and `HUDPanelTests` pass unchanged.
- [ ] Capture exclusion, panel behavior, and positioning are untouched (R3) — no edits outside the pill's `HStack`, the stranded helper, and two comments.

---

## Sources & Research

- `macos/Screencap/Views/Record/RecordingHUDPanel.swift` — the Draw stub (pill `HStack`), the `stub(_:ticket:)` helper and its sole call site, the file header comment, and the `hideButton` doc comment.
- `docs/runbooks/new-ui-manual-qa.md` §4, §4a — the two manual-QA checklist lines that assert Draw renders.
- `docs/plans/2026-07-03-001-feat-screencap-prototype-ui-plan.md` — U7 (HUD origin), KTD-8 (the stub-everything policy), the SCR-217 roadmap row, and the Risks entry that deferred the per-surface show-disabled-vs-omit decision this plan now makes for the HUD.
- `docs/plans/2026-07-06-002-feat-recording-toolbar-hide-controls-v2-plan.md` — the repo's stated HUD test convention ("layout is not unit-testable", verified by manual QA), which grounds KTD4.
- `docs/plans/2026-07-11-002-feat-mid-recording-mic-mute-plan.md` — records Mute's transition from stub to functional (SCR-254), leaving Draw as the pill's last non-working control.
- `macos/ScreencapTests/RecordingHUDModelTests.swift`, `macos/ScreencapTests/HUDPanelTests.swift` — the existing HUD test surface; neither references Draw.
- Memory `project_xcodebuild_in_worktree_tcc_bricks_session` — confirmed-safe compile path from a `~/dev` worktree, used in the Verification Contract.

No external research was run: the change is entirely local, the codebase supplies the pattern directly, and nothing here depends on external prior art.
