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
        // updatePermissionSetupPresentation (Phase 1c, SCR-49).
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
/// the onboarding wizard takeover (U11, KTD-10) instead of the shell; after
/// onboarding, permission loss and the upgrade migration interstitial present
/// the same onboarding permissions screen as a window takeover
/// (`PermissionSetupTakeover`, U14 — the retired walkthrough sheet's
/// replacement).
struct MainWindow: View {
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var permissions: PermissionController
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var privacy: PrivacyController
    @EnvironmentObject private var auth: CloudAuthController

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
    /// True while the permission-setup takeover owns the window content (U14 —
    /// the onboarding permissions screen doubling as the recovery surface;
    /// replaces the retired modal walkthrough sheet).
    @State private var showingPermissionSetup = false
    /// True while the permission setup is open because the user explicitly tapped
    /// "Finish setup" (the recovery latch), as opposed to the launch gate. Set when
    /// the recovery latch presents the takeover, cleared when it closes.
    /// Suppresses `updatePermissionSetupPresentation`'s auto-close so a
    /// coincident daemon-grant / transport update can't dismiss the just-reopened
    /// surface before the user acts (SCR-144).
    @State private var reopenedViaRecovery = false
    /// U6: the New-recording sheet is an in-window overlay (KTD-4), presented
    /// from the Library header. Kept here (not in LibraryView) so it layers over
    /// the whole shell like the prototype's z-41 overlay.
    @State private var showingNewRecording = false
    /// U12 / account-sheet U5: the shared Account & Plan sheet, presented via
    /// `.sheet(item:)` so the presentation carries its context (KTD-3). Gated
    /// record/search affordances set `.gate`; nil means no sheet. The Settings
    /// entry is NOT this — it is the embedded `.account` route (KTD-4).
    @State private var presentedAccountContext: AccountSheetContext?
    /// U10: the Recall palette — an in-window overlay (KTD-4) opened by the
    /// window-scoped ⌘⇧F (KTD-13), the Library/Journal search pills, and the
    /// menu-bar "Search…" item (via notification).
    @State private var showingPalette = false
    /// Search U8 (R6): the one-time search-by-default disclosure. Presented as a
    /// sheet on the shell (after any onboarding/permission takeover clears) when the
    /// corpus is not yet encrypted and consent wasn't declined — covering both new
    /// installs (post-onboarding) and existing installs (which never re-run
    /// onboarding). Evaluated at most once per window session.
    @State private var showingSearchDisclosure = false
    @State private var didEvaluateSearchDisclosure = false
    @State private var searchRetentionDays = 30
    @StateObject private var searchDisclosure = SearchDisclosureController()

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
                    updatePermissionSetupPresentation()
                    // Search U8: new installs see the disclosure right after
                    // onboarding completes (existing installs get it on plain launch).
                    Task { await maybePresentSearchDisclosure() }
                }
            } else if showingPermissionSetup {
                // U14: permission repair reuses the onboarding permissions
                // screen as a window takeover (the retired walkthrough sheet's
                // replacement) — one permission surface everywhere.
                PermissionSetupTakeover(onClose: closePermissionSetup)
            } else {
                shellContent
            }
        }
        .sheet(isPresented: matrixDisclosurePresented) {
            if let disclosure = recorder.matrixDisclosure {
                PrivacyMatrixDisclosureView(disclosure: disclosure)
                    .environmentObject(recorder)
            }
        }
        .sheet(isPresented: $showingSearchDisclosure) {
            SearchDisclosureView(
                controller: searchDisclosure,
                retentionDays: searchRetentionDays,
                onResolved: { showingSearchDisclosure = false }
            )
        }
        .onAppear {
            decideOnboardingTakeover()
            updatePermissionSetupPresentation()
            Task { await maybePresentSearchDisclosure() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .screenCapRecordingDidEnd)) { _ in
            // U7: a recording ended and the main window was restored — land on
            // Library (with the fresh draft card).
            route = .library
        }
        .onChange(of: recorder.daemonProbeCompleted) { _ in
            updatePermissionSetupPresentation()
        }
        .onChange(of: recorder.transport) { _ in
            updatePermissionSetupPresentation()
        }
        .onChange(of: permissions.daemonGrants) { _ in
            // The gate keys on daemon-reported grants now (U4), and a refresh
            // (U5) can flip them while the window is open — re-evaluate so the
            // sheet appears on a newly-detected denial and closes once the
            // daemon path is satisfied.
            updatePermissionSetupPresentation()
        }
        .onChange(of: permissions.migrationNeeded) { _ in
            // Phase 1c (SCR-49): the one-time migration marker flips this false
            // when the helper install completes mid-sheet. Re-evaluate so the
            // post-migration auto-close path keys on daemon grants again rather
            // than the migration override holding the sheet open.
            updatePermissionSetupPresentation()
        }
        .onChange(of: permissions.reopenSetupRequested) { requested in
            // Explicit user recovery action ("Finish setup" in the Privacy tab):
            // present the walkthrough even though `setupDismissed` would suppress
            // the launch gate. This is the way back from a mistaken "Skip for
            // now". Consume the latch so it doesn't re-present on later updates.
            guard requested else { return }
            // Mark this as a recovery-latched open so updatePermissionSetupPresentation
            // won't auto-close it out from under the user (SCR-144). Cleared in the
            // sheet's onDismiss.
            reopenedViaRecovery = true
            showingPermissionSetup = true
            permissions.consumeReopenSetupRequest()
        }
    }

    /// The normal app shell — everything the window shows when the onboarding
    /// takeover isn't active.
    private var shellContent: some View {
        // The first-run privacy banner is a full-width sibling ABOVE the
        // two-column row, NOT inside the detail column, so it spans the whole
        // window and its multiline `.fixedSize(...)` height (FirstRunPrivacyBanner)
        // stays decoupled from the sidebar/detail column layout.
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
            // A plain two-column HStack — NOT a NavigationSplitView. Routing is
            // via `route` (no NavigationLink anywhere), so the split view added no
            // behavior, only native split chrome. Hiding that chrome's window
            // toolbar (to drop the sidebar-collapse control) let the split view's
            // sidebar host view expand over the window's top edge and cover the
            // hidden-title-bar traffic lights — so the app showed no
            // close/minimize/zoom buttons. The HStack keeps the real buttons
            // visible over the sidebar's reserved chrome slot (ShellSidebarView).
            HStack(spacing: 0) {
                sidebar
                detail
                .frame(maxWidth: .infinity, maxHeight: .infinity)
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
        .sheet(item: $presentedAccountContext) { context in
            // U12 / account-sheet U5: the unified Account & Plan sheet shared
            // by every gated record/search affordance across the shell
            // (replaces the retired UpgradePromptView).
            AccountSheetView(
                auth: auth,
                context: context,
                onDismiss: { presentedAccountContext = nil }
            )
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
        .onReceive(NotificationCenter.default.publisher(for: .screenCapOpenAccountGate)) { _ in
            // U12 / account-sheet U5: a gated affordance (menu-bar Start,
            // New-recording sheet, Recall palette) routed here after focusing
            // the window — present the shared sheet in gate context.
            presentedAccountContext = .gate
        }
        .onReceive(NotificationCenter.default.publisher(for: .screenCapOpenAccountPane)) { _ in
            // Account-sheet U5 (KTD-4): the menu-bar "Account…" item — select
            // the embedded Account & Plan pane route.
            route = .account
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

    /// Search U8 (R6): present the one-time search-by-default disclosure when the
    /// shell owns the window and the corpus is not yet encrypted / not declined.
    /// Evaluated at most once per window session; heavily gated so it never fights
    /// the onboarding / permission takeover or another sheet, and fails safe (no
    /// present) when the daemon settings are unavailable.
    private func maybePresentSearchDisclosure() async {
        guard !didEvaluateSearchDisclosure,
              onboarding == nil,
              !showingPermissionSetup,
              recorder.matrixDisclosure == nil else { return }
        didEvaluateSearchDisclosure = true
        do {
            let data = try await CLIClient.runJSONRaw(["settings", "--json"])
            let env = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            let acknowledged = env.settings.corpusEncrypted ?? false
            let declined = env.settings.contentIndexConsentDeclined ?? false
            if SearchDisclosurePolicy.shouldPresent(acknowledged: acknowledged, declined: declined) {
                showingSearchDisclosure = true
            }
        } catch {
            // Daemon/settings unavailable → don't present; allow a retry next launch.
            didEvaluateSearchDisclosure = false
        }
    }

    private func updatePermissionSetupPresentation() {
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
        if reopenedViaRecovery, !showingPermissionSetup {
            reopenedViaRecovery = false
        }
        // Auto-close decision lives in FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate
        // (see its doc-comment); suppressed while the takeover was reopened via
        // the recovery latch.
        if showingPermissionSetup, FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate(
            transport: recorder.transport,
            daemonGrants: permissions.daemonGrants,
            reopenedViaRecovery: reopenedViaRecovery,
            migrationNeeded: permissions.migrationNeeded
        ) {
            closePermissionSetup()
        }
        if FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
            daemonProbeCompleted: recorder.daemonProbeCompleted,
            transport: recorder.transport,
            daemonGrants: permissions.daemonGrants,
            setupDismissed: permissions.setupDismissed,
            migrationNeeded: permissions.migrationNeeded,
            onboardingTakeoverActive: onboarding != nil
        ) {
            showingPermissionSetup = true
        }
    }

    /// The takeover's single close path — the step's Continue and the policy
    /// auto-close both land here. Carries the retired sheet's `onDismiss`
    /// bookkeeping: clear the recovery latch (SCR-144) and record migration
    /// completion (Phase 1c, SCR-49 — guarded + idempotent, a no-op when
    /// migration wasn't pending; also covers the already-installed upgrade
    /// cohort whose helper never produces the `installedAndRunning` edge that
    /// otherwise writes the marker). Without the marker write,
    /// `shouldPresentOnLaunch` returns true *unconditionally* while
    /// `migrationNeeded` and a close would be immediately undone by the next
    /// daemon-grant refresh.
    private func closePermissionSetup() {
        reopenedViaRecovery = false
        permissions.markMigrationComplete()
        showingPermissionSetup = false
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
        // Fixed-width custom column (the prototype's sidebar is not a native
        // NavigationSplitView sidebar). 248pt matches the design; ShellSidebarView
        // fills the full window height itself. No `.toolbar(.hidden, …)`: there is
        // no NavigationSplitView, so no native window toolbar or sidebar-collapse
        // control to hide — which is what previously occluded the traffic lights.
        // Two frames: `width:` and `maxHeight:` are distinct SwiftUI overloads.
        .frame(width: 248)
        .frame(maxHeight: .infinity)
    }

    @ViewBuilder
    private var detail: some View {
        switch route {
        // Index-independent routes are reachable even while the recordings index
        // is loading or errored — handle them before the index gate.
        case .account:
            // Account-sheet U5 (KTD-4): the Settings "Account" entry renders
            // the shared account content as an embedded pane — `account`
            // context, no onDismiss (embedded panes have no dismiss
            // affordance; the sidebar is the way out).
            AccountSheetView(auth: auth, context: .account)
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
        case .intelligence:
            // U9: the Intelligence pane — model picker + per-task cloud-consent
            // matrix, persisting through `screencap settings intelligence`.
            IntelligenceSettingsView()
        case .chat:
            // Conversational-recall U8: the multi-turn Chat surface. Grounded
            // answers with the captured moments shown as sources; each source
            // deep-links the Inspect window at that timestamp. Reuses Search's
            // shipped result components (SnippetHighlighter, RecordingCardThumbnail)
            // and the deep-link opener — no new pointer rendering (KTD7). The
            // no-backend affordance (R6) deep-links to the Intelligence pane.
            ChatView(onOpenIntelligenceSettings: { route = .intelligence })
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
                onUpgradePrompt: { presentedAccountContext = .gate },
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
