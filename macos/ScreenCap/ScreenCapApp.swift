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
    @StateObject private var uploads: UploadCoordinator
    @StateObject private var auth: CloudAuthController

    init() {
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
                .environmentObject(auth)
                .environmentObject(uploads)
                .frame(minWidth: 880, minHeight: 560)
                .background(OpenWindowBridge())
                .onAppear {
                    appDelegate.bind(recorder: recorder)
                    recorder.bindIndex(index)
                    recorder.bindPermissions(permissions)
                    permissions.refresh()
                }
                .task {
                    // Cloud sign-in state. Runs concurrently with the daemon
                    // probe below (separate `.task`) so a slow `whoami` refresh
                    // never delays recording-engine startup. Local recording is
                    // never gated on auth (R3).
                    if !isRunningUnderTests {
                        await auth.refresh()
                    }
                }
                .task {
                    if !isRunningUnderTests {
                        await recorder.probeDaemon()
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
        } label: {
            MenuBarLabel(
                isRecording: recorder.state.isRecording,
                privacyAttention: privacy.bannerActive
            )
        }
        .menuBarExtraStyle(.menu)
    }
}

/// MenuBarExtra label composition. `record.circle` base layer is always
/// rendered; the recording state drives the fill, and the privacy-attention
/// badge is overlaid in the upper-right corner when the first-run banner is
/// active. Color and corner position keep the attention dot visually
/// distinct from the recording red dot — co-existing while recording is the
/// expected case during the first-launch window.
private struct MenuBarLabel: View {
    let isRecording: Bool
    let privacyAttention: Bool

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Image(systemName: isRecording ? "record.circle.fill" : "record.circle")
                .symbolRenderingMode(.palette)
                .foregroundStyle(isRecording ? Color.red : Color.primary)
            if privacyAttention {
                Circle()
                    .fill(Color.orange)
                    .frame(width: 5, height: 5)
                    .overlay(
                        // `Color.primary` adapts to the menu bar appearance so
                        // the hairline shows up against both light and dark
                        // menu bar wallpapers — a hardcoded black/white stroke
                        // disappears in one of the two modes.
                        Circle().stroke(Color.primary.opacity(0.4), lineWidth: 0.5)
                    )
                    .offset(x: 2, y: -2)
                    .accessibilityHidden(true)
            }
        }
        .accessibilityLabel(accessibilityDescription)
    }

    private var accessibilityDescription: String {
        switch (isRecording, privacyAttention) {
        case (true, true):   return "ScreenCap, recording — privacy setup needed"
        case (true, false):  return "ScreenCap, recording"
        case (false, true):  return "ScreenCap — privacy setup needed"
        case (false, false): return "ScreenCap"
        }
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
