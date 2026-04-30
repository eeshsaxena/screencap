import SwiftUI

/// Loom-style first-run permissions walkthrough. Three rows (Screen Recording,
/// Accessibility, Microphone) with green/red indicators and "Open System Settings"
/// deep links. Live polling (1Hz + workspace-activation) is owned by the
/// `PermissionController` injected via the environment.
///
/// Dismissible only when both required permissions are granted; microphone is
/// independently togglable. Per DL-004 the sheet sits over `MainWindow` until
/// dismissed.
struct FirstRunPermissionsView: View {
    @EnvironmentObject private var permissions: PermissionController
    @Binding var isPresented: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Set up ScreenCap")
                    .font(.title.bold())
                Text("Grant the permissions below so ScreenCap can record your screen with privacy built in.")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }

            VStack(spacing: 12) {
                permissionRow(
                    pane: .screenRecording,
                    status: permissions.screenRecording
                )
                permissionRow(
                    pane: .accessibility,
                    status: permissions.accessibility
                )
                permissionRow(
                    pane: .inputMonitoring,
                    status: permissions.inputMonitoring
                )
                permissionRow(
                    pane: .microphone,
                    status: permissions.microphone
                )
            }

            HStack {
                Spacer()
                Button("Done") { isPresented = false }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!permissions.allRequiredGranted)
            }
        }
        .padding(28)
        .frame(width: 520)
        .onAppear { permissions.startWatching() }
    }

    @ViewBuilder
    private func permissionRow(pane: PrivacyPane, status: PermissionStatus) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: status.isGranted ? "checkmark.circle.fill" : "xmark.circle.fill")
                .font(.system(size: 20))
                .foregroundStyle(status.isGranted ? Color.green : Color.red)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(pane.displayName)
                        .font(.headline)
                    if !pane.isRequired {
                        Text("Optional")
                            .font(.caption)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(.secondary.opacity(0.15), in: Capsule())
                    }
                }
                Text(pane.rationale)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            if !status.isGranted {
                Button("Open System Settings") {
                    permissions.openSystemSettings(for: pane)
                }
                .buttonStyle(.borderedProminent)
            } else {
                Text("Granted")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
    }
}
