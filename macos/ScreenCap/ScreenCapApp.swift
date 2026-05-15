import SwiftUI

@main
struct ScreenCapApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    @StateObject private var recorder = RecorderController()
    @StateObject private var permissions = PermissionController()
    @StateObject private var index = RecordingsIndex()

    var body: some Scene {
        WindowGroup("ScreenCap") {
            MainWindow()
                .environmentObject(recorder)
                .environmentObject(permissions)
                .environmentObject(index)
                .frame(minWidth: 880, minHeight: 560)
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
