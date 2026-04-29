import SwiftUI

/// Top-level window content. Unit 9 ships a placeholder; Unit 11 swaps in the
/// calendar view, Unit 12 adds the recordings list, and Unit 13 layers in the
/// recording banner.
struct MainWindow: View {
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var permissions: PermissionController

    @State private var showingPermissionsSheet = false
    @State private var smokeStatus: String = "—"

    var body: some View {
        VStack(spacing: 16) {
            HStack {
                Image(systemName: "record.circle")
                    .font(.system(size: 32))
                    .foregroundStyle(.secondary)
                VStack(alignment: .leading) {
                    Text("ScreenCap")
                        .font(.title2.bold())
                    Text("Native macOS UI — Unit 9 scaffolding")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }

            Divider()

            VStack(alignment: .leading, spacing: 8) {
                Text("CLI smoke test")
                    .font(.headline)
                Text(smokeStatus)
                    .font(.system(.body, design: .monospaced))
                    .foregroundStyle(.secondary)
                Button("Run `screencap status --json`") {
                    Task {
                        if let status = await recorder.smokeStatus() {
                            smokeStatus = "is_recording=\(status.isRecording), schema=\(status.schemaVersion)"
                        } else {
                            smokeStatus = "(failed — see Console)"
                        }
                    }
                }
            }

            Divider()

            VStack(alignment: .leading, spacing: 8) {
                Text("Permissions")
                    .font(.headline)
                permissionRow("Screen Recording", status: permissions.screenRecording)
                permissionRow("Accessibility",    status: permissions.accessibility)
                permissionRow("Microphone",       status: permissions.microphone)
                Button("Open permissions walkthrough") {
                    showingPermissionsSheet = true
                }
            }

            Spacer()
        }
        .padding(24)
        .sheet(isPresented: $showingPermissionsSheet) {
            FirstRunPermissionsView(isPresented: $showingPermissionsSheet)
                .environmentObject(permissions)
        }
        .onAppear {
            permissions.startWatching()
            if !permissions.allRequiredGranted {
                showingPermissionsSheet = true
            }
        }
        .onDisappear {
            permissions.stopWatching()
        }
    }

    @ViewBuilder
    private func permissionRow(_ name: String, status: PermissionStatus) -> some View {
        HStack {
            Image(systemName: status.isGranted ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(status.isGranted ? .green : .red)
            Text(name)
            Spacer()
            Text(status.isGranted ? "Granted" : "Not granted")
                .foregroundStyle(.secondary)
                .font(.caption)
        }
    }
}
