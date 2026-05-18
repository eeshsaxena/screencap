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
            Image(systemName: recorder.state.isRecording ? "record.circle.fill" : "record.circle")
                .symbolRenderingMode(.palette)
                .foregroundStyle(recorder.state.isRecording ? Color.red : Color.primary)
        }
        .menuBarExtraStyle(.menu)
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
