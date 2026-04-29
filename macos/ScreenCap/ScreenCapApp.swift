import SwiftUI

@main
struct ScreenCapApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    @StateObject private var recorder = RecorderController()
    @StateObject private var permissions = PermissionController()

    var body: some Scene {
        WindowGroup("ScreenCap") {
            MainWindow()
                .environmentObject(recorder)
                .environmentObject(permissions)
                .frame(minWidth: 720, minHeight: 480)
                .onAppear { appDelegate.bind(recorder: recorder) }
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
