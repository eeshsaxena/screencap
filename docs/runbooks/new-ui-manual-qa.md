---
title: "Runbook: new-UI manual QA checklist"
date: 2026-07-03
type: runbook
plan: docs/plans/2026-07-03-001-feat-screencap-prototype-ui-plan.md
unit: U14
status: ready
---

# Runbook: new-UI manual QA (prototype UI rebuild, U14)

The paths unit tests cannot reach: window/menu-bar topology, TCC grant loops,
Spaces/full-screen behavior, and the daemon lifecycle across app updates. Run
this top-to-bottom on a **clean machine state** after any change to the shell,
onboarding, HUD, or window lifecycle — and once per release candidate.

Design authority: `docs/design/screencap-prototype/Screencap Prototype.dc.html`
(open it beside the app for the fidelity passes). Honesty substitutions
(KTD-9) are intentional differences: badges read "uploaded" not
"shared · encrypted", the HUD footer has no "· encrypted", storage/account
steps carry no pricing or encryption claims.

## 0. Machine prep (hygiene — do not skip)

Dev builds churn TCC: each rebuild is a new code signature, so
Screen Recording / Accessibility / Input Monitoring grants silently go stale
and permission-flow bugs become unreproducible noise. Before a QA pass:

1. Quit ScreenCap; `screencap stop` any live recording; uninstall the
   LaunchAgent if present (`launchctl bootout gui/$UID/com.screencap.daemon`
   — label per `screencap setup` output).
2. Sweep stray app copies — `mdfind "kMDItemFSName == 'ScreenCap.app'"` —
   and delete every copy except the build under test. TCC resolves grants per
   *path+signature*; a stray copy answering the daemon socket or holding a
   grant poisons the pass.
3. Reset state for the fresh-install legs:
   `tccutil reset ScreenCapture; tccutil reset Accessibility;
   tccutil reset ListenEvent; tccutil reset Microphone`,
   `defaults delete com.screencap.app` (ignore "does not exist"),
   and move `~/.screencap` aside (restore it for the upgrade leg).
4. **Reboot.** TCC and launchd cache per-process state; a reboot is the only
   reliable flush (see
   `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`).

## 1. Onboarding — fresh install (wizard takeover)

State: no `~/.screencap`, no LaunchAgent, TCC reset, defaults cleared.

- [ ] First launch replaces the window content with the wizard (no shell, no
      permissions sheet popping over it). Progress dots match the design.
- [ ] Welcome → Permissions: tiles report *daemon-subject* status honestly
      (no green checkmarks before the helper is installed and granted).
- [ ] Grant Screen Recording mid-wizard → macOS requires relaunch: use the
      wizard's Quit & Relaunch. On relaunch the wizard **re-derives the same
      step** — it never restarts from Welcome and never skips ahead of an
      ungranted requirement (KTD-10: no stored step integer).
- [ ] App-rules step: "Edit the list" deep link → finishing the wizard lands
      on App rules; otherwise finishing lands on Library.
- [ ] Storage step shows three cards; Personal cloud / Team cloud carry no
      pricing, billing, or encryption claims (SCR-229 / SCR-221 / SCR-220).
      "Keep it on this Mac" completes at 4 dots; a cloud pick extends to the
      account step (browser sign-in round-trip works); Team adds the stubbed
      team-setup step (6 dots).
- [ ] Finish writes the completion marker: quit + relaunch → shell, no wizard.
- [ ] Sidebar "Replay onboarding" re-enters the wizard read-only — live grant
      states, and finishing writes nothing (marker file mtime unchanged).
- [ ] Replay is refused while a recording is active.

## 2. Onboarding — upgrade path (no wizard, migration interstitial)

State: restore a pre-existing `~/.screencap` (recordings + config) or install
the previous release first; completion marker absent.

- [ ] Launch: **no marketing wizard** (prior-install evidence backfills the
      marker). The one-time migration interstitial / permission walkthrough
      sheet shows instead, exactly once — dismissing it survives relaunch.
- [ ] The first-run privacy banner (FirstRunPrivacyBanner) appears above the
      split view in the new shell; "Review settings" routes to Privacy
      settings; both actions mark setup complete and the banner never returns.

## 3. Duplicate-window guards (SCR-55)

- [ ] Close the main window (app stays in menu bar) → menu bar "Open
      ScreenCap" restores the **same** window; repeat 5× rapidly — never a
      second main window.
- [ ] With the window already open, "Open ScreenCap" focuses it (no new
      instance); with the permissions sheet attached, focusing targets the
      parent window, not the sheet.
- [ ] Dock-icon reopen (click the Dock icon after closing the window) goes
      through the same guard — one window.
- [ ] Review and Inspect windows: open the same recording's Review twice from
      the Library card menu → the existing per-recording window is focused,
      not duplicated; two *different* recordings open two Review windows.

## 4. Recording lifecycle + HUD (U7)

