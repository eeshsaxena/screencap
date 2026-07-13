import SwiftUI

/// Stable identifier for the main `Window` scene. Shared by the scene
/// declaration and any consumer that calls `openWindow(id:)` so producer
/// and consumers cannot drift.
let MainWindowID = "main"

@main
struct ScreenCapApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    @StateObject private var recorder = RecorderController()
    @StateObject private var permissions = PermissionController()
    @StateObject private var index = RecordingsIndex()
    @StateObject private var privacy = PrivacyController()
    /// SCR-258 U10: owns the encrypted-store Lock / Unlock actions + the
    /// store-scoped present-user gate. Shared by the shell and the menu bar.
    @StateObject private var store = StoreController()
    @StateObject private var intelligence = IntelligenceController()
    @StateObject private var uploads: UploadCoordinator
    @StateObject private var auth: CloudAuthController

    init() {
        // Register the four bundled typefaces before any view renders (KTD-3).
        // `ATSApplicationFontsPath` also registers them at launch; this is the
        // belt-and-suspenders programmatic path so a folder-reference packaging
        // slip can't silently drop the design's type. Idempotent.
        SCFonts.registerBundledFonts()

        // Upload bookkeeping lives in its own app-wide observable; the auth
        // controller reads its in-flight flag (for `canSignOut`) without owning
        // the count. Build the coordinator first, then hand the auth controller
        // a closure onto it — `@StateObject` property initializers can't
        // cross-reference, so the wiring happens here in `init`.
        let uploads = UploadCoordinator()
        _uploads = StateObject(wrappedValue: uploads)
        _auth = StateObject(wrappedValue: CloudAuthController(
            isUploadInFlight: { [weak uploads] in uploads?.isUploadInFlight ?? false }
        ))
    }

    /// True when the process is running inside the XCTest host, so launch-time
    /// `.task` side effects (auth refresh, daemon probe) don't fire and race the
    /// unit tests. Mirrors the guard the SwiftUI test host needs in every
    /// scene-level `.task`.
    private var isRunningUnderTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }

    var body: some Scene {
        // Use `Window` (macOS 13+) rather than `WindowGroup` so the scene is
        // a true singleton: `openWindow(id:)` focuses the existing window
        // instead of instantiating a new one each call. WindowGroup is
        // multi-window by design — every openWindow(id:) creates a fresh
        // window of the group, which caused duplicate main windows from
        // both the menu bar and Dock/relaunch reopen paths (SCR-55 QA).
        Window("ScreenCap", id: MainWindowID) {
            MainWindow()
                .environmentObject(recorder)
                .environmentObject(permissions)
                .environmentObject(index)
                .environmentObject(privacy)
                .environmentObject(intelligence)
                .environmentObject(auth)
                .environmentObject(uploads)
                .environmentObject(store)
                .frame(minWidth: 880, minHeight: 560)
                .background(OpenWindowBridge())
                .onAppear {
                    appDelegate.bind(recorder: recorder)
                    recorder.bindIndex(index)
                    recorder.bindPermissions(permissions)
                    // SCR-258 U10: lock/unlock refresh the store state via the index.
                    store.bind(index: index)
                    permissions.refresh()
                }
                // Cloud sign-in state is deliberately NOT probed here at launch.
                // `whoami` decrypts the Keychain refresh token, and a decrypt the
                // reading binary's ACL doesn't silently authorize raises a macOS
                // "ScreenCap wants to use screencap-auth" prompt — probing on
                // appear made that fire the instant the app opened, before any
                // cloud interaction. Cloud surfaces (menu-bar account section,
                // Upload gate, cloud onboarding) call `auth.refreshIfNeeded()` on
                // appear instead, so the decrypt/prompt only happens on genuine
                // cloud engagement. Storage-layer fix: SCR-241. Local recording
                // is never gated on auth (R3), so nothing at launch needs it.
                .task {
                    if !isRunningUnderTests {
                        // SCR-262: stale-daemon restart decision FIRST, then the
                        // initial probe (plus the convergence loop when a helper
                        // swap is in flight) — see runLaunchDaemonCheck.
                        await recorder.runLaunchDaemonCheck()
                        // First-launch sequencing: write fail-closed mode →
                        // refresh status → load apps. Runs after the daemon
                        // probe so it doesn't contend with daemon socket
                        // setup. ensureFirstLaunchModeWritten() probes
                        // on-disk and short-circuits when [privacy] already
                        // exists, so this is a one-time cost on first launch.
                        await privacy.ensureFirstLaunchModeWritten()
                        await privacy.refreshStatus()
                        await privacy.refreshApps()
                    }
                }
        }
        // The prototype has no title bar: content fills the window and the
        // real traffic lights overlay the sidebar's reserved chrome slot
        // (design 300–304 draws mock dots there; see ShellSidebarView). The
        // window keeps `.titled` in its styleMask, so the SCR-55 reopen/focus
        // guards (AppDelegate, MenuBarMenu) are unaffected.
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }

        // Per-recording review window scene (plan U3). `WindowGroup` —
        // multi-window by contract — gives R3's "multiple concurrent windows"
        // behavior, distinct from the main scene's singleton `Window`. The
        // payload type pins the scene's value to the recording name so
        // `openWindow(id: ReviewWindowID, value: name)` materializes a fresh
        // window scoped to that recording. SCR-55 documents the
        // WindowGroup-vs-Window distinction.
        WindowGroup("Review", id: ReviewWindowID, for: String.self) { $recordingName in
            // The optional unwrap defends against the system rehydrating a
            // window with a missing/corrupt value; surface a benign placeholder
            // rather than crashing.
            if let name = recordingName {
                ReviewWindow(recordingName: name)
                    .environmentObject(index)
                    .environmentObject(auth)
                    .environmentObject(uploads)
            } else {
                Text("Review window not available.")
                    .padding()
            }
        }
        .windowResizability(.contentSize)

        // Per-recording read-only INSPECT window scene — the "just looking"
        // surface, opened from search results and Recordings-list clicks. Like
        // the review scene it is a `WindowGroup` keyed on the recording name
        // (multi-window by contract — one per recording, R7; SCR-55 documents
        // why the singleton `Window` is the wrong primitive here). It injects
        // ONLY `index`: inspect carries no upload/consent machinery, so it needs
        // neither `auth` nor `uploads`.
        WindowGroup("Inspect", id: InspectWindowID, for: String.self) { $recordingName in
            if let name = recordingName {
                InspectWindow(recordingName: name)
                    .environmentObject(index)
            } else {
                Text("Inspect window not available.")
                    .padding()
            }
        }
        .windowResizability(.contentSize)

        MenuBarExtra {
            MenuBarMenu()
                .environmentObject(recorder)
                .environmentObject(auth)
                .environmentObject(uploads)
                .environmentObject(index)
                .environmentObject(store)
                .environmentObject(privacy)
        } label: {
            MenuBarLabel(isRecording: recorder.state.isRecording)
        }
        .menuBarExtraStyle(.menu)
    }
}

