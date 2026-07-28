import Combine
import SwiftUI

enum FirstRunSetupPresentationPolicy {
    /// What the window shows at launch (and on state updates) for the
    /// permission-setup surface (SCR-262 tri-state, superseding the old Bool).
    enum LaunchPresentation: Equatable {
        /// The normal app shell.
        case shell
        /// SCR-262: a helper swap is converging — show the "Finishing update…"
        /// interstitial, never the permission checklist (whose rows can't be
        /// verified until the swapped-in helper is up).
        case updateInterstitial
        /// The permission wall (`PermissionSetupTakeover`).
        case permissionWall
    }

    /// Decide what the window presents for permission setup (R3, U4; SCR-262).
    ///
    /// This keys on *daemon-reported* grant state, not on the daemon being
    /// unreachable. It **supersedes SCR-54's "do not pre-block on the daemon
    /// transport"** — but legitimately so: SCR-54 removed pre-blocking on the
    /// *app process's* (irrelevant) TCC state; this gate keys on the *daemon's*
    /// (the TCC subject's) state.
    ///
    /// - Converging (SCR-262): while a stale-daemon kickstart is converging and
    ///   its deadline hasn't passed, the interstitial wins over everything the
    ///   probe can't verify — including the migration banner (which resumes
    ///   after convergence) and a persisted `setupDismissed` (the interstitial
    ///   is status, not a nag). Only the explicit converging signal enters
    ///   here — bare `.cliFallback` unreachability still means the wall, so a
    ///   dead registration keeps its immediate repair surface.
    /// - `.cliFallback` (daemon unreachable, not converging): the wall — the
    ///   recording would run under the app's own TCC identity, and the wall's
    ///   auto-fired helper install is the only self-heal for dead registrations.
    /// - `.daemon` (reachable): the wall only when the daemon reports a missing
    ///   required grant. Indeterminate / absent-block is never "missing" (the
    ///   engine preflight is the backstop), so it never presents.
    ///
    /// A persisted `setupDismissed` ("Skip for now") suppresses the wall so it
    /// stops re-popping every launch. The start-block (R4) is independent of
    /// `setupDismissed`, so a dismissed sheet never lets a broken recording start.
    ///
    /// U11: `onboardingTakeoverActive` suppresses everything while the
    /// onboarding wizard owns the window — the wizard embeds the same
    /// install + grant machinery, so popping the wall over it would run two
    /// copies of the walkthrough at once. On a fresh install the migration
    /// marker is also absent, so without this guard the migration override
    /// would force the wall over the wizard's welcome step.
    static func launchPresentation(
        daemonProbeCompleted: Bool,
        transport: RecorderTransport,
        daemonGrants: DaemonPermissionGrants,
        setupDismissed: Bool,
        migrationNeeded: Bool,
        onboardingTakeoverActive: Bool = false,
        updateConverging: Bool = false,
        convergenceDeadlineExpired: Bool = false
    ) -> LaunchPresentation {
        guard !onboardingTakeoverActive else { return .shell }
        guard daemonProbeCompleted else { return .shell }
        // SCR-262: mid-swap, nothing UNVERIFIABLE presents the wall — hold the
        // interstitial until the loop verifies a fresh daemon or expires. An
        // answered DENIAL is different: the spurious-wall bug is about
        // indeterminate rows, never an affirmative denied report, so a
        // reachable daemon reporting a required denial presents the wall
        // without waiting out convergence. Past the deadline the wall (the
        // repair surface) presents; the converging flag is normally already
        // false by then (the loop clears it), so the expired input is
        // belt-and-suspenders for the same tick.
        if updateConverging, !convergenceDeadlineExpired {
            // The denial escape honors setupDismissed exactly like the
            // non-converging path below — a user who persisted "Skip for now"
            // must not get the wall mid-swap on a (possibly dying-daemon)
            // denial the settled gate would suppress.
            if transport == .daemon, daemonGrants.anyRequiredDenied, !setupDismissed {
                return .permissionWall
            }
            return .updateInterstitial
        }
        // Phase 1c (SCR-49): the one-time upgrade migration banner shows even when
        // the daemon already reports grants, and even if the walkthrough was
        // previously skipped — it is the upgrade explanation, shown once until
        // migration completes (marker written). It therefore overrides
        // `setupDismissed`. The caller's `recorder.state.isRecording` guard still
        // suppresses the sheet over an active capture (R3).
        if migrationNeeded { return .permissionWall }
        guard !setupDismissed else { return .shell }
        switch transport {
        case .cliFallback:
            return .permissionWall
        case .daemon:
            return daemonGrants.anyRequiredDenied ? .permissionWall : .shell
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
/// `Window` scene (KTD-1). The detail routes on `ShellRoute`: Days (U3), Tasks
/// and Clips placeholders, the Day timeline (U9), Privacy settings, and App
/// rules are live.
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

    @State private var route: ShellRoute = .days
    /// U4 (origin-aware back): the last primary surface the user was on before
    /// opening the day page, so the day page's "← Back" returns there (Days /
    /// Tasks / Chat) rather than always Days. Defaults to Days (R2). Recorded on
    /// every navigation into one of those surfaces; the day page itself never
    /// overwrites it.
    @State private var lastNonTimelineRoute: ShellRoute = .days
    /// Non-nil while the onboarding wizard owns the window content (U11).
    @State private var onboarding: OnboardingMode?
    /// True while the permission-setup takeover owns the window content (U14 —
    /// the onboarding permissions screen doubling as the recovery surface;
    /// replaces the retired modal walkthrough sheet).
    @State private var showingPermissionSetup = false
    /// SCR-262: true while the "Finishing update…" interstitial owns the window
    /// content — a helper swap is converging and the permission rows can't be
    /// verified yet.
    @State private var showingUpdateInterstitial = false
    /// SCR-262: the interstitial is launch-scoped. Latched once the first
    /// post-probe presentation decision lands on shell or wall, so a mid-session
    /// convergence source never replaces window content the user is working in.
    /// SCR-264's post-recording stale-daemon recheck is such a source, but it
    /// fires on a recording→idle edge where this latch is still false (the
    /// evaluator early-returns while recording, so no launch decision settled) —
    /// so its interstitial shows, while a mid-shell "restart helper" retry
    /// through the convergence state stays suppressed.
    @State private var launchPresentationDecided = false
    /// True while the permission setup is open because the user explicitly tapped
    /// "Finish setup" (the recovery latch), as opposed to the launch gate. Set when
    /// the recovery latch presents the takeover, cleared when it closes.
    /// Suppresses `updatePermissionSetupPresentation`'s auto-close so a
    /// coincident daemon-grant / transport update can't dismiss the just-reopened
    /// surface before the user acts (SCR-144).
    @State private var reopenedViaRecovery = false
    /// U6: the New-recording sheet is an in-window overlay (KTD-4), presented
    /// from the Days header. Kept here (not in DaysView) so it layers over
    /// the whole shell like the prototype's z-41 overlay.
    @State private var showingNewRecording = false
    /// U12 / account-sheet U5: the shared Account & Plan sheet, presented via
    /// `.sheet(item:)` so the presentation carries its context (KTD-3). Gated
    /// record/search affordances set `.gate`; nil means no sheet. The Settings
    /// entry is NOT this — it is the embedded `.account` route (KTD-4).
    @State private var presentedAccountContext: AccountSheetContext?
    /// Tracks whether the gate sheet initiated the in-flight sign-in flow
    /// (mirrors `ReviewWindow.startedSignIn`). The auth controller is
    /// app-wide, so only the surface that started the flow may cancel it on
    /// dismissal — otherwise dismissing the gate sheet would abort a login
    /// another window started. Without this teardown, "Sign In" then "Not
    /// now" would orphan the `screencap login` subprocess + browser flow
    /// until its watchdog timeout.
    @State private var startedSignIn = false
    /// U10: the Recall palette — an in-window overlay (KTD-4) opened by the
    /// window-scoped ⌘⇧F (KTD-13), the Days search pill, and the
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
    /// Feedback U4/U5 (KTD-8): the in-app feedback sheet. The controller is
    /// owned here — above the sheet — so the draft survives dismissal for the
    /// session (no disk persistence, deliberate v1 exclusion). Presented from
    /// the sidebar affordance and the menu-bar "Send Feedback…" item (via
    /// notification); no new window scene.
    @StateObject private var feedback = FeedbackController()
    @State private var showingFeedback = false

    var body: some View {
        Group {
            if let mode = onboarding {
                // U11: the onboarding takeover replaces the window content for
                // fresh installs (KTD-10) and for the sidebar's read-only
                // replay. Finish routes to Days, or App rules when step 2's
                // "Edit the list" deep link was taken.
                OnboardingWizard(replay: mode == .replay) { destination in
                    onboarding = nil
                    route = destination == .appRules ? .appRules : .days
                    updatePermissionSetupPresentation()
                    // Search U8: new installs see the disclosure right after
                    // onboarding completes (existing installs get it on plain launch).
                    Task { await maybePresentSearchDisclosure() }
                }
            } else if showingUpdateInterstitial {
                // SCR-262: a helper swap is converging — honest status instead
                // of a permission checklist whose rows can't be verified yet.
                UpdateConvergenceView(anchor: recorder.updateConvergenceAnchor)
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
        .onChange(of: route) { newValue in
            // U4 (origin-aware back): remember the primary surface a day page can
            // be opened FROM — Days, Moments, or Chat — so `onBack` returns there.
            // The day page (`.timeline`) and the settings routes never become an
            // origin, so a back-out never lands on a settings pane.
            switch newValue {
            case .days, .moments, .chat, .taskDetail:
                // `.taskDetail` is recorded so the day page's Back (opened via the
                // task view's "Open full day") returns to the task view. The task
                // view's OWN Back targets `.moments` explicitly (see below), so this
                // never self-loops.
                lastNonTimelineRoute = newValue
            default:
                break
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .screenCapRecordingDidEnd)) { _ in
            // U7: a recording ended and the main window was restored — land on
            // Days (the default landing surface).
            route = .days
            // SCR-262: state changes that fired mid-recording were swallowed by
            // the isRecording early return (e.g. convergence finishing while a
            // recording ran would otherwise leave the interstitial latched with
            // no remaining exit trigger) — re-evaluate now.
            updatePermissionSetupPresentation()
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
        .onChange(of: recorder.updateConverging) { _ in
            // SCR-262: the ONLY trigger for the deadline's interstitial→wall
            // transition — at expiry the loop clears converging while nothing
            // else observable changes (transport stays .cliFallback, grants stay
            // indeterminate), so without this the interstitial would latch
            // forever on a failed convergence.
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
                NewRecordingSheet(
                    isPresented: $showingNewRecording,
                    onOpenIntelligence: { showingNewRecording = false; route = .intelligence }
                )
            }
        }
        .sheet(isPresented: $recorder.showFirstRecordingBeat) {
            // U7 (honest status): the one-time first-recording beat, presented over
            // the shell after the record action fires (non-blocking). Burning the
            // beat-seen one-shot is controller-owned — the binding's reset on
            // dismissal triggers `showFirstRecordingBeat`'s `didSet` (AE2).
            FirstRecordingBeatSheet(
                isPresented: $recorder.showFirstRecordingBeat,
                onOpenIntelligence: { route = .intelligence }
            )
        }
        .sheet(item: $presentedAccountContext, onDismiss: {
            // Every dismissal route — "Not now", Esc, and the system's
            // item → nil transition — converges here, mirroring
            // ReviewWindow's `.sheet(onDismiss:)` teardown: cancel the
            // in-flight sign-in only if this sheet started it.
            teardownSignInIfOwned()
        }) { context in
            // U12 / account-sheet U5: the unified Account & Plan sheet shared
            // by every gated record/search affordance across the shell
            // (replaces the retired UpgradePromptView).
            AccountSheetView(
                auth: auth,
                context: context,
                onStartSignIn: {
                    // Ownership latch (R14): fired precisely when THIS
                    // sheet's own Sign In / retry buttons launch a login, so
                    // dismissing it cancels only flows it actually started —
                    // never a sign-in another surface began while the gate
                    // sheet happened to be up.
                    startedSignIn = true
                },
                onDismiss: { presentedAccountContext = nil }
            )
        }
        .sheet(isPresented: $showingFeedback) {
            // Feedback U4 (KTD-8): the in-app feedback form. Dismissal in the
            // draft state is harmless (the controller keeps the draft for the
            // session); the sheet itself disables interactive dismissal while
            // sending.
            FeedbackSheetView(
                controller: feedback,
                auth: auth,
                onDismiss: { showingFeedback = false }
            )
        }
        .overlay {
            // U10: the Recall palette overlay (the prototype's z-40/41 scrim +
            // panel). ↵ on a hit routes to the Day timeline at that moment.
            if showingPalette {
                RecallPaletteView(
                    isPresented: $showingPalette,
                    onJump: { day, seekMs in route = .timeline(day: day, seekMs: seekMs, highlight: nil) }
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
        .onReceive(NotificationCenter.default.publisher(for: .screenCapOpenFeedbackForm)) { _ in
            // Feedback U5 (KTD-8): the menu-bar "Send Feedback…" item routed
            // here after focusing/opening the window — same bridge pattern as
            // the Search and account-gate items.
            showingFeedback = true
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
              !showingUpdateInterstitial,
              recorder.matrixDisclosure == nil else { return }
        didEvaluateSearchDisclosure = true
        do {
            let data = try await CLIClient.runJSONRaw(["settings", "--json"])
            let env = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            let acknowledged = env.settings.corpusEncrypted ?? false
            let declined = env.settings.contentIndexConsentDeclined ?? false
            if SearchDisclosurePolicy.shouldPresent(acknowledged: acknowledged, declined: declined) {
                // Re-check the takeover guards: the interstitial or wall can
                // appear while the settings read was in flight (the launch
                // task's first probe completes after onAppear evaluated the
                // guard above). Retry next evaluation instead of popping the
                // consent sheet over a takeover.
                guard onboarding == nil, !showingPermissionSetup, !showingUpdateInterstitial else {
                    didEvaluateSearchDisclosure = false
                    return
                }
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
        let decision = FirstRunSetupPresentationPolicy.launchPresentation(
            daemonProbeCompleted: recorder.daemonProbeCompleted,
            transport: recorder.transport,
            daemonGrants: permissions.daemonGrants,
            setupDismissed: permissions.setupDismissed,
            migrationNeeded: permissions.migrationNeeded,
            onboardingTakeoverActive: onboarding != nil,
            updateConverging: recorder.updateConverging,
            convergenceDeadlineExpired: recorder.updateConvergenceFailed
        )
        // SCR-262: the interstitial is launch-scoped — once the first post-probe
        // decision landed on shell or wall, the interstitial may not replace
        // content the user is working in. SCR-264 added a mid-session convergence
        // source (the post-recording stale-daemon recheck), but it fires on a
        // recording→idle edge where this latch is still false — the evaluator
        // early-returns while recording, so the launch decision never settled — so
        // its interstitial still shows. The latch keeps the other mid-session
        // sources (e.g. wiring the Days/Day-timeline "Restart helper" retries
        // through the convergence state) from becoming a surprise window takeover.
        // The launch path itself is unaffected: at onAppear the probe hasn't
        // completed, so the latch only sets after the launch task's sequenced
        // first probe resolves the real decision.
        switch decision {
        case .updateInterstitial:
            if !launchPresentationDecided {
                showingUpdateInterstitial = true
            }
        case .shell, .permissionWall:
            showingUpdateInterstitial = false
            if recorder.daemonProbeCompleted {
                launchPresentationDecided = true
            }
            if decision == .permissionWall {
                showingPermissionSetup = true
            }
        }
    }

    /// The takeover's single close path — the step's Continue and the policy
    /// auto-close both land here. Carries the retired sheet's `onDismiss`
    /// bookkeeping: clear the recovery latch (SCR-144) and record migration
    /// completion (Phase 1c, SCR-49 — guarded + idempotent, a no-op when
    /// migration wasn't pending; also covers the already-installed upgrade
    /// cohort whose helper never produces the `installedAndRunning` edge that
    /// otherwise writes the marker). Without the marker write,
    /// `launchPresentation` returns the wall *unconditionally* while
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
            // Feedback U5: the main-window entry point — same sheet as the
            // menu-bar item (KTD-8).
            onSendFeedback: { showingFeedback = true },
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
        // The width is a HARD minimum the window must be able to honour — see
        // ShellWindowLayout, which pins it against the declared window minimum.
        .frame(width: ShellWindowLayout.sidebarWidth)
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
        case .days:
            // U3: the Days landing surface — day cards + the always-present Today
            // card. The New-recording pill opens U6's in-window sheet (KTD-4),
            // gated so it never opens over an active recording; the search pill
            // opens the Recall palette (U10); a card opens the day page (U9).
            DaysView(
                onNewRecording: presentNewRecording,
                onUpgradePrompt: { presentedAccountContext = .gate },
                onOpenSearch: { showingPalette = true },
                onOpenTimeline: { date, seekMs in route = .timeline(day: date, seekMs: seekMs, highlight: nil) },
                // U9 — the resume card lands on the day at the thread's last block,
                // with the block band highlighted (AE3), keyed by block_id span.
                onResumeThread: { day, seekMs, highlight in
                    route = .timeline(day: day, seekMs: seekMs, highlight: highlight)
                }
            )
        case .moments:
            // Moments — the merged surface (Tasks + Clips): one cross-day list of
            // app-detected spans and the ranges you clipped, interleaved by footage
            // time, with the clipped ones marked and filterable (R1–R7). A row click
            // opens its day page seeked to the span with the band highlighted (AE3);
            // clipped rows play / export / share / delete in place (R4).
            MomentsView(
                onOpenTask: { key in route = .taskDetail(key) },
                onOpenTimeline: { day, seekMs, highlight in
                    route = .timeline(day: day, seekMs: seekMs, highlight: highlight)
                },
                onOpenIntelligence: { route = .intelligence }
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
            // answers with the captured moments shown as sources rendered as
            // day + time (R13/KTD-11, never a recording name). U12: a source tap
            // routes to the DAY PAGE seeked to that moment (replacing the old
            // Inspect deep-link, KTD-10); the incoming `highlight` (nil for Chat
            // sources today) threads AE3's task-band emphasis. The no-backend
            // affordance (R6) deep-links to the Intelligence pane.
            ChatView(
                onOpenIntelligenceSettings: { route = .intelligence },
                onOpenCitation: { day, seekMs, highlight in
                    route = .timeline(day: day, seekMs: seekMs, highlight: highlight)
                }
            )
        case .timeline(let day, let seekMs, let highlight):
            // U9: the day view. `.id(day)` gives each date a fresh engine +
            // search scope rather than mutating one view's state across days.
            // U4: `onBack` restores the surface the user came from (Days/Tasks/
            // Chat), not always Days; an incoming task-span highlight (AE3) is
            // threaded through to the strip.
            DayTimelineView(
                date: day,
                initialSeekMs: seekMs,
                highlightedSpan: highlight.map { (startMs: $0.startMs, endMs: $0.endMs) },
                onBack: { route = lastNonTimelineRoute }
            )
                .id(day)
        case .taskDetail(let key):
            // Opening a task moment lands on its own scoped view — player +
            // task-only strip — not the whole day. Back returns to Moments
            // explicitly; "Open full day" reaches the day page seeked + highlighted
            // (the prior behavior, kept as the secondary path).
            TaskDetailView(
                task: key,
                onBack: { route = .moments },
                onOpenFullDay: {
                    route = .timeline(
                        day: key.day,
                        seekMs: key.startMs,
                        highlight: DaySpanHighlight(startMs: key.startMs, endMs: key.endMs)
                    )
                }
            )
                .id(key)
        }
    }

    /// Cancels the in-flight sign-in only if the gate sheet started it and a
    /// flow is still in progress (mirrors `ReviewWindow.teardownSignInIfOwned`).
    /// Safe on any dismissal route; clears the ownership flag so it's a no-op
    /// on a second call. The embedded `.account` pane needs no counterpart —
    /// it isn't dismissible, so a login it starts is never orphaned.
    private func teardownSignInIfOwned() {
        guard startedSignIn else { return }
        startedSignIn = false
        if case .inProgress = auth.signInFlow {
            auth.cancelSignIn()
        }
    }

    /// Present the New-recording sheet unless a recording is already in flight —
    /// the pure gate lives in `NewRecordingSheetPolicy` (U6).
    private func presentNewRecording() {
        guard NewRecordingSheetPolicy.canPresent(recorderState: recorder.state) else { return }
        showingNewRecording = true
    }

}
