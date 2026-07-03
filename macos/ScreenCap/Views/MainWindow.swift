import Combine
import SwiftUI

enum FirstRunSetupPresentationPolicy {
    /// Decide whether to present the first-run permission walkthrough (R3, U4).
    ///
    /// This now keys on *daemon-reported* grant state, not on the daemon being
    /// unreachable. It **supersedes SCR-54's "do not pre-block on the daemon
    /// transport"** — but legitimately so: SCR-54 removed pre-blocking on the
    /// *app process's* (irrelevant) TCC state; this gate keys on the *daemon's*
    /// (the TCC subject's) state.
    ///
    /// - `.cliFallback` (daemon unreachable): preserve the existing CLI-path
    ///   walkthrough — the recording would run under the app's own TCC identity.
    /// - `.daemon` (reachable): present only when the daemon reports a missing
    ///   required grant. Indeterminate / absent-block is never "missing" (the
    ///   engine preflight is the backstop), so it never presents.
    ///
    /// A persisted `setupDismissed` ("Skip for now") suppresses the gate so it
    /// stops re-popping every launch. The start-block (R4) is independent of
    /// `setupDismissed`, so a dismissed sheet never lets a broken recording start.
    ///
    /// U11: `onboardingTakeoverActive` suppresses the sheet outright while the
    /// onboarding wizard owns the window — the wizard embeds the same
    /// install + grant machinery, so popping the sheet over it would run two
    /// copies of the walkthrough at once. On a fresh install the migration
    /// marker is also absent, so without this guard the migration override
    /// would force the sheet over the wizard's welcome step.
    static func shouldPresentOnLaunch(
        daemonProbeCompleted: Bool,
        transport: RecorderTransport,
        daemonGrants: DaemonPermissionGrants,
        setupDismissed: Bool,
        migrationNeeded: Bool,
        onboardingTakeoverActive: Bool = false
    ) -> Bool {
        guard !onboardingTakeoverActive else { return false }
        guard daemonProbeCompleted else { return false }
        // Phase 1c (SCR-49): the one-time upgrade migration banner shows even when
        // the daemon already reports grants, and even if the walkthrough was
        // previously skipped — it is the upgrade explanation, shown once until
        // migration completes (marker written). It therefore overrides
        // `setupDismissed`. The caller's `recorder.state.isRecording` guard still
        // suppresses the sheet over an active capture (R3).
        if migrationNeeded { return true }
        guard !setupDismissed else { return false }
        switch transport {
        case .cliFallback:
            return true
        case .daemon:
            return daemonGrants.anyRequiredDenied
        }
    }

    /// Decide whether an in-flight grant/transport/probe update should auto-close
    /// the walkthrough sheet — the inverse of the launch gate, re-evaluated on
    /// every `daemonGrants` / `transport` / `daemonProbeCompleted` change while
    /// the sheet is up.
    ///
    /// Closes once the daemon path is satisfied: reachable (`transport == .daemon`)
    /// and NOT reporting a required denial (all granted, or indeterminate). Covers
    /// the cold-boot cliFallback→daemon bounce and the post-grant refresh (U5). A
    /// reachable daemon still reporting a denial keeps the walkthrough up.
    ///
    /// `reopenedViaRecovery` suppresses the close for the lifetime of a sheet
    /// opened by the explicit "Finish setup" recovery latch (SCR-144). Without it,
    /// the sheet's immediate on-open daemon-grant refresh (or a coincident
    /// transport flip) re-enters this gate and dismisses the sheet the user just
    /// reopened, before they can act. The launch path leaves the flag `false`, so
    /// its documented auto-close behavior is unchanged.
    static func shouldAutoCloseOnUpdate(
        transport: RecorderTransport,
        daemonGrants: DaemonPermissionGrants,
        reopenedViaRecovery: Bool,
        migrationNeeded: Bool
    ) -> Bool {
        // Never auto-close while the one-time migration banner is still pending —
        // it is the sheet's leading step and must not be dismissed out from under
        // a reading user by a coincident daemon-grant refresh (which the sheet's
        // own onAppear starts). Making the override explicit here removes the
        // reliance on the present-check running after this one in
        // updateFirstRunSheetPresentation (Phase 1c, SCR-49).
        guard !reopenedViaRecovery, !migrationNeeded else { return false }
        return transport == .daemon && !daemonGrants.anyRequiredDenied
    }
}

