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
                    daemonPermissionRow(
                        pane: .screenRecording,
                        status: permissions.screenRecording
                    )
                    daemonPermissionRow(
                        pane: .accessibility,
                        status: permissions.accessibility
                    )
                    daemonPermissionRow(
                        pane: .inputMonitoring,
                        status: permissions.inputMonitoring
                    )
                }
            }

            // macOS caches TCC state per-process — once you grant a
            // permission in System Settings, this app doesn't see the change
            // until it relaunches. Standard Mac-app pattern (Loom, 1Password,
            // …) is an explicit Quit & Relaunch.
            VStack(alignment: .leading, spacing: 8) {
                Text("After granting permissions in System Settings, quit and relaunch ScreenCap to apply.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                HStack {
                    // Disable while a relaunch is already in flight (prevents
                    // double-click stacking new instances) or while a
                    // recording is active (avoids racing PR3's `.terminateLater`
                    // NSAlert path against the detached `open -n` shell).
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

                    // Always-available escape hatch. Dismisses the sheet
                    // even if the cached permission state still reads denied.
                    // Recording itself will still be gated by the actual TCC
                    // state at start time — this just unblocks navigation.
                    Button("Skip for now") { isPresented = false }
                        .buttonStyle(.bordered)

                    Spacer()

                    Button("Done") { isPresented = false }
                        .keyboardShortcut(.defaultAction)
                        .disabled(!isDaemonInstallComplete || !permissions.allRequiredGranted)
                }
            }
        }
        .padding(28)
        .frame(width: 520)
        .onAppear { permissions.startWatching() }
        .onDisappear { permissions.stopWatching() }
        .onChange(of: daemonInstaller.state) { state in
            if state == .installedAndRunning {
                isDaemonInstallComplete = true
            }
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
    private func daemonPermissionRow(pane: PrivacyPane, status: PermissionStatus) -> some View {
        // TCC does not expose a programmatic status check for arbitrary
        // binaries (the daemon's `com.screencap.daemon` subject). The only
        // local signal we have is whether the user clicked Open Settings,
        // which doesn't actually confirm a grant. Use neutral icons that
        // don't claim a state we can't verify — gray/blue, not red/green.
        let opened = openedDaemonPanes.contains(pane)
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: opened ? "circle.inset.filled" : "circle")
                .font(.system(size: 20))
                .foregroundStyle(opened ? Color.accentColor : Color.secondary)
                .accessibilityLabel(opened ? "Settings visited" : "Settings not yet visited")
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("\(pane.displayName) for ScreenCap helper")
                        .font(.headline)
                }
                Text(daemonRationale(for: pane, appStatus: status))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Button(opened ? "Open Again" : "Open Settings") {
                openedDaemonPanes.insert(pane)
                permissions.requestAndOpenSettings(for: pane, subject: .daemon)
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
    }

    private func daemonRationale(for pane: PrivacyPane, appStatus: PermissionStatus) -> String {
        let statusText = appStatus.isGranted ? "The app entry is already granted; enable the helper entry too." : "Enable the ScreenCap helper entry in this pane."
        return "\(pane.rationale) \(statusText)"
    }
}
