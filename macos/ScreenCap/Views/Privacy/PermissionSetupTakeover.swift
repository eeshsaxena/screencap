import SwiftUI

/// U14 — the permission-repair takeover: the onboarding permissions screen
/// (design 87–151, `OnboardingPermissionsStep`) presented as the main
/// window's content whenever permission setup is needed after onboarding —
/// the launch gate (missing daemon grants / CLI fallback) and the Privacy
/// pane's "Finish setup" recovery. Replaces the retired modal walkthrough
/// sheet so the app has exactly one permission surface.
///
/// The sheet's behavioral contract carries over verbatim:
/// - Upgrade users see the one-time migration interstitial first
///   (`DaemonMigrationView`), before the permissions screen.
/// - The daemon grant snapshot refreshes while visible (re-activation + slow
///   timer), so the rows react to toggles made in System Settings.
/// - The helper confirming mid-flow writes the migration marker immediately
///   (idempotent), clearing the presentation override without waiting for a
///   dismiss.
/// - Continue exits; when required grants are still missing that persists the
///   dismissal (the old sheet's "Skip for now"), so the state-derived launch
///   gate stops re-presenting. The start-block stays independent (R4), so a
///   skipped setup can never let a broken recording start silently.
struct PermissionSetupTakeover: View {
    @EnvironmentObject private var permissions: PermissionController
    @EnvironmentObject private var recorder: RecorderController
    @StateObject private var daemonInstaller = DaemonInstallController()
    /// MainWindow clears the takeover state (and runs the shared dismiss
    /// bookkeeping — migration marker, recovery-latch reset).
    let onClose: () -> Void

    /// True once the user taps Continue on the one-time migration banner this
    /// session (Phase 1c, SCR-49). Combined with `permissions.migrationNeeded`
    /// (the persisted marker) it gates whether the banner or the permissions
    /// screen shows — reading `migrationNeeded` directly avoids a flash of the
    /// permissions screen before the banner on upgrade launches.
    @State private var migrationStepAcknowledged = false

    var body: some View {
        VStack(spacing: 0) {
            windowControlsSlot
            if permissions.migrationNeeded && !migrationStepAcknowledged {
                DaemonMigrationView(onContinue: { migrationStepAcknowledged = true })
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                OnboardingPermissionsStep(
                    daemonInstaller: daemonInstaller,
                    onContinue: close
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color.scCanvas)
        .onChange(of: daemonInstaller.state) { state in
            // Phase 1c: write the migration marker as soon as the helper is
            // confirmed running (idempotent) — the close path is the primary
            // writer; writing here too clears the presentation override the
            // moment migration genuinely completes.
            if state == .installedAndRunning {
                permissions.markMigrationComplete()
            }
        }
        .onAppear {
            // Refresh the daemon's grant snapshot while visible so the rows
            // reflect grants the user toggles in System Settings —
            // re-activation + a slow 5s timer (not the 1Hz app-process poll).
            permissions.startDaemonGrantWatching {
                await recorder.refreshDaemonGrants()
            }
        }
        .onDisappear {
            permissions.stopDaemonGrantWatching()
        }
    }

    private func close() {
        // Exiting with required grants still missing = the retired sheet's
        // "Skip for now": persist the dismissal so the state-derived launch
        // gate doesn't re-present on the next update (U4).
        if !permissions.allRequiredDaemonGrantsGranted {
            permissions.markSetupDismissed()
        }
        onClose()
    }

    /// Reserve the real traffic lights' overlay slot (`.hiddenTitleBar`),
    /// mirroring the onboarding wizard's slot.
    private var windowControlsSlot: some View {
        Color.clear
            .frame(height: 12)
            .padding(.vertical, 18)
    }
}