/// Top-level window content (U4). The prototype sidebar (`ShellSidebarView`)
/// replaces the legacy four-pane `List` sidebar inside the existing singleton
/// `Window` scene (KTD-1). The detail routes on `ShellRoute`: Library (U5),
/// Journal (U8), the Day timeline (U9), Privacy settings (U12), and App rules
/// (U13) are live.
///
/// The recording banner overlays the top of the detail area. Fresh installs get
/// the onboarding wizard takeover (U11, KTD-10) instead of the shell; upgrade
/// users keep the migration-interstitial walkthrough sheet, which also remains
/// the permission-loss recovery surface after onboarding.
struct MainWindow: View {
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var permissions: PermissionController
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var privacy: PrivacyController

    /// U11: how the onboarding wizard is being shown, when it is. `.firstRun`
    /// persists progress/completion; `.replay` (sidebar "Replay onboarding")
    /// is read-only — finish writes nothing (KTD-10).
    enum OnboardingMode: Equatable {
        case firstRun
        case replay
    }

    @State private var route: ShellRoute = .library
    /// Non-nil while the onboarding wizard owns the window content (U11).
    @State private var onboarding: OnboardingMode?
    @State private var showingPermissionsSheet = false
    /// True while the walkthrough sheet is open because the user explicitly tapped
    /// "Finish setup" (the recovery latch), as opposed to the launch gate. Set when
    /// the recovery latch presents the sheet, cleared when the sheet dismisses
    /// (`onDismiss`). Suppresses `updateFirstRunSheetPresentation`'s auto-close so a
    /// coincident daemon-grant / transport update can't dismiss the just-reopened
    /// sheet before the user acts (SCR-144).
    @State private var reopenedViaRecovery = false
    /// U6: the New-recording sheet is an in-window overlay (KTD-4), presented
    /// from the Library header. Kept here (not in LibraryView) so it layers over
    /// the whole shell like the prototype's z-41 overlay.
    @State private var showingNewRecording = false
    /// U10: the Recall palette — an in-window overlay (KTD-4) opened by the
    /// window-scoped ⌘⇧F (KTD-13), the Library/Journal search pills, and the
    /// menu-bar "Search…" item (via notification).
    @State private var showingPalette = false

