---
module: macos
tags: [tcc, code-signing, smappservice, migration, scr-196]
problem_type: migration
---

# Daemon TCC identity migration: bare `screencap` → helper bundle `com.screencap.daemon`

## What changed (SCR-196)

The recording daemon used to ship as a **bare Mach-O** at
`Screencap.app/Contents/Resources/screencap/screencap` whose code-signing
identifier was the PyInstaller default `screencap` (no `Info.plist`, no bundle
id). It now ships as a proper helper **`.app` bundle** at
`Screencap.app/Contents/Library/LoginItems/ScreencapDaemon.app` with
`CFBundleIdentifier=com.screencap.daemon`, so macOS treats it as a first-class
TCC subject (`tccutil`-targetable, persistent across rebuilds).

> **Caveat (SCR-201): "auto-listed" holds for Accessibility and Input Monitoring
> only.** Those attribute to the helper's own identity. **Screen Recording does
> not** — macOS rolls a nested LoginItem's SR request/capture up to the
> responsible host app (`com.screencap.macos`), so the SR row lists under the
> app ("Screencap"), not the helper. See
> `docs/solutions/integration-issues/macos-screen-recording-tcc-host-app-rollup-2026-07-02.md`.

The launchd **Label** is unchanged (`com.screencap.daemon` — it always was), so
the LaunchAgent install/bootout machinery is unaffected. Only the **binary's
code identity** changed.

## Why existing testers must re-grant once

TCC stores each grant against the subject's **designated requirement (`csreq`)**,
which pins the code-signing identifier. Changing the identifier from `screencap`
to `com.screencap.daemon` (even with the same Developer-ID Team) means the old
`csreq` no longer matches, so prior Screen Recording / Accessibility / Input
Monitoring grants are **orphaned** — they neither carry over nor block the new
identity. This is a one-time, deliberate re-grant, communicated in advance.

## Tester upgrade steps (one time)

1. Quit Screencap and replace `/Applications/Screencap.app` with the new build.
   (Replacing the bundle removes the old bare exec, so the headless auto-spawn
   path — `screencap status` → `posix_spawn` — cannot resurrect the old-identity
   daemon.)
2. Launch Screencap. The first-run walkthrough / "Finish setup" banner will show
   the three permissions as **not granted** (expected — the grants were orphaned).
   The daemon-install flow's existing bootout + version reconciliation
   (SCR-121/135) evicts any surviving old daemon and binds the new helper
   automatically; no manual `launchctl` step is needed in the normal case.
3. Grant each permission, matching the row **per pane** (the label differs — see
   SCR-201). In **Accessibility** and **Input Monitoring** the daemon's row reads
   **"ScreencapDaemon"** (the helper bundle's `.app` filename — `CFBundleDisplayName`
   does not override it; SCR-200 U2 / SCR-201 U1) — enable that one; the app's
   stray **"Screencap"** row in Accessibility is a decoy. In **Screen Recording**
   there is no helper row: the grant rolls up to the host app, so enable the
   **"Screencap"** row (that same app identity owns Microphone too).
4. If macOS prompts to approve a new Login Item / background item, approve it
   (SMAppService may re-flag the helper under its new identity).
5. Use **Restart to apply permissions** (or quit + reopen) so the per-process TCC
   cache is re-read (`macos-tcc-per-process-cache-quit-and-relaunch.md`).

## Cleaning up orphaned old rows (optional hygiene)

The orphaned `screencap` `csreq` rows remain in `TCC.db` as harmless dead
entries (the binary they pin no longer ships). They cannot be cleared by
`tccutil reset <service> com.screencap.daemon` (that targets the *new* identity).
To remove them, run once:

```bash
tccutil reset All screencap
```

This targets the **old** bare identifier. It is optional — the dead rows do not
affect the new helper — but keeps the Privacy panes tidy, especially on dev
machines that accumulated many ad-hoc rebuild rows.

## Peer-plan note (resolution A)

The Developer-ID notarized-distribution plan
(`docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`)
documents the daemon launch/sign path as
`Contents/Resources/screencap/screencap`. SCR-196 relocates that to the helper
`.app`. We intentionally did **not** rewrite that peer plan's scope; this note
records the relocation so its signing/notarization runbook is reconciled to the
new path when that plan is next executed.

## See also

- `docs/plans/2026-06-30-002-feat-daemon-helper-bundle-tcc-plan.md` (SCR-196 plan)
- `docs/research/2026-06-30-daemon-helper-bundle-ondevice-validation.md` (U8 gate runbook)
- `docs/solutions/integration-issues/macos-screen-recording-tcc-host-app-rollup-2026-07-02.md` (SCR-201 — SR attribution rolls up to the host app; the corollary this migration doc's SR claims missed)
- `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`