/// MenuBarExtra label. The `record.circle` glyph carries the Aurora accent when
/// idle and the warm-amber recording role when live (SCR-197 U4). The former
/// orange privacy-attention pip is retired: the in-window first-run banner and
/// the permissions sheet already surface that signal, and a scarce-color menubar
/// holds the "calm instrument" discipline — advisories de-color, color is reserved
/// for the recording state and the brand accent (R7).
private struct MenuBarLabel: View {
    let isRecording: Bool

    var body: some View {
        Image(systemName: isRecording ? "record.circle.fill" : "record.circle")
            .symbolRenderingMode(.palette)
            .foregroundStyle(isRecording ? Color.scRecording : Color.scAccent)
            .accessibilityLabel(isRecording ? "ScreenCap, recording" : "ScreenCap")
    }
}

/// Captures `openWindow` for `AppDelegate.applicationShouldHandleReopen`; see
/// `WindowOpener` / SCR-55. Lives inside the main `Window` body so it runs
/// whenever the window is materialized (including after teardown).
/// Also registers the `ReviewWindowOpener` seam (plan U3) so non-SwiftUI
/// contexts (and unit tests) can dispatch into the per-recording review
/// `WindowGroup` without an `@Environment(\.openWindow)` reference.
private struct OpenWindowBridge: View {
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .onAppear {
                WindowOpener.shared.openMain = { openWindow(id: MainWindowID) }
                ReviewWindowOpener.shared.openReview = { name in
                    openWindow(id: ReviewWindowID, value: name)
                }
                InspectWindowOpener.shared.openInspect = { name in
                    openWindow(id: InspectWindowID, value: name)
                }
            }
    }
}
