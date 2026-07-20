---
title: "Fallback transports: keep recovery affordances reachable and process exit signals distinguishable"
date: 2026-06-15
category: design-patterns
module: macOS app shell — permission onboarding + recorder transport
problem_type: design_pattern
component: macos/Screencap (PermissionController, MainWindow, PrivacyPaneView, RecordingStateMachine); screencap.engine preflight
platform: macos
severity: medium
applies_when:
  - "A persisted opt-out / dismissed flag suppresses a UI prompt and is auto-cleared only under some conditions"
  - "A feature has a happy path and a fallback path with different state availability (e.g. daemon transport vs CLI fallback)"
  - "A child process reports failure to a GUI parent via exit codes and/or stderr"
tags: [macos, permissions, transport-fallback, state-machine, exit-codes, error-handling, recovery-ux]
---

# Fallback transports: keep recovery affordances reachable and process exit signals distinguishable

## Context

The macOS first-run permission walkthrough persisted a "Skip for now" dismissal flag (`permissionSetupDismissed`) to stop the sheet re-popping on every launch. Its **only** auto-clear condition was "all required *daemon* grants granted" (`updateDaemonGrants` → `clearSetupDismissed`). On the **CLI-fallback** transport (helper/daemon not installed — exactly where you land if you skip the install step) daemon grants stay `indeterminate` forever, so the flag never cleared, and no other affordance reopened the walkthrough. A mistaken Skip was a permanent dead end.

Separately, when the spawned recorder failed its permission preflight it exited with code `1` — the same code the engine uses for a generic crash (`_stderr_events.py`: `1=generic failure`). The actionable guidance the engine printed went to a console the GUI never reads, so the only feedback was the inscrutable "Recorder exited with code 1."

Surfaced and fixed in PR #232; the precise engine-signal follow-up is tracked as SCR-142.

## Guidance

Two related rules, both about information lost at a boundary:

1. **A persisted opt-out flag needs a recovery trigger that holds on every path the user can actually be in — not just the happy path.** If the auto-clear (re-arm) keys on happy-path-only state (here: daemon grants), then on the fallback path the flag is write-once-forever. Either make the clear condition reachable on the fallback path too, **or** add a path-independent escape hatch: an explicit, always-available "reopen" affordance that bypasses the suppression gate. Prefer the explicit affordance — it does not depend on any transport reaching a "complete" state, and it keeps the original anti-nag behavior intact.

2. **Don't overload one process exit code across distinct, separately-actionable causes.** When a child process reports failure to a GUI parent, reserve a distinct exit code (or emit a structured event on the channel the GUI parses) per cause the consumer would handle differently. A code that means both "generic failure" *and* "missing permission" forces the consumer to guess or to show a useless message. Human-readable guidance printed to a TTY the GUI never attaches to is invisible — actionable signal must ride the structured channel.

## Why This Matters

- **Happy-path-only re-arm silently strands fallback users.** The dead-end is invisible to any test that only exercises the primary transport, so it ships looking fine. The escape hatch (or a path-independent clear) is the only thing that makes a "dismiss" reversible everywhere.
- **Unactionable errors erode trust and block self-service.** "Exited with code 1" gives the user nothing to do. Naming the likely cause and pointing at a concrete recovery turns a dead end into a next step — even before the precise (structured) signal exists.
- **Avoid the tempting wrong fix.** Auto-clearing the dismissal on *app-process* grants was rejected here: the CLI-fallback presentation gate ignores app-process grants, so clearing the flag would re-nag the walkthrough on every launch. The reachable-recovery rule is satisfied by the explicit affordance, not by widening the auto-clear.

## When to Apply

- A persisted "dismissed"/"snoozed"/"opt-out" flag gates a prompt, and its clear condition depends on state that only one code path produces.
- A feature degrades across transports/modes (daemon vs CLI, online vs offline, native vs web fallback) where the same "completion" signal isn't available in every mode.
- A spawned process (CLI child, worker, subprocess) communicates failure to a GUI/parent and you are choosing exit codes or stderr conventions.

## Examples

**Dead-end re-arm (before) → path-independent escape hatch (after):**

```swift
// BEFORE: the ONLY clear path — never reached on CLI-fallback (grants stay indeterminate)
func updateDaemonGrants(_ grants: DaemonPermissionGrants) {
    daemonGrants = grants
    if grants.allRequiredGranted { clearSetupDismissed() }   // happy-path only
}

// AFTER: an explicit, transport-independent way back that bypasses the suppression gate.
// Skip stays sticky; this just makes it reversible from the Privacy tab.
@Published private(set) var reopenSetupRequested = false
func requestReopenSetup() { reopenSetupRequested = true }   // "Finish setup" button
// MainWindow observes the latch and presents the sheet even though setupDismissed == true.
```

**Overloaded exit code (before) → actionable mapping (after):**

```swift
// BEFORE: exit 1 falls through to the generic default
default:
    effects.append(.surfaceError("Recorder exited with code \(exitCode)."))

// AFTER: exit 1 on the CLI-fallback path is most often the permission preflight bailing.
// Hedged wording (1 is also the generic-failure code) + a pointer to the recovery affordance.
case 1:
    effects.append(.surfaceError(
        "Recording couldn't start. This usually means Screen Recording, "
        + "Accessibility, or Input Monitoring isn't granted to the recorder — "
        + "open the Privacy tab and choose \"Finish setup\" to grant them."))
```

The fully-correct version of the second fix — a distinct exit code plus a structured `permission_required` event so the GUI can name the *exact* missing permission and route it through the existing grant-flow alert — is the cross-language follow-up (SCR-142).

## Related

- PR #232 — `fix(macos): recover skipped permission setup, clarify start errors`
- SCR-142 — engine-side structured permission signal + distinct exit code (the precise fix)
- [[../runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch]] — related macOS TCC per-process caching behavior
