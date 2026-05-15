# SCR-54 macOS Permission Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make daemon-backed recording depend on daemon/helper permission ownership rather than the Swift app process's cached TCC status.

**Architecture:** Keep `PermissionController` as app-process permission state. Change `RecorderController` so daemon transport does not pre-block or watchdog-stop based on that app state, while CLI fallback keeps the existing permission gate.

**Tech Stack:** Swift 5.9, SwiftUI, XCTest, existing `RecorderController` and `PermissionController` test hooks.

---

### Task 1: Add Recorder Permission Ownership Tests

**Files:**
- Modify: `macos/ScreenCapTests/RecorderControllerTests.swift`
- Modify: `macos/ScreenCap/Controllers/PermissionController.swift`
- Modify: `macos/ScreenCap/Controllers/RecorderController.swift`

- [ ] **Step 1: Write the failing tests**

Add deterministic test hooks to production files under `#if DEBUG`, then add tests that express the desired transport split.

In `macos/ScreenCap/Controllers/PermissionController.swift`, add this inside a `#if DEBUG` extension:

```swift
#if DEBUG
extension PermissionController {
    func _testSetRequiredPermissionsGranted(_ granted: Bool) {
        let status: PermissionStatus = granted ? .granted : .denied
        screenRecording = status
        accessibility = status
        inputMonitoring = status
    }
}
#endif
```

In `macos/ScreenCap/Controllers/RecorderController.swift`, extend the existing `#if DEBUG` test extension:

```swift
    func _testSetTransport(_ transport: RecorderTransport) {
        self.transport = transport
    }

    func _testCheckPermissionsDuringRecording() {
        checkPermissionsDuringRecording()
    }
```

In `macos/ScreenCapTests/RecorderControllerTests.swift`, add:

```swift
    func testDaemonTransportDoesNotBlockStartOnAppProcessPermissions() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)

        recorder.start(name: "daemon-owned")

        XCTAssertEqual(recorder.state, .starting)
        XCTAssertNil(recorder.lastError)
    }

    func testCLIFallbackStillBlocksStartOnAppProcessPermissions() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.cliFallback)

        recorder.start(name: "cli-owned")

        XCTAssertEqual(recorder.state, .idle)
        XCTAssertEqual(recorder.lastError, RecorderController.requiredPermissionsErrorMessage)
    }

    func testDaemonTransportPermissionWatchdogIgnoresAppProcessPermissions() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)
        recorder._testSetPresentation(state: .recording(elapsed: 3))

        recorder._testCheckPermissionsDuringRecording()

        XCTAssertEqual(recorder.state, .recording(elapsed: 3))
        XCTAssertNil(recorder.lastError)
    }
```

- [ ] **Step 2: Run tests to verify the behavior fails**

Run:

```bash
cd macos
xcodebuild test -scheme ScreenCap -destination 'platform=macOS' -only-testing:ScreenCapTests/RecorderControllerTests
```

Expected: `testDaemonTransportDoesNotBlockStartOnAppProcessPermissions` fails because `start()` still blocks before checking transport. The watchdog test fails because app-process denied state still stops an active recording.

- [ ] **Step 3: Implement the minimal transport-aware gate**

In `macos/ScreenCap/Controllers/RecorderController.swift`, change `start(name:)` so the permission preflight runs only for `.cliFallback`:

```swift
        if transport == .cliFallback, let permissions, !permissions.allRequiredGranted {
            lastError = Self.requiredPermissionsErrorMessage
            return
        }
```

In `checkPermissionsDuringRecording()`, ignore daemon transport:

```swift
        guard transport == .cliFallback else { return }
        guard case .recording = state, let permissions else { return }
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
cd macos
xcodebuild test -scheme ScreenCap -destination 'platform=macOS' -only-testing:ScreenCapTests/RecorderControllerTests
```

Expected: all `RecorderControllerTests` pass.

- [ ] **Step 5: Run daemon recorder tests**

Run:

```bash
cd macos
xcodebuild test -scheme ScreenCap -destination 'platform=macOS' -only-testing:ScreenCapTests/RecorderControllerDaemonTests
```

Expected: daemon event-stream tests continue to pass.

### Task 2: Align First-Run Copy and Done Gate

**Files:**
- Modify: `macos/ScreenCap/Views/Privacy/FirstRunPermissionsView.swift`
- Modify: `macos/README.md`

- [ ] **Step 1: Write the failing/static expectation**

Inspect the current `Done` gate:

```bash
rg -n "Done|allRequiredGranted|ScreenCap helper|After granting" macos/ScreenCap/Views/Privacy/FirstRunPermissionsView.swift
```

Expected before implementation: `Done` is disabled by `!permissions.allRequiredGranted`, which is the app-process state.

- [ ] **Step 2: Implement the smaller UX correction**

Change the post-permission copy to avoid promising app-process cache refresh for daemon-owned setup:

```swift
                Text("After enabling ScreenCap in System Settings, return here to continue. If macOS still shows a separate ScreenCap helper entry, enable that entry too.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
```

Change the `Done` gate to helper setup completion:

```swift
                    Button("Done") { isPresented = false }
                        .keyboardShortcut(.defaultAction)
                        .disabled(!isDaemonInstallComplete)
```

- [ ] **Step 3: Update the README stale guidance**

In `macos/README.md`, replace the sentence that says recording is gated by engine-side checks after using `Skip for now` with:

```markdown
If a rebuild leaves the walkthrough's app-process indicators stale, click **Skip for now** or **Done** after the helper is installed to dismiss the sheet and keep testing the rest of the UI. Daemon-backed recording is enforced by the helper/engine at start time; CLI fallback still uses the app/CLI permission path.
```

- [ ] **Step 4: Run focused Swift tests**

Run:

```bash
cd macos
xcodebuild test -scheme ScreenCap -destination 'platform=macOS' -only-testing:ScreenCapTests/PermissionControllerTests -only-testing:ScreenCapTests/RecorderControllerTests
```

Expected: both test classes pass.

### Task 3: Final Verification

**Files:**
- Verify: `macos/ScreenCap/Controllers/RecorderController.swift`
- Verify: `macos/ScreenCap/Views/Privacy/FirstRunPermissionsView.swift`
- Verify: `macos/README.md`

- [ ] **Step 1: Run full macOS unit tests**

Run:

```bash
cd macos
xcodebuild test -scheme ScreenCap -destination 'platform=macOS'
```

Expected: `ScreenCapTests` pass.

- [ ] **Step 2: Review the diff**

Run:

```bash
git diff -- macos/ScreenCap/Controllers/RecorderController.swift macos/ScreenCap/Controllers/PermissionController.swift macos/ScreenCap/Views/Privacy/FirstRunPermissionsView.swift macos/ScreenCapTests/RecorderControllerTests.swift macos/README.md
```

Expected: only SCR-54 permission ownership changes are present.