    var body: some View {
        Group {
            if let mode = onboarding {
                // U11: the onboarding takeover replaces the window content for
                // fresh installs (KTD-10) and for the sidebar's read-only
                // replay. Finish routes to Library, or App rules when step 2's
                // "Edit the list" deep link was taken.
                OnboardingWizard(replay: mode == .replay) { destination in
                    onboarding = nil
                    route = destination == .appRules ? .appRules : .library
                    updateFirstRunSheetPresentation()
                }
            } else {
                shellContent
            }
        }
        .sheet(isPresented: $showingPermissionsSheet, onDismiss: {
            reopenedViaRecovery = false
            // Phase 1c (SCR-49): dismissing the first-run sheet while migration is
            // pending means the user has seen the one-time migration banner — it is
            // the sheet's leading step whenever `migrationNeeded` (see
            // FirstRunPermissionsView). Record completion here so the banner is
            // truly one-time and the presentation override stops forcing the sheet
            // open: `shouldPresentOnLaunch` returns true *unconditionally* while
            // `migrationNeeded`, so without this a "Skip for now" / "Done" tap is
            // immediately undone by the next daemon-grant refresh. Guarded +
            // idempotent (a no-op when migration wasn't pending), and it also
            // covers the already-installed upgrade cohort, whose helper never
            // produces the `installedAndRunning` edge that otherwise writes the
            // marker.
            permissions.markMigrationComplete()
        }) {
            FirstRunPermissionsView(isPresented: $showingPermissionsSheet)
                .environmentObject(permissions)
                .environmentObject(recorder)
        }
        .sheet(isPresented: matrixDisclosurePresented) {
            if let disclosure = recorder.matrixDisclosure {
                PrivacyMatrixDisclosureView(disclosure: disclosure)
                    .environmentObject(recorder)
            }
        }
        .onAppear {
            decideOnboardingTakeover()
            updateFirstRunSheetPresentation()
        }
        .onReceive(NotificationCenter.default.publisher(for: .screenCapRecordingDidEnd)) { _ in
            // U7: a recording ended and the main window was restored — land on
            // Library (with the fresh draft card).
            route = .library
        }
        .onChange(of: recorder.daemonProbeCompleted) { _ in
            updateFirstRunSheetPresentation()
        }
        .onChange(of: recorder.transport) { _ in
            updateFirstRunSheetPresentation()
        }
        .onChange(of: permissions.daemonGrants) { _ in
            // The gate keys on daemon-reported grants now (U4), and a refresh
            // (U5) can flip them while the window is open — re-evaluate so the
            // sheet appears on a newly-detected denial and closes once the
            // daemon path is satisfied.
            updateFirstRunSheetPresentation()
        }
        .onChange(of: permissions.migrationNeeded) { _ in
            // Phase 1c (SCR-49): the one-time migration marker flips this false
            // when the helper install completes mid-sheet. Re-evaluate so the
            // post-migration auto-close path keys on daemon grants again rather
            // than the migration override holding the sheet open.
            updateFirstRunSheetPresentation()
        }
        .onChange(of: permissions.reopenSetupRequested) { requested in
            // Explicit user recovery action ("Finish setup" in the Privacy tab):
            // present the walkthrough even though `setupDismissed` would suppress
            // the launch gate. This is the way back from a mistaken "Skip for
            // now". Consume the latch so it doesn't re-present on later updates.
            guard requested else { return }
            // Mark this as a recovery-latched open so updateFirstRunSheetPresentation
            // won't auto-close it out from under the user (SCR-144). Cleared in the
            // sheet's onDismiss.
            reopenedViaRecovery = true
            showingPermissionsSheet = true
            permissions.consumeReopenSetupRequest()
        }
    }

