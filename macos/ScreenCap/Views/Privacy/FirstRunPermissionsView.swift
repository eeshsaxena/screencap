import SwiftUI

enum PermissionSheetRelaunchFlow {
    static let sheetDismissalDelayNanoseconds: UInt64 = 350_000_000

    @MainActor
    static func dismissThenRelaunch(
        dismiss: () -> Void,
        relaunch: () -> Void,
        sleep: (UInt64) async -> Void = { nanoseconds in
            try? await Task.sleep(nanoseconds: nanoseconds)
        }
    ) async {
        dismiss()
        await sleep(sheetDismissalDelayNanoseconds)
        relaunch()
    }
}

/// Loom-style first-run helper walkthrough. The rows open the relevant Privacy
/// & Security panes for the helper-owned permissions, but do not poll the
/// ScreenCap app process's TCC state.
///
/// Dismissible once the helper is installed. Daemon-backed recording reports
/// helper-side permission failures at start time.
struct FirstRunPermissionsView: View {
    @EnvironmentObject private var permissions: PermissionController
    @EnvironmentObject private var recorder: RecorderController
    @Binding var isPresented: Bool
    @StateObject private var daemonInstaller = DaemonInstallController()
    @State private var isPreparingRelaunch = false
    @State private var isDaemonInstallComplete = false
    @State private var openedDaemonPanes: Set<PrivacyPane> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Set up ScreenCap")
                    .font(.title.bold())
                Text("Set up the ScreenCap helper, then grant the permissions it needs to record.")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }

            if !isDaemonInstallComplete {
                daemonInstallStep
            } else {
                VStack(spacing: 12) {
                    daemonPermissionRow(pane: .screenRecording)
                    daemonPermissionRow(pane: .accessibility)
                    daemonPermissionRow(pane: .inputMonitoring)
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("After enabling ScreenCap in System Settings, return here to continue. If macOS shows a separate ScreenCap helper entry, enable that entry too.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                HStack {
                    // "Quit & Relaunch" only helps the *app process's* TCC cache
                    // (CLI-fallback path). On the daemon path the daemon is the
                    // TCC subject and needs no app restart, so don't present the
                    // relaunch as a required step there (U5). Full removal /
                    // relabel is a deferred follow-up.
                    if recorder.transport == .cliFallback {
                        Button("Quit & Relaunch") {
                            isPreparingRelaunch = true
                            Task { @MainActor in
                                await PermissionSheetRelaunchFlow.dismissThenRelaunch(
                                    dismiss: { isPresented = false },
                                    relaunch: { permissions.relaunchApplication() }
                                )
                            }
                        }
                        .buttonStyle(.bordered)
                        .disabled(isPreparingRelaunch || permissions.isRelaunching || recorder.state.isRecording)
                    }

                    // Always-available escape hatch. Persists the dismissal so
                    // the state-driven gate stops re-popping (U4). The start-block
                    // stays independent, so a skipped sheet can't let a broken
                    // daemon recording start silently.
                    Button("Skip for now") {
                        permissions.markSetupDismissed()
                        isPresented = false
                    }
                    .buttonStyle(.bordered)

                    Spacer()

                    // Enabled once the daemon reports all three required grants
                    // granted (the happy-path exit). A partial/indeterminate
                    // state exits via "Skip for now" instead.
                    Button("Done") { isPresented = false }
                        .keyboardShortcut(.defaultAction)
                        .disabled(!permissions.allRequiredDaemonGrantsGranted)
                }
            }
        }
        .padding(28)
        .frame(width: 520)
        .onChange(of: daemonInstaller.state) { state in
            if state == .installedAndRunning {
                isDaemonInstallComplete = true
            }
        }
        .onAppear {
            // Refresh the daemon's grant snapshot while the sheet is visible so
            // the rows reflect grants the user toggles in System Settings —
            // re-activation + a slow 5s timer (not the 1Hz app-process poll).
            permissions.startDaemonGrantWatching {
                await recorder.refreshDaemonGrants()
            }
        }
        .onDisappear {
            permissions.stopDaemonGrantWatching()
        }
    }

    @ViewBuilder
    private var daemonInstallStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: daemonInstallIconName)
                    .font(.system(size: 22))
                    .foregroundStyle(daemonInstallIconColor)
                    .frame(width: 24)

                VStack(alignment: .leading, spacing: 6) {
                    Text("Approve ScreenCap helper")
                        .font(.headline)
                    Text(daemonInstallStatusText)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }

                Spacer()
            }

            HStack {
                switch daemonInstaller.state {
                case .idle, .installFailed, .pollingFailed:
                    Button("Approve helper") {
                        Task { await daemonInstaller.install() }
                    }
                    .buttonStyle(.borderedProminent)
                case .requiresApproval:
                    Button("Open Login Items") {
                        DaemonInstallController.openLoginItemsSettings()
                    }
                    .buttonStyle(.borderedProminent)
                    Button("Retry") {
                        Task { await daemonInstaller.retry() }
                    }
                    .buttonStyle(.bordered)
                case .registering, .polling:
                    ProgressView()
                        .controlSize(.small)
                case .installedAndRunning:
                    Text("Helper running")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
    }

    private var daemonInstallIconName: String {
        switch daemonInstaller.state {
        case .installedAndRunning:
            return "checkmark.circle.fill"
        case .installFailed, .pollingFailed:
            return "exclamationmark.triangle.fill"
        case .registering, .polling, .requiresApproval:
            return "clock.fill"
        case .idle:
            return "gearshape.fill"
        }
    }

    private var daemonInstallIconColor: Color {
        switch daemonInstaller.state {
        case .installedAndRunning:
            return .green
        case .installFailed, .pollingFailed:
            return .red
        case .registering, .polling, .requiresApproval:
            return .orange
        case .idle:
            return .secondary
        }
    }

    private var daemonInstallStatusText: String {
        switch daemonInstaller.state {
        case .idle:
            return "ScreenCap uses a background helper for recording. Approve it in System Settings when prompted."
        case .registering:
            return "Starting helper..."
        case .requiresApproval:
            return "Approve ScreenCap helper in System Settings -> Login Items."
        case .polling:
            return "Waiting for helper to start..."
        case .installedAndRunning:
            return "ScreenCap helper is running."
        case .pollingFailed(let reason):
            return reason
        case .installFailed(let reason):
            return installFailureCopy(reason)
        }
    }

    private func installFailureCopy(_ reason: DaemonInstallController.InstallFailureReason) -> String {
        switch reason {
        case .plistWriteFailed:
            return "The helper plist was not found in the app bundle."
        case .launchctlBootstrapFailed:
            return "macOS did not start the helper. Retry after approving Login Items."
        case .daemonDidNotStart:
            return "The helper did not respond after launch."
        case .daemonSigningInvalid:
            return "macOS rejected the helper signature."
        case .diskFull:
            return "The disk is full, so the helper could not be installed."
        case .unknown:
            return "The helper could not be installed."
        }
    }

    @ViewBuilder
    private func daemonPermissionRow(pane: PrivacyPane) -> some View {
        // The icon now reflects the daemon's *real* reported grant state (U3/U5)
        // rather than the old "did the user click Open Settings" proxy. The
        // tri-state is honest: granted (green), denied (needs-action orange),
        // and indeterminate ("couldn't verify" — a muted icon with a distinct
        // accessibility label, never a green check or red needs-action).
        let state = permissions.daemonGrant(for: pane)
        let icon = Self.grantRowIcon(for: state)
        let opened = openedDaemonPanes.contains(pane)
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon.systemName)
                .font(.system(size: 20))
                .foregroundStyle(icon.color)
                .accessibilityLabel(icon.accessibilityLabel)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("\(pane.displayName) for ScreenCap helper")
                        .font(.headline)
                }
                Text(daemonRationale(for: pane))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            if state == .granted {
                Text("Granted")
                    .font(.subheadline)
                    .foregroundStyle(.green)
            } else if permissions.isDaemonRegistering(pane) {
                // U8: a daemon registration round-trip is in flight for this
                // pane. Mirror the helper-install step's spinner so the user
                // sees the Grant action is working and a repeat tap is a no-op.
                ProgressView()
                    .controlSize(.small)
            } else {
                Button(opened ? "Open Again" : "Grant") {
                    openedDaemonPanes.insert(pane)
                    permissions.requestAndOpenSettings(for: pane, subject: .daemon)
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
    }

    /// Map the tri-state daemon grant to a row icon. Indeterminate is "couldn't
    /// verify" — a muted dashed circle with its own accessibility label, never
    /// confused with granted (check) or denied (needs-action).
    static func grantRowIcon(
        for state: DaemonGrantState
    ) -> (systemName: String, color: Color, accessibilityLabel: String) {
        switch state {
        case .granted:
            return ("checkmark.circle.fill", .green, "Granted")
        case .denied:
            return ("exclamationmark.circle.fill", .orange, "Needs action")
        case .indeterminate:
            return ("circle.dashed", .secondary, "Couldn't verify")
        }
    }

    private func daemonRationale(for pane: PrivacyPane) -> String {
        "\(pane.rationale) Enable the ScreenCap helper entry in this pane."
    }
}
