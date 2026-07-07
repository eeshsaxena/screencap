import SwiftUI

/// Where the shell lands when the wizard exits (U11): Library normally, App
/// rules when the user asked to edit the pre-blocked list (the step-2 deep
/// link).
enum OnboardingFinishDestination: Equatable {
    case library
    case appRules
}

/// U11 — the onboarding takeover (design 50–294). Replaces the main-window
/// content for fresh installs (KTD-10); also re-enterable read-only via the
/// sidebar's "Replay onboarding" (`replay: true` — live grant states, current
/// storage selection, finish writes nothing).
///
/// The current step is derived (`OnboardingStepPolicy`), never stored: a Quit &
/// Relaunch mid-wizard re-derives from {started marker, live daemon grants} and
/// lands on permissions or app rules, not back on welcome. Forward navigation
/// within a session (storage → account → team) is user-driven state.
struct OnboardingWizard: View {
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var permissions: PermissionController
    @EnvironmentObject private var privacy: PrivacyController
    @EnvironmentObject private var auth: CloudAuthController

    let replay: Bool
    let onFinish: (OnboardingFinishDestination) -> Void

    /// Owned here (not by the permissions step) so install progress survives
    /// step navigation within the wizard session.
    @StateObject private var daemonInstaller = DaemonInstallController()

    private let markers = OnboardingMarkerStore()

    @State private var step: OnboardingStep = .welcome
    @State private var tier: OnboardingStorageTier = .local
    /// Step 2's "Edit the list" deep link: finish the wizard, then land on the
    /// App rules pane instead of Library.
    @State private var openAppRulesAfterFinish = false
    /// Collapses double-fires of the finish path (e.g. a sign-in completion
    /// racing a skip tap).
    @State private var finishing = false

    var body: some View {
        VStack(spacing: 0) {
            windowControlsSlot
            stepContent
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            progressDots
        }
        .background(Color.scCanvas)
        .onAppear {
            step = replay
                ? .welcome
                : OnboardingStepPolicy.initialStep(
                    wizardStarted: markers.wizardStarted,
                    requiredGrantsGranted: permissions.daemonGrants.allRequiredGranted
                )
            if replay {
                tier = .from(uploadDefault: privacy.uploadDefault)
            }
            // Live grant detection while the wizard is up — the same slow
            // daemon-grant refresh lifecycle the walkthrough sheet uses (U5),
            // driving the permission rows and the "listening…" footer. Uses the
            // staleness-defeating variant so a grant made after the daemon
            // launched (which the running daemon reports as still-missing) is
            // picked up via a restart rather than stranding the user.
            permissions.startDaemonGrantWatching {
                await recorder.refreshDaemonGrantsDefeatingStaleness()
            }
            Task {
                // Independent CLI reads — run concurrently (each spawns its
                // own subprocess): apps seeds step 2's pre-blocked list,
                // status seeds replay's storage selection.
                async let apps: Void = privacy.refreshApps()
                async let status: Void = privacy.refreshStatus()
                _ = await (apps, status)
            }
        }
        .onDisappear {
            permissions.stopDaemonGrantWatching()
        }
        .onChange(of: permissions.daemonGrants) { grants in
            // Grant detected mid-wizard → auto-advance off the permissions step
            // (the `shouldAutoCloseOnUpdate` analog, KTD-10).
            if OnboardingStepPolicy.shouldAutoAdvance(
                from: step, requiredGrantsGranted: grants.allRequiredGranted
            ) {
                step = .appRules
            }
        }
        .onChange(of: daemonInstaller.state) { state in
            // Helper confirmed running mid-wizard: record the one-time
            // migration explainer as satisfied (mirrors PermissionSetupTakeover)
            // so the migration banner never pops after the wizard.
            if state == .installedAndRunning {
                permissions.markMigrationComplete()
            }
        }
    }

    // MARK: - Steps

    @ViewBuilder
    private var stepContent: some View {
        switch step {
        case .welcome:
            welcomeStep
        case .permissions:
            OnboardingPermissionsStep(
                daemonInstaller: daemonInstaller,
                replay: replay,
                onContinue: { step = .appRules }
            )
        case .appRules:
            OnboardingAppRulesStep(
                apps: privacy.apps,
                isLoading: privacy.isLoading,
                onLooksRight: { step = .storage },
                onEditList: {
                    openAppRulesAfterFinish = true
                    step = .storage
                }
            )
        case .storage:
            OnboardingStorageStep(tier: $tier, onContinue: storageContinue)
        case .account:
            OnboardingAccountStep(
                onSignedIn: accountSignedIn,
                onSkip: complete
            )
        case .teamSetup:
            OnboardingTeamStep(onSkip: complete)
        case .downloadModel:
            // SCR-239 — opt-in downloadable model. Both the download and Skip
            // complete onboarding (never blocks; R7).
            OnboardingDownloadModelStep(onContinue: complete, onSkip: complete)
        }
    }

    // MARK: - Welcome (design 57–85)