    /// The normal app shell — everything the window shows when the onboarding
    /// takeover isn't active.
    private var shellContent: some View {
        // The first-run privacy banner lives here — a sibling ABOVE the
        // NavigationSplitView, NOT inside its detail column. It must stay out of
        // the split view's subtree: the banner's multiline `.fixedSize(...)` text
        // (FirstRunPrivacyBanner) drives a runaway/oscillating height when it
        // participates in NavigationSplitView's column-height negotiation, which
        // balloons the sidebar List's height and pushes its rows off-screen — the
        // sidebar visibly "disappears" ~1–2s after launch, the moment `bannerActive`
        // flips true. Rendering the banner above the split view (full-window width)
        // decouples its layout and keeps the sidebar stable.
        VStack(spacing: 0) {
            if privacy.bannerActive {
                FirstRunPrivacyBanner(
                    onReview: {
                        route = .privacy
                        Task { await privacy.markSetupComplete() }
                    },
                    onDismiss: {
                        Task { await privacy.markSetupComplete() }
                    }
                )
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .transition(.opacity)
            }
            NavigationSplitView {
                sidebar
            } detail: {
                VStack(spacing: 0) {
                    RecordingBanner()
                        .padding(.horizontal, 16)
                        .padding(.top, recorder.state.isRecording ? 12 : 0)
                    detail
                }
                .overlay(alignment: .top) {
                    if let err = recorder.lastError {
                        RecorderErrorMessage(message: err)
                            .padding(SCMetrics.space2)
                            .background(Color.scErrorSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusSm))
                            .padding(.top, SCMetrics.space1)
                            .transition(.opacity)
                    } else if let advisory = recorder.captureAdvisory {
                        // Advisory, non-terminal (SCR-76). De-colored to the neutral
                        // advisory surface (R7) — not yellow — so it reads as "FYI",
                        // distinct from the red error surface above. Errors take priority.
                        RecorderErrorMessage(message: advisory)
                            .padding(SCMetrics.space2)
                            .background(Color.scAdvisorySurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusSm))
                            .padding(.top, SCMetrics.space1)
                            .transition(.opacity)
                    }
                }
            }
        }
        .overlay {
            // U6: New-recording sheet as an in-window overlay (KTD-4), layered
            // over the whole shell like the prototype's z-41 overlay.
            if showingNewRecording {
                NewRecordingSheet(isPresented: $showingNewRecording)
            }
        }
        .overlay {
            // U10: the Recall palette overlay (the prototype's z-40/41 scrim +
            // panel). ↵ on a hit routes to the Day timeline at that moment.
            if showingPalette {
                RecallPaletteView(
                    isPresented: $showingPalette,
                    onJump: { day, seekMs in route = .timeline(day: day, seekMs: seekMs) }
                )
            }
        }
        .background(
            // KTD-13: ⌘⇧F is window-scoped — a hidden in-window shortcut
            // anchor, not a global event tap. Opening over the New-recording
            // sheet is harmless (the palette layers above and esc unwinds it).
            Button("") { showingPalette = true }
                .keyboardShortcut("f", modifiers: [.command, .shift])
                .opacity(0)
                .accessibilityHidden(true)
        )
        .onReceive(NotificationCenter.default.publisher(for: .screenCapOpenRecallPalette)) { _ in
            showingPalette = true
        }
    }

    /// U11 (KTD-10): decide once at launch whether the wizard takes over. The
    /// pure decision lives in `OnboardingStepPolicy.takeover`; this applies its
    /// side effects — latching the pending flag so a mid-wizard relaunch
    /// re-enters (the app's own first-launch `config.toml` write would
    /// otherwise read as prior-install evidence), or backfilling the completion
    /// marker write-once so existing users never see the marketing wizard.
    private func decideOnboardingTakeover() {
        let markers = OnboardingMarkerStore()
        switch OnboardingStepPolicy.takeover(
            completed: markers.isCompleted(),
            wizardPending: markers.wizardPending,
            priorInstallEvidence: markers.hasPriorInstallEvidence(),
            setupSkipped: permissions.setupDismissed,
            isRecording: recorder.state.isRecording
        ) {
        case .wizard:
            markers.setWizardPending(true)
            onboarding = .firstRun
        case .backfillMarker:
            // Best-effort: an IO failure just re-runs this decision next launch.
            try? markers.markCompleted()
        case .none:
            break
        }
    }

    private func updateFirstRunSheetPresentation() {
        // U11: the onboarding wizard embeds the install + grant machinery —
        // never pop (or auto-close bookkeeping for) the walkthrough sheet while
        // the takeover owns the window.
        if onboarding != nil {
            return
        }
        // Don't pop (or churn) the first-run sheet over an active recording. The
        // transport can flip to .cliFallback mid-recording (schemaMismatch /
        // socketUnavailable / connectionFailed) and we don't want to interrupt
        // the in-flight capture with a permissions walkthrough.
        if recorder.state.isRecording {
            return
        }
        // SCR-144 hardening: self-heal a stranded recovery flag. `reopenedViaRecovery`
        // is normally cleared in the sheet's `onDismiss`, but if a co-located sheet
        // won the presentation race the permissions sheet's `onDismiss` may never
        // fire, leaving the flag stuck true and suppressing every future auto-close.
        // If the flag is still set while the sheet is no longer showing, that clear
        // was missed — reset it here so a later satisfied-daemon update auto-closes
        // normally. (Durable fix: fold both sheets into one enum-driven binding.)
        if reopenedViaRecovery, !showingPermissionsSheet {
            reopenedViaRecovery = false
        }
        // Auto-close decision lives in FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate
        // (see its doc-comment); suppressed while the sheet was reopened via the recovery latch.
        if FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate(
            transport: recorder.transport,
            daemonGrants: permissions.daemonGrants,
            reopenedViaRecovery: reopenedViaRecovery,
            migrationNeeded: permissions.migrationNeeded
        ) {
            showingPermissionsSheet = false
        }
        if FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
            daemonProbeCompleted: recorder.daemonProbeCompleted,
            transport: recorder.transport,
            daemonGrants: permissions.daemonGrants,
            setupDismissed: permissions.setupDismissed,
            migrationNeeded: permissions.migrationNeeded,
            onboardingTakeoverActive: onboarding != nil
        ) {
            showingPermissionsSheet = true
        }
    }

    private var matrixDisclosurePresented: Binding<Bool> {
        Binding {
            recorder.matrixDisclosure != nil
        } set: { isPresented in
            if !isPresented {
                recorder.dismissMatrixDisclosure()
            }
        }
    }

    private var sidebar: some View {
        ShellSidebarView(
            route: $route,
            recordings: index.recordings,
            onReplayOnboarding: {
                // U11: re-enter the wizard read-only — live grant states,
                // current storage selection, finish writes nothing (KTD-10).
                // Never seize the window mid-recording (the menu bar can
                // restore the hidden main window while capture runs) — the
                // same gate the automatic takeover applies.
                guard !recorder.state.isRecording else { return }
                onboarding = .replay
            }
        )
        .navigationSplitViewColumnWidth(248)
        .navigationTitle("ScreenCap")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                recordingToolbarControl
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await index.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh recordings")
            }
        }
    }

    /// Persistent Start / Stop control in the window toolbar so the user can
    /// reach it without going to the menu bar once the calendar is populated
    /// (the empty-state Start button only renders when totalCount == 0).
    /// State branches mirror MenuBarMenu so the two surfaces stay in lockstep.
    @ViewBuilder
    private var recordingToolbarControl: some View {
        if recorder.quitProgressSecondsRemaining != nil {
            // Non-actionable during a Cmd+Q-driven shutdown — the menu bar
            // already shows the countdown line.
            Label("Finalizing…", systemImage: "hourglass")
                .labelStyle(.titleAndIcon)
                .foregroundStyle(.secondary)
        } else if case .recording = recorder.state {
            Button {
                recorder.stop()
            } label: {
                Label("Stop", systemImage: "stop.circle.fill")
            }
            .help("Stop recording")
        } else if recorder.state.isRecording {
            // .starting or .stopping — surface progress, don't offer an
            // action that would re-enter the state machine.
            Label(recorder.state.isStopping ? "Stopping…" : "Starting…", systemImage: "hourglass")
                .labelStyle(.titleAndIcon)
                .foregroundStyle(.secondary)
        } else {
            Button {
                recorder.start()
            } label: {
                Label("Start", systemImage: "record.circle")
            }
            .help("Start a new recording")
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch route {
        // Index-independent routes are reachable even while the recordings index
        // is loading or errored — handle them before the index gate.
        case .privacy:
            // U12: the prototype Privacy settings pane. The mask row's
            // disclosure deep-links to App rules — the per-app view of what
            // the always-on policy engine masks and blocks.
            PrivacySettingsView(onOpenAppRules: { route = .appRules })
        case .journal:
            // U8: day-grouped Journal. The search pill opens the Recall palette (U10).
            JournalView(
                onOpenSearch: { showingPalette = true },
                onOpenTimeline: { date, seekMs in route = .timeline(day: date, seekMs: seekMs) }
            )
        case .appRules:
            // U13: the prototype App rules pane.
            AppRulesView()
        case .timeline(let day, let seekMs):
            // U9: the day view. `.id(day)` gives each date a fresh engine +
            // search scope rather than mutating one view's state across days.
            DayTimelineView(date: day, initialSeekMs: seekMs, onBack: { route = .journal })
                .id(day)
        case .library:
            // U5: the prototype card grid. It owns its own loading / error /
            // empty / zero-match states over the recordings index. The
            // New-recording pill opens U6's in-window sheet (KTD-4), gated so it
            // never opens over an active recording (logic 780). Card clicks land
            // on the Day timeline seeked to the recording (U9).
            LibraryView(
                onNewRecording: presentNewRecording,
                onOpenSearch: { showingPalette = true },
                onOpenTimeline: { date, seekMs in route = .timeline(day: date, seekMs: seekMs) }
            )
        }
    }

    /// Present the New-recording sheet unless a recording is already in flight —
    /// the pure gate lives in `NewRecordingSheetPolicy` (U6).
    private func presentNewRecording() {
        guard NewRecordingSheetPolicy.canPresent(recorderState: recorder.state) else { return }
        showingNewRecording = true
    }

}
