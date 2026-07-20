---
title: "CLI JSON commands must exit 0 for any envelope field the SwiftUI client reads (a non-zero exit discards stdout)"
date: 2026-07-02
category: integration-issues
module: macos-app-shell
problem_type: integration_issue
component: tooling
symptoms:
  - "The inspect window shows 'Could not load this recording.' right after stop, even though the recording plays fine moments later"
  - "inspect-data --json prints {ok:false, retryable:true} on stdout, but the SwiftUI auto-retry never fires and the window goes straight to a hard failure"
root_cause: wrong_api
resolution_type: code_fix
severity: high
related_components:
  - tooling
tags:
  - macos
  - swiftui
  - cli
  - json-contract
  - exit-code
  - cross-language
  - inspect-data
---

# CLI JSON commands must exit 0 for any envelope field the SwiftUI client reads

## Problem

The SwiftUI shell shells out to the bundled `screencap` CLI and reads its JSON envelope from stdout. Any envelope field the app must act on beyond a hard failure (for example `retryable`) is silently lost if the CLI signals that condition with a non-zero exit code, because `CLIClient.runJSONRaw` throws and discards stdout on a non-zero exit.

## Symptoms

- Opening a recording immediately after stopping it showed "Could not load this recording." even though the recording was intact and played fine seconds later.
- `inspect-data --json` printed `{"ok": false, "retryable": true, ...}` on stdout, yet the inspect window never entered its auto-retry path — it went straight to `.failed`.

## What Didn't Work

Adding `retryable: true` to the error envelope while keeping the CLI's `sys.exit(1)` failure convention. The envelope was correct on stdout, but the Swift side never saw it:

- `CLIClient.runOneShot` (`macos/Screencap/Controllers/CLIClient.swift:243-247`) throws `CLIError.nonZeroExit(code, stderr)` on any non-zero exit and returns only stderr — **stdout is dropped**.
- `runJSONRaw` propagates that throw, so `LiveInspectDataLoader.load` never reaches `JSONDecoder`. The loader's `try await` throws, the ViewModel's catch sets `.failed`, and the auto-retry loop (which only loops on a *decoded* `ok:false + retryable` envelope) is unreachable in production.

The gap was invisible in unit tests because the fake `InspectDataLoader` returns a decoded envelope, bypassing `runJSONRaw` entirely. A code review tracing the real subprocess path caught it before merge.

## Solution

Exit **0** for the transient/retryable case so stdout survives and the envelope reaches the decoder. A genuine hard failure still exits non-zero.

```python
# src/screencap/cli/__init__.py — inspect_data_cmd
try:
    envelope = prepare_inspect_data(name)
except ReviewPrepareBusy as e:            # transient: recording still finalizing
    # Exit ZERO: runJSONRaw discards stdout on a non-zero exit, so a non-zero
    # exit here would drop this envelope before the shell could decode it and
    # retry — reintroducing the exact "Could not load this recording." failure.
    click.echo(json.dumps({
        "ok": False, "schema_version": REVIEW_SCHEMA_VERSION,
        "error": str(e), "retryable": True,
    }))
    return                                 # click default exit code == 0
except ReviewPrepareError as e:            # hard failure: keep non-zero exit
    click.echo(json.dumps({"ok": False, "schema_version": REVIEW_SCHEMA_VERSION, "error": str(e)}))
    sys.exit(1)
```

Verified end to end by holding the recording's `terminal_lock` and running the CLI as a real subprocess: exit code `0` with `{"ok": false, "retryable": true}` on stdout.

## Why This Works

`runJSONRaw` couples "non-zero exit" with "no usable output." The envelope's own `ok` field already carries success/failure, so a transient "not ready yet" result is better modeled as a *successful* command (exit 0) that reports `ok:false` in its payload than as a process failure. With exit 0, `runJSONRaw` returns stdout, the decoder produces the `retryable` envelope, and the ViewModel can branch on it.

## Prevention

- Any field the SwiftUI shell must READ from an envelope (not just `error`) requires the command to exit 0 in that case. Reserve non-zero exits for conditions where the app only needs to know "it failed," never the payload.
- When adding a Swift branch/auto-retry keyed on an envelope field, add a test at the CLI level (a Click `CliRunner` asserting `exit_code == 0` plus the field), not only a fake-loader ViewModel test — the fake loader bypasses `runJSONRaw` and hides exit-code coupling.
- Contract reminder: `CLIClient.runOneShot` throws `CLIError.nonZeroExit` and returns only stderr on a non-zero exit; stdout is discarded. `runJSON` / `runJSONRaw` inherit this.

## Related Issues

- proteus-computer-use/screencap#322 — the view-during-finalization fix that surfaced this.
- `review-data-nullable-timing-swift-consumer-2026-06-01.md` — sibling CLI-to-SwiftUI review-data envelope contract gotcha (nullable timing fields).