- [ ] Library "New recording" pill → sheet (design 600–652): mode cards
      (Window/Area disabled with SCR-215 tooltips), mic row with live level
      meter **only if** mic TCC already granted (opening the sheet must never
      trigger a permission prompt), camera + MCP rows stubbed.
- [ ] Start recording → main window hides; the floating HUD appears
      bottom-center of the recorded display.
- [ ] HUD is a non-activating panel: clicking its buttons never steals focus
      from the frontmost app.
- [ ] HUD follows across Spaces and over a full-screen app.
- [ ] The HUD does **not** appear in its own recording (capture-excluded),
      and the footer reads "recording to this Mac" with no encryption claim.
- [ ] HUD Draw renders disabled with the SCR-217 tooltip; the **Mute** control is
      functional (SCR-254, see the Mute section below); Stop ends the recording;
      the main window restores on Library with the fresh card in `processing` →
      `ready` state.
- [ ] Menu bar during recording: icon swaps, Stop Recording (⌘⇧S) works;
      menu-bar Start (⌘⇧R, idle) starts with the sheet's defaults (flip
      `audio_default` off via
      `screencap settings set recording.audio_default false --json` — a
      menu-bar start must record without audio).
- [ ] CLI fallback: `launchctl bootout` the daemon, quit + relaunch the app →
      recording still starts via the CLI path (walkthrough may surface per
      grant state); the "running without the background helper" advisory shows
      in Library.

### 4a. Hide controls v2 — ⌘⇧H, edge peek, one-time hint (design 8a)

Run these with a *second app focused* (e.g. a browser) so the global paths are
genuinely exercised — a window-scoped shortcut would pass this only by accident.

- [ ] Pill shows a plain **"Hide"** button beside Draw and the Mute control (not a
      chevron icon); clicking it dismisses the pill and the recording continues.
- [ ] With focus in the recorded app, **⌘⇧H** hides the pill; **⌘⇧H** again
      restores it bottom-center. The elapsed timer keeps advancing throughout.
- [ ] With **no** recording active, ⌘⇧H does nothing in ScreenCap and reaches
      the frontmost app normally (the hotkey is registered only while recording).
- [ ] While hidden, rest the cursor at the **bottom screen edge** → after a brief
      dwell a slim "Show controls ⌘⇧H" bar appears; move away and it hides without
      pinning; move up onto the bar and click → the toolbar restores bottom-center
      (the bar must not dismiss before the click lands). Confirm the band clears a
      bottom **Dock** (default Dock position).
