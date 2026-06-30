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
    /// When the user first opened each pane (= reached the toggle step). Drives
    /// U6's grant-state-timeout heuristic: the block-with-Retry state can only
    /// fire after the user has had a real chance to toggle (R7).
    @State private var openedDaemonPaneAt: [PrivacyPane: Date] = [:]

    /// Phase 1c (SCR-49): true once the user taps Continue on the one-time
    /// migration banner this session. Combined with `permissions.migrationNeeded`
    /// (the persisted marker) it gates whether the banner or the walkthrough
    /// shows — reading `migrationNeeded` directly in the body avoids a flash of
    /// the walkthrough before the banner on upgrade launches.
    @State private var migrationStepAcknowledged = false

    var body: some View {
        Group {
            if permissions.migrationNeeded && !migrationStepAcknowledged {
                DaemonMigrationView(onContinue: { migrationStepAcknowledged = true })
            } else {
                walkthroughContent
            }
        }
        .onChange(of: daemonInstaller.state) { state in
            if state == .installedAndRunning {
                isDaemonInstallComplete = true
                // Phase 1c: write the migration marker as soon as the helper is
                // confirmed running (idempotent). The sheet's `onDismiss`
                // (MainWindow) is the primary writer and covers every dismiss
                // path; writing it here too clears the presentation override the
                // moment migration genuinely completes, without waiting for the
                // sheet to close.
                permissions.markMigrationComplete()
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

    private var walkthroughContent: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Set up ScreenCap")
                    .font(.title.bold())
                Text("Set up the ScreenCap helper, then grant the permissions it needs to record.")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }

            if permissions.showAdHocDevBuildWarning {
                adHocDevBuildCallout
            }

            if !isDaemonInstallComplete {
                daemonInstallStep
            } else {
                // Only the two required, grantable permissions are shown. Input
                // Monitoring is intentionally omitted: on macOS 26.x the helper
                // cannot register a toggleable IM row by any known mechanism, so
                // a row here would be a dead end (a Grant button opening a pane
                // with no helper entry). IM is advisory, not capture-fatal, so
                // recording works without it. See DaemonPermissionGrants.
                VStack(spacing: 12) {
                    daemonPermissionRow(pane: .screenRecording)
                    daemonPermissionRow(pane: .accessibility)
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("After enabling the ScreenCap entry in System Settings, return here to continue.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                HStack {
                    // The relaunch only helps the *app process's* TCC cache
                    // (CLI-fallback path): a grant made post-launch isn't visible
                    // until a fresh process re-reads TCC. On the daemon path the
                    // daemon is the TCC subject and needs no app restart, so the
                    // affordance is hidden there (U5). The label is purpose-first
                    // ("Restart to apply permissions") rather than "Quit &
                    // Relaunch" so it reads as the fallback-only refresh it is,
                    // not a required onboarding step (SCR-120 item 1 relabel).
                    if recorder.transport == .cliFallback {
                        Button("Restart to apply permissions") {
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
    }

    /// Developer-only hint: on an ad-hoc build, TCC grants are orphaned on every
    /// rebuild, so the rows can read "needs action" even though System Settings
    /// shows an earlier build as granted. Explains the cause and the fix so a
    /// developer doesn't chase a phantom permission bug. Never renders on a
    /// signed build (see `PermissionController.showAdHocDevBuildWarning`).
    @ViewBuilder
    private var adHocDevBuildCallout: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "hammer.fill")
                .font(SCTypography.labelPrimary)
                // Developer advisory — de-colored to neutral (R7), not orange.
                .foregroundStyle(Color.scAdvisoryFg)
                .padding(.top, 2)
                // Decorative — the adjacent warning text conveys the full
                // meaning, so keep VoiceOver from announcing the symbol as a
                // separate, content-free focus stop (mirrors finishSetupBanner).
                .accessibilityHidden(true)
            Text(PermissionController.adHocDevBuildWarning)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .fill(Color.scAdvisorySurface)
        )
    }

    @ViewBuilder
    private var daemonInstallStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: daemonInstallIconName)
                    .font(SCTypography.title)
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
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .fill(Color.scSurfaceElevated)
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
            return .scSuccessFg
        case .installFailed, .pollingFailed:
            return .scErrorFg
        case .registering, .polling, .requiresApproval:
            // In-progress = advisory, de-colored to neutral (R7).
            return .scAdvisoryFg
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
        case .daemonVersionMismatch:
            return "A different version of the ScreenCap helper is already running. Click Approve helper to reinstall the bundled version."
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
                .font(SCTypography.title)
                .foregroundStyle(icon.color)
                .accessibilityLabel(icon.accessibilityLabel)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    // R4: show the "ScreenCap" mark the user will match against the
                    // System Settings row, with an SF Symbol fallback so an absent
                    // app-icon image never leaves a blank frame.
                    switch Self.helperRowIcon(appIcon: Self.helperRowAppIcon()) {
                    case .image(let nsImage):
                        Image(nsImage: nsImage)
                            .resizable()
                            .frame(width: 18, height: 18)
                            .accessibilityLabel("ScreenCap icon")
                    case .symbol(let name):
                        Image(systemName: name)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("ScreenCap icon")
                    }
                    Text("\(pane.displayName) for ScreenCap")
                        .font(.headline)
                }
                Text(daemonRationale(for: pane))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            // U6 (R7): resolve the row's trailing control from the registration-
            // outcome + grant-state-timeout heuristic. The block-with-Retry state
            // only fires once the user has opened the pane (reached the toggle
            // step) and a budget has elapsed without the grant resolving.
            let elapsed = openedDaemonPaneAt[pane].map { Date().timeIntervalSince($0) }
            let rowState = Self.daemonRowState(
                grant: state,
                isRegistering: permissions.isDaemonRegistering(pane),
                elapsedSinceOpened: elapsed,
                budget: Self.daemonRowBlockBudget
            )
            switch rowState {
            case .granted:
                Text("Granted")
                    .font(.subheadline)
                    .foregroundStyle(Color.scSuccessFg)
            case .registering:
                // U8: a daemon registration round-trip is in flight for this
                // pane. Mirror the helper-install step's spinner so the user
                // sees the Grant action is working and a repeat tap is a no-op.
                ProgressView()
                    .controlSize(.small)
            case .blockedWithRetry:
                // R7: the row never became grantable within the budget — block
                // with a clear state + Retry that re-fires registration. Never a
                // manual "+" add, never a silent advance.
                VStack(alignment: .trailing, spacing: 4) {
                    Text("Couldn't set this up")
                        .font(.caption)
                        .foregroundStyle(Color.scAdvisoryFg)
                    Button("Retry") {
                        openedDaemonPaneAt[pane] = Date()  // restart the budget
                        permissions.requestAndOpenSettings(for: pane, subject: .daemon)
                    }
                    .buttonStyle(.borderedProminent)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(pane.displayName) couldn't be set up. Retry.")
            case .actionable:
                Button(opened ? "Open Again" : "Grant") {
                    openedDaemonPanes.insert(pane)
                    if openedDaemonPaneAt[pane] == nil {
                        openedDaemonPaneAt[pane] = Date()
                    }
                    permissions.requestAndOpenSettings(for: pane, subject: .daemon)
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .fill(Color.scSurfaceElevated)
        )
    }

    /// Map the tri-state daemon grant to a row icon. Indeterminate is "couldn't
    /// verify" — a muted dashed circle with its own accessibility label, never
    /// confused with granted (check) or denied (needs-action). Color follows the
    /// state roles (R7): granted = success green; denied = de-colored advisory,
    /// the `exclamationmark` *shape* carrying the "needs action" meaning rather
    /// than a warning hue; indeterminate stays muted secondary.
    static func grantRowIcon(
        for state: DaemonGrantState
    ) -> (systemName: String, color: Color, accessibilityLabel: String) {
        switch state {
        case .granted:
            return ("checkmark.circle.fill", .scSuccessFg, "Granted")
        case .denied:
            return ("exclamationmark.circle.fill", .scAdvisoryFg, "Needs action")
        case .indeterminate:
            return ("circle.dashed", .secondary, "Couldn't verify")
        }
    }

    /// The leading "ScreenCap" mark for a permission row, resolved to either the
    /// app/helper icon image or an SF Symbol fallback (R4). Pure so the fallback
    /// path — the one that guarantees the row is never blank — is unit-testable.
    enum HelperRowIcon {
        case image(NSImage)
        case symbol(String)
    }

    /// SF Symbol shown when the app-icon image can't be resolved. Never blank.
    static let helperRowFallbackSymbol = "app.dashed"

    static func helperRowIcon(appIcon: NSImage?) -> HelperRowIcon {
        if let appIcon {
            return .image(appIcon)
        }
        return .symbol(helperRowFallbackSymbol)
    }

    /// The app's own icon, which the helper shares as its mark (same "ScreenCap"
    /// glyph the System Settings row shows). `nil` if it can't be loaded, driving
    /// the SF Symbol fallback.
    static func helperRowAppIcon() -> NSImage? {
        NSImage(named: NSImage.applicationIconName)
    }

    /// The trailing control state for a daemon permission row (SCR-200 U6 / R7).
    enum DaemonRowState: Equatable {
        case granted
        case registering       // a daemon round-trip is in flight
        case actionable        // normal Grant / Open Again — not (yet) blocked
        case blockedWithRetry  // budget exhausted with the row still not granted
    }

    /// Budget after the user opens a pane before a still-denied row is treated as
    /// "couldn't set this up" (R7). Generous so a slow-but-normal toggle never
    /// false-blocks; the exact value is on-device-tuned (U7 leg F).
    static let daemonRowBlockBudget: TimeInterval = 25

    /// Resolve a daemon row's trailing control from the registration-outcome +
    /// grant-state-timeout heuristic (R7). There is **no** public, non-SIP API for
    /// TCC row *presence* — an absent row and a present-but-OFF row both read
    /// `denied`, and TCC.db is SIP-protected — so "the row never appeared" is
    /// *inferred*, not read: the pane was opened (registration fired) AND the
    /// grant has not resolved to `granted` within `budget` after the user reached
    /// the toggle step. The block is gated on `elapsedSinceOpened` so it never
    /// fires before the user has had a real chance to toggle (no immediate
    /// post-install false-block). `indeterminate` ("couldn't verify") keeps Retry
    /// available rather than hard-blocking, so a transient probe hiccup can't
    /// false-block a user who is actually granted.
    static func daemonRowState(
        grant: DaemonGrantState,
        isRegistering: Bool,
        elapsedSinceOpened: TimeInterval?,
        budget: TimeInterval
    ) -> DaemonRowState {
        if grant == .granted { return .granted }
        if isRegistering { return .registering }
        // Not yet opened → the user hasn't reached the toggle step; never block.
        guard let elapsed = elapsedSinceOpened else { return .actionable }
        // Couldn't verify → keep Retry available, don't hard-block.
        if grant == .indeterminate { return .actionable }
        // Opened + still denied past the budget → the row didn't take.
        if elapsed >= budget { return .blockedWithRetry }
        return .actionable
    }

    private func daemonRationale(for pane: PrivacyPane) -> String {
        // SCR-200: the daemon's row reads "ScreenCap" and is the only "ScreenCap"
        // row in this pane (the app never appears here, R6), so naming it is
        // unambiguous — no "helper vs app" disambiguation is needed. Spell out the
        // exact row to enable so the user toggles the right one.
        let entry = pane.helperSettingsEntryName
        return "\(pane.rationale) Enable the “\(entry)” entry in this pane."
    }
}
