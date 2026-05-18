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
                .frame(minWidth: 880, minHeight: 560)
                .background(OpenWindowBridge())
                .onAppear {
                    appDelegate.bind(recorder: recorder)
                    recorder.bindIndex(index)
                    recorder.bindPermissions(permissions)
                    permissions.refresh()
                }
                .task {
                    if ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil {
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

        MenuBarExtra {
            MenuBarMenu()
                .environmentObject(recorder)
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
private struct OpenWindowBridge: View {
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .onAppear {
                WindowOpener.shared.openMain = { openWindow(id: MainWindowID) }
            }
    }
}