- [ ] **First-ever hide only:** a one-time hint appears near the menu bar naming
      it as where status/Stop live and ⌘⇧H to return; it never appears on
      subsequent hides (this or later recordings). Reset with
      `defaults delete com.screencap.app com.screencap.macos.hasShownHideHint`
      (or the test host's domain) to re-arm.
- [ ] **Capture exclusion (blocker if it fails):** across hide, ⌘⇧H, the peek
      bar, and the hint, none of these ScreenCap surfaces appears in the captured
      video, and capture keeps writing (`sharingType = .none`).
- [ ] **No new permission prompt:** exercising ⌘⇧H and the edge peek triggers no
      Accessibility / Input Monitoring dialog beyond what recording already needs.
- [ ] **VoiceOver:** the peek bar reads as "Show recording controls"; the hint is
      announced; ⌘⇧H restores the toolbar for a keyboard-only user.
- [ ] **⌘⇧H collision:** register ⌘⇧H in another app first, start a recording →
      ScreenCap logs the registration failure and the menu-bar "Show recording
      controls" item still restores the pill (graceful degradation, no dead hotkey).

### 4b. Mid-recording mic mute (SCR-254 / SCR-218)

Requires the **daemon transport** (mute is daemon-only; the CLI-fallback pill/menu
mute control is inert). Rebuild the embedded daemon (PyInstaller) so it carries the
`recording.mute` verb before testing.

- [ ] Start an **audio-on** recording (mic granted). The HUD pill shows **"Mic on"**
      with a `mic.fill` icon. Speak, then click Mute → briefly **"Muting…"**
      (disabled), then a filled **rust "Muted"** pill with `mic.slash.fill` **only
      after** capture actually stops (never optimistically before). Speak again,
      click to unmute → **"Unmuting…"** → **"Mic on"**.
- [ ] **No audio on disk for the muted span:** inspect the recording's audio /
      transcript — the muted interval holds no speech and shows a `[microphone
      muted]` marker (transcript check is U6).
- [ ] **Menu-bar item:** the menu shows a state-reflecting mic item beside Stop
      with the SAME grammar ("Mic on" / "Muted"); toggling from the menu bar moves
      both surfaces in lockstep (one shared state, R7). The item is absent when not
      recording.
- [ ] **Unmute a `--no-audio` recording (R2):** start with mic off (flip
      `audio_default` off or use the sheet). The control reads **"Muted"**; click it
      → with mic permission granted the mic turns on and it reads **"Mic on"**.
- [ ] **Unmute permission — undetermined (AE2):** on a machine where mic TCC is
      undetermined, unmute triggers the standard macOS mic prompt; on grant, capture
      starts and the control reads "Mic on".
- [ ] **Unmute permission — denied (AE3, R3):** with mic access denied, unmute shows
      a **visible** "Microphone access needed" modal (Open System Settings), the
      recording keeps running **muted**, and it is never a silent no-op. Verify this
      **from the menu bar with the HUD hidden** too — the modal appears (and the pill
      is auto-revealed), not an invisible inline-only error.
- [ ] **Verb failure is non-terminal:** if the mute request can't reach the daemon,
      the recording keeps running and an advisory (menu dropdown) says the mic state
      is unchanged — the recording is **not** torn down.
- [ ] **Reconnect preserves state (AE4):** mute, then drop/restore the events
      subscription (e.g. `launchctl kickstart` the daemon is too heavy — instead
      briefly lose contact) → after re-attach the pill/menu still show "Muted"
      without a re-toggle.
- [ ] **VoiceOver:** the Mute control reads "Microphone on, tap to mute" /
      "Microphone muted, tap to unmute" (never a bare "Muted").

## 5. TCC quit-relaunch loop (permission loss + recovery)

- [ ] With everything granted, `tccutil reset ScreenCapture` while the app
      runs → the watchdog/permission machinery surfaces the missing grant
      (no silent black recordings); re-grant → Quit & Relaunch flow restores
      recording. Per-process TCC caching means the *old* process may still
      look granted — the relaunch is the fix, verify the prompt says so.
- [ ] Privacy settings "Finish setup" (visible after a skipped setup with a
      missing grant) reopens the walkthrough sheet, and it does **not**
      auto-close out from under you while you read it (SCR-144).

## 6. Stale-daemon restart (app update path)

- [ ] Simulate an update: with the old build's daemon still running, launch
      the new build → the app detects the stale daemon and restarts it on
      launch; Library loads (no persistent HTTP-500 "Couldn't load
      recordings"). If Library does error, the state offers "Restart helper"
      and it heals (see
      `docs/solutions/integration-issues/stale-daemon-after-app-update-http-500-2026-07-02.md`).

## 7. Search: consent, backfill, palette (U10)

- [ ] ⌘⇧F opens the Recall palette **only when the ScreenCap window is key**
      (window-scoped, KTD-13); menu bar "Search…" opens/focuses the window
      with the palette pre-opened.
- [ ] With `content_index_enabled` off: free-text search shows the consent
      banner; Enable turns indexing on (verify
      `screencap settings --json | jq .settings.content_index_enabled`);
      declining persists and stops re-prompting.
- [ ] After consent with prior recordings: backfill offer appears; Accept
      shows determinate progress and a terminal state; Cancel/resume works;
      Skip persists the decline.
- [ ] ↵ on a hit lands on the Day timeline at that moment; esc unwinds
      palette → (sheet if open) → nothing.
- [ ] Palette results show only pointer data (app, title, snippet, time) —
      spot-check VoiceOver reads a row as "{lead}. {stream}. at {time}".

## 8. Upload consent flow (Review window — load-bearing, R6)

- [ ] Library card menu "Review & upload…" opens the per-recording Review
      window; the recording uploads **only** after explicit consent there —
      no path in the new UI uploads without the Review window.
- [ ] After a successful upload the Library card flips to "uploaded" (index
      refresh on the upload notification) and the chip filters (All / Local /
      Uploaded / Needs review) bucket it correctly.
- [ ] Badges/chips say "uploaded" — never "shared" or "encrypted" (KTD-9).

## 9. Journal, timeline, settings fidelity pass

- [ ] Journal groups by day with app chips; the "split by the agent" affordance
      is stubbed (SCR-214); search pill opens the palette.
- [ ] Day timeline: playback across chunk/recording boundaries; gaps render
      as neutral "nothing captured" placeholders; hatched regions appear
      **only** where fail-closed blocked data proves it (labeled "blocked");
      Clip is stubbed (SCR-219).
- [ ] Privacy settings: every toggle round-trips through
      `screencap settings … --json` (verify one write lands in
      `screencap settings --json` output); "Keep recordings local by default"
      shows ON only for `local`, with the caption naming `ask`/`cloud`/`both`
      defaults; the E2EE row is informational, no active toggle (SCR-220).
- [ ] App rules: segment taps write through the CLI layer; matrix-excluded
      rows are locked; rows keep a stable order across taps.
- [ ] Fonts: Newsreader (serif headers), Space Grotesk, IBM Plex Mono
      (chips/times), Public Sans render — no Helvetica/system-font fallback
      anywhere (a fallback means `ATSApplicationFontsPath` broke).

## Run record

| Date | Build | Machine | Sections run | Result / notes |
|---|---|---|---|---|
| | | | | |