    private var welcomeStep: some View {
        VStack(spacing: 0) {
            ShellLogoMark(size: 56)
                .padding(.bottom, 26)
            Text(OnboardingCopy.welcomeHeadline)
                .font(SCTypography.serifDisplay)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 560)
                .padding(.bottom, 14)
            Text(OnboardingCopy.welcomeSub)
                .font(SCTypography.sans(size: 15))
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
                .padding(.bottom, 36)
            HStack(spacing: 12) {
                valueCard(index: 0, dot: .fill(Color.scTeal))
                valueCard(index: 1, dot: .fill(Color.scAmber))
                valueCard(index: 2, dot: .outline)
            }
            .frame(maxWidth: 640)
            .padding(.bottom, 40)
            HStack(spacing: 18) {
                OnboardingPrimaryButton(title: "Set up permissions", action: startWizard)
                Button("Skip for now", action: skip)
                    .buttonStyle(.plain)
                    .font(SCTypography.sans(size: 13.5))
                    .foregroundStyle(Color.scInkMuted)
            }
        }
        .padding(.horizontal, 100)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private enum ValueCardDot {
        case fill(Color)
        case outline
    }

    private func valueCard(index: Int, dot: ValueCardDot) -> some View {
        let card = OnboardingCopy.welcomeCards[index]
        return VStack(alignment: .leading, spacing: 0) {
            Group {
                switch dot {
                case .fill(let color):
                    Circle().fill(color)
                case .outline:
                    Circle().strokeBorder(Color.scInk, lineWidth: 2)
                }
            }
            .frame(width: 10, height: 10)
            .padding(.bottom, 12)
            Text(card.title)
                .font(SCTypography.sans(size: 13.5, weight: .semibold))
                .foregroundStyle(Color.scInk)
                .padding(.bottom, 4)
            Text(card.body)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
    }

    // MARK: - Actions

    private func startWizard() {
        if !replay {
            markers.setWizardStarted(true)
        }
        step = .permissions
    }

    /// "Skip for now" (step 0): persists the dismissal so the takeover stops
    /// re-popping (the recovery latch in the Privacy pane stays available —
    /// SCR-143), and records the one-time migration explainer as seen so the
    /// legacy sheet doesn't pop right after. Recording stays blocked until
    /// permissions land — the start-block is independent of this flag (R4).
    private func skip() {
        if !replay {
            permissions.markSetupDismissed()
            permissions.markMigrationComplete()
            markers.setWizardPending(false)
            markers.setWizardStarted(false)
        }
        onFinish(.library)
    }

    private func storageContinue() {
        if let next = OnboardingStepPolicy.stepAfterStorage(tier: tier) {
            step = next
            return
        }
        // Local pick: persist `upload_default = local` through the CLI seam
        // (KTD-11), then finish. A CLI failure still finishes — the untouched
        // default (`ask`) is the more conservative value and the Privacy pane
        // shows disk truth.
        if replay {
            complete()
            return
        }
        Task {
            await privacy.setUploadDefault("local")
            complete()
        }
    }

    private func accountSignedIn() {
        if let next = OnboardingStepPolicy.stepAfterAccount(tier: tier) {
            step = next
        } else {
            complete()
        }
    }

    /// Terminal path for every finish route. Persists the completion marker
    /// (write-once — the wizard never auto-presents again), releases the
    /// in-progress flags, and records the migration explainer + first-run
    /// privacy disclosure as satisfied (the wizard's own steps covered both).
    /// Replay writes nothing.
    private func complete() {
        guard !finishing else { return }
        finishing = true
        let destination: OnboardingFinishDestination = openAppRulesAfterFinish ? .appRules : .library
        if replay {
            onFinish(destination)
            return
        }
        do {
            try markers.markCompleted()
        } catch {
            // Non-fatal: the takeover decision re-runs next launch and the
            // prior-install evidence (config written by now) backfills.
        }
        markers.setWizardPending(false)
        markers.setWizardStarted(false)
        permissions.markMigrationComplete()
        Task { await privacy.markSetupComplete() }
        onFinish(destination)
    }

    // MARK: - Chrome

    /// The design's mock traffic-light dots are the prototype's fake window
    /// chrome. The window uses `.hiddenTitleBar`, so the REAL controls overlay
    /// this slot while the takeover owns the window content — reserve the
    /// row's height instead of drawing a second set (U14 fidelity pass).
    private var windowControlsSlot: some View {
        Color.clear
            .frame(height: 12)
            .padding(.vertical, 18)
    }

    private var progressDots: some View {
        HStack(spacing: 7) {
            ForEach(0..<OnboardingStepPolicy.dotCount(tier: tier), id: \.self) { index in
                Circle()
                    .fill(
                        index == OnboardingStepPolicy.activeDotIndex(for: step)
                            ? Color.scInk : Color.scBorderWarm
                    )
                    .frame(width: 7, height: 7)
            }
        }
        .padding(.top, 18)
        .padding(.bottom, 24)
        .accessibilityLabel(
            "Step \(OnboardingStepPolicy.activeDotIndex(for: step) + 1) of \(OnboardingStepPolicy.dotCount(tier: tier))"
        )
    }
}

/// The wizard's teal primary pill (design's `#0E7C6B` CTA).
struct OnboardingPrimaryButton: View {
    let title: String
    var enabled: Bool = true
    /// Optional key binding (the migration banner's Continue is `.defaultAction`).
    var shortcut: KeyboardShortcut?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(SCTypography.sans(size: 14.5, weight: .semibold))
                .foregroundStyle(Color.scCanvas)
                .padding(.horizontal, 26)
                .padding(.vertical, 12)
                .background(Color.scTeal, in: Capsule())
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .keyboardShortcut(shortcut)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.5)
    }
}

/// The wizard's underlined secondary link (design's text links).
struct OnboardingLinkButton: View {
    let title: String
    var enabled: Bool = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(SCTypography.sans(size: 13))
                .underline()
                .foregroundStyle(Color.scInkSecondary)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.5)
    }
}
