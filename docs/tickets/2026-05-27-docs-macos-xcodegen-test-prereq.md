---
title: "Document xcodegen-generate prerequisite for xcodebuild test on a fresh clone"
status: open
priority: medium
created: 2026-05-27
---

# Document xcodegen-generate prerequisite for xcodebuild test on a fresh clone

## Problem

On a fresh clone of `proteus-computer-use/screencap`, running

```bash
cd macos
xcodebuild test -only-testing:ScreenCapTests \
  -project ScreenCap.xcodeproj -scheme ScreenCap
```

fails Swift compilation with errors like `cannot find type 'RecordingState' in scope`. The cause is environmental, not a code bug: [macos/.gitignore:2](../../macos/.gitignore) intentionally excludes `ScreenCap.xcodeproj/` (including `project.pbxproj`), and the source-of-truth `macos/project.yml` is processed by XcodeGen. New source files added since the last local `xcodegen generate` (e.g. [RecordingStateMachine.swift](../../macos/ScreenCap/Controllers/RecordingStateMachine.swift), [DaemonSessionService.swift](../../macos/ScreenCap/Controllers/DaemonSessionService.swift)) are missing from any stale or absent pbxproj, so Xcode can't see them and dependent symbols (`RecordingState`, etc.) fail to resolve.

Hit during SCR-59 verification (PR #188). Fix is to run `xcodegen generate` from `macos/` first.

[macos/README.md](../../macos/README.md) already has a "Generate the Xcode project" section that documents this step for interactive Xcode use, but:

1. The "Build" section ([macos/README.md:43](../../macos/README.md)) lists `xcodebuild ... build` as a standalone command without back-linking to "Generate the Xcode project" — a contributor jumping straight to the build command on a fresh clone hits the failure.
2. There is **no documented invocation for running tests** from the command line. `xcodebuild test -only-testing:ScreenCapTests` is absent from the README entirely, so anyone trying to run the macOS test suite has to figure it out — and then independently figure out that `xcodegen generate` is a prerequisite.
3. The `brew install xcodegen` requirement is one bullet at the top ([macos/README.md:8](../../macos/README.md)) and easy to skim past.

`script/build_and_run.sh` already does the right thing — it gates on `xcodegen` being installed and regenerates the project when needed ([script/build_and_run.sh:162](../../script/build_and_run.sh)) — but that only helps for the dev-run path, not for test-only CLI invocations.

## What's needed

Pick option 1 first. Escalate to option 2 if two or more contributors report this failure within one month of merge.

1. **Document the prerequisite in `macos/README.md`** (cheapest, low risk).
   - Add an explicit "Run tests" subsection under "Build" with the `xcodebuild test -only-testing:ScreenCapTests` invocation.
   - Add a one-line "before running any `xcodebuild` command from a fresh clone, run `xcodegen generate` first — the `.xcodeproj` is gitignored" callout near the start of "Build" (or as a `> Note:` block right before the first `xcodebuild` snippet).
   - Ensure the `brew install xcodegen` install command is referenced (or repeated) in the new section so a contributor who skipped the Requirements list still sees it.

2. **Add a pre-build wrapper / Makefile target** that runs `xcodegen generate` if `ScreenCap.xcodeproj/project.pbxproj` is missing or older than `project.yml`'s mtime. Reuse the gating logic from [script/build_and_run.sh:162](../../script/build_and_run.sh). Reserve for the case where the escalation trigger above fires (≥2 reports within one month of merging option 1).

3. **Check `project.pbxproj` into git and drop it from `.gitignore`.** Explicitly out of scope here — the file is gitignored deliberately (probably to avoid merge conflicts on a generated artifact), and reversing that decision deserves its own discussion. Do not pursue without first investigating why the .gitignore entry was added.

## Acceptance

- A new contributor can `git clone` and follow [macos/README.md](../../macos/README.md) from Requirements through the "Run tests" section and run `xcodebuild test -only-testing:ScreenCapTests` successfully on first try.
- The new "Run tests" subsection contains either an inline `brew install xcodegen` command or a link that resolves directly to the Requirements bullet (not just the top of the README).
- The `xcodebuild test ...` invocation used during SCR-59 verification is documented in copy-pasteable form (the `cd macos` variant is acceptable as an equivalent).

## Out of scope

- Changes to Swift code, `project.yml`, or the test infrastructure itself.
- Reversing the `.gitignore` decision on `ScreenCap.xcodeproj/`.
- Automating xcodegen for all `xcodebuild` invocations (option 2 above) — escalation only.

## References

- Hit during PR #188 (SCR-59 cursor-unknown snapshot promotion) verification.
- XcodeGen install: `brew install xcodegen`.
- Source of truth for the project file: [macos/project.yml](../../macos/project.yml).
- Existing automated regenerate path (for the dev-run script, not test runs): [script/build_and_run.sh:162](../../script/build_and_run.sh).
