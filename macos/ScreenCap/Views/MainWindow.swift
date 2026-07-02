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
    static func shouldPresentOnLaunch(
        daemonProbeCompleted: Bool,
        transport: RecorderTransport,
        daemonGrants: DaemonPermissionGrants,
        setupDismissed: Bool,
        migrationNeeded: Bool
    ) -> Bool {
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

    /// Decide whether to present the first-run plan choice — "keep everything
    /// local (free)" vs "set up cloud" (U6, R1/R2). Hybrid onboarding: the choice
    /// is shown once, AFTER the permission steps, and never again once a cloud
    /// decision has been made (the `cloudDecisionMade` axis). Starting free is the
    /// default, so a user who never decides just stays local (R2).
    ///
    /// Gated so it never stacks over the permission walkthrough (it follows it),
    /// never shows over an active capture (R3), and only once the daemon probe has
    /// completed (so onboarding state is settled).
    static func shouldPresentPlanChoice(
        daemonProbeCompleted: Bool,
        cloudDecisionMade: Bool,
        permissionsSheetShowing: Bool,
        isRecording: Bool
    ) -> Bool {
        guard daemonProbeCompleted else { return false }
        guard !cloudDecisionMade else { return false }
        guard !permissionsSheetShowing else { return false }
        guard !isRecording else { return false }
        return true
    }
}

/// Top-level window content. Sidebar (Calendar / Recordings / Privacy) +
/// detail area. Calendar is the default. Calendar day click filters the
/// recordings list to that day; "Show all" clears the filter. Privacy is a
/// stub until Unit 18.
///
/// Unit 13 overlays the recording banner at the top of the detail area.
struct MainWindow: View {
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var permissions: PermissionController
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var privacy: PrivacyController
    @EnvironmentObject private var auth: CloudAuthController
    @EnvironmentObject private var cloudDecision: CloudDecisionStore

    enum SidebarSection: Hashable { case calendar, recordings, search, privacy }

    @State private var section: SidebarSection = .calendar
    @State private var selectedDate: Date?
    @State private var visibleMonth: Date = startOfCurrentMonth()
    @State private var showingPermissionsSheet = false
    /// U6 first-run plan choice ("keep local" vs "set up cloud"), shown once after
    /// the permission steps until a cloud decision is made.
    @State private var showingPlanChoiceSheet = false
    /// The shared cloud-setup sheet (U7), reached from the plan choice or an upsell.
    @State private var showingCloudSetupSheet = false
    /// Set when "Set up cloud" is chosen in the plan choice, so the cloud-setup
    /// sheet is presented from the plan-choice sheet's `onDismiss` — one sheet
    /// transition per runloop tick (SwiftUI presents only one sheet reliably).
    @State private var pendingCloudSetup = false
    /// True while the walkthrough sheet is open because the user explicitly tapped
    /// "Finish setup" (the recovery latch), as opposed to the launch gate. Set when
    /// the recovery latch presents the sheet, cleared when the sheet dismisses
    /// (`onDismiss`). Suppresses `updateFirstRunSheetPresentation`'s auto-close so a
    /// coincident daemon-grant / transport update can't dismiss the just-reopened
    /// sheet before the user acts (SCR-144).
    @State private var reopenedViaRecovery = false

    var body: some View {
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
                        section = .privacy
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
            // The plan choice follows the permission steps — re-evaluate now that
            // the permission sheet has closed (U6).
            updatePlanChoicePresentation()
        }) {
            FirstRunPermissionsView(isPresented: $showingPermissionsSheet)
                .environmentObject(permissions)
                .environmentObject(recorder)
        }
        .sheet(isPresented: $showingPlanChoiceSheet, onDismiss: {
            // Chain the cloud-setup sheet AFTER the plan-choice sheet has fully
            // dismissed, so only one sheet transition happens per tick.
            if pendingCloudSetup {
                pendingCloudSetup = false
                showingCloudSetupSheet = true
            }
        }) {
            FirstRunPlanChoiceView(
                onKeepLocal: {
                    // Start free, all-local (R2): persist local + record the choice.
                    Task { _ = await auth.setUploadDestination("local") }
                    cloudDecision.markDecided()
                    showingPlanChoiceSheet = false
                },
                onChooseCloud: {
                    // Opt into cloud (R1): record the choice and funnel into the one
                    // shared setup flow (R4), presented from this sheet's onDismiss.
                    cloudDecision.markDecided()
                    pendingCloudSetup = true
                    showingPlanChoiceSheet = false
                }
            )
        }
        .sheet(isPresented: $showingCloudSetupSheet) {
            CloudSetupView(
                auth: auth,
                onComplete: { didGrant in
                    Task {
                        // Only steer the destination to cloud on an actual grant —
                        // never merely because the sheet opened for an already-
                        // entitled account.
                        if didGrant { _ = await auth.setUploadDestination("cloud") }
                        await auth.refreshEntitlements()
                    }
                    showingCloudSetupSheet = false
                },
                onDismiss: { showingCloudSetupSheet = false }
            )
        }
        .alert(
            "Signed in to a different account",
            isPresented: accountMismatchPresented,
            presenting: auth.accountMismatch
        ) { _ in
            Button("Sign In Again") {
                auth.dismissAccountMismatch()
                auth.startSignIn { _ in }
            }
            Button("Later", role: .cancel) { auth.dismissAccountMismatch() }
        } message: { mismatch in
            Text("A cloud recording belongs to a different account\(mismatch.signedInEmail.map { " than \($0)" } ?? ""). Sign in with the owning account to upload it.")
        }
        .sheet(isPresented: matrixDisclosurePresented) {
            if let disclosure = recorder.matrixDisclosure {
                PrivacyMatrixDisclosureView(disclosure: disclosure)
                    .environmentObject(recorder)
            }
        }
        .onAppear {
            updateFirstRunSheetPresentation()
            updatePlanChoicePresentation()
        }
        .onChange(of: recorder.daemonProbeCompleted) { _ in
            updateFirstRunSheetPresentation()
            updatePlanChoicePresentation()
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
        .onChange(of: section) { new in
            // Intentionally one-directional. We only clear the date filter
            // when leaving the recordings section, not when re-entering it
            // from the sidebar with a stale `selectedDate`. The "Show all"
            // breadcrumb in `RecordingsListView` provides the recovery
            // affordance for that edge case. Revisit if friend-trial
            // feedback shows users expect sidebar tap to clear filters.
            if new != .recordings { selectedDate = nil }
        }
    }

    private func updateFirstRunSheetPresentation() {
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
            migrationNeeded: permissions.migrationNeeded
        ) {
            showingPermissionsSheet = true
        }
    }

    /// Present the first-run plan choice (U6) once the permission steps are done
    /// and no cloud decision has been made. Keyed on the same signals as the
    /// permission gate, but a distinct sheet so it never stacks over it.
    private func updatePlanChoicePresentation() {
        guard FirstRunSetupPresentationPolicy.shouldPresentPlanChoice(
            daemonProbeCompleted: recorder.daemonProbeCompleted,
            cloudDecisionMade: cloudDecision.decisionMade,
            permissionsSheetShowing: showingPermissionsSheet,
            isRecording: recorder.state.isRecording
        ) else { return }
        showingPlanChoiceSheet = true
    }

    /// Binding that presents the account-mismatch re-login alert whenever the
    /// controller publishes a live mismatch (U9 / R16).
    private var accountMismatchPresented: Binding<Bool> {
        Binding(
            get: { auth.accountMismatch != nil },
            set: { if !$0 { auth.dismissAccountMismatch() } }
        )
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
        List(selection: $section) {
            NavigationLink(value: SidebarSection.calendar) {
                Label("Calendar", systemImage: "calendar")
            }
            NavigationLink(value: SidebarSection.recordings) {
                Label("Recordings", systemImage: "list.bullet.rectangle")
            }
            NavigationLink(value: SidebarSection.search) {
                Label("Search", systemImage: "magnifyingglass")
            }
            NavigationLink(value: SidebarSection.privacy) {
                Label("Privacy", systemImage: "lock.shield")
            }
        }
        .listStyle(.sidebar)
        .frame(minWidth: 180)
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
        // Search does not depend on the recordings list, so it is reachable
        // even while the index is loading or errored — intercept before the
        // index gate.
        if section == .search {
            SearchView()
        }
        // Three distinct states the user can be in. Without this gate the
        // welcome state (CalendarView) would render misleadingly during
        // first-load and after any CLI failure — both of which look like
        // "no recordings" but mean something different.
        else if index.isLoading && index.recordings.isEmpty {
            loadingState
        } else if let error = index.lastError {
            errorState(error)
        } else {
            sectionContent
        }
    }

    @ViewBuilder
    private var sectionContent: some View {
        switch section {
        case .calendar:
            CalendarView(
                selectedDate: $selectedDate,
                visibleMonth: $visibleMonth
            ) { day in
                selectedDate = day
                section = .recordings
            }
        case .recordings:
            RecordingsListView(filterDay: $selectedDate) { day in
                selectedDate = nil
                visibleMonth = day
                section = .calendar
            }
        case .search:
            // Normally intercepted in `detail` before the index gate; handled
            // here too for switch exhaustiveness.
            SearchView()
        case .privacy:
            PrivacyPaneView()
        }
    }

    private var loadingState: some View {
        VStack(spacing: 12) {
            ProgressView()
                .controlSize(.large)
            Text("Loading recordings…")
                .foregroundStyle(.secondary)
                .font(.callout)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ message: String) -> some View {
        VStack(spacing: 16) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 36))
                // Genuine load failure: red foreground + triangle shape is the
                // reserved error treatment (R7), distinct from de-colored advisories.
                .foregroundStyle(Color.scErrorFg)
            Text("Couldn't load recordings")
                .font(.headline)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            HStack(spacing: 8) {
                Button("Retry") {
                    Task { await index.refresh() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(index.isLoading)

                Button("Dismiss") {
                    index.clearError()
                }
                .buttonStyle(.bordered)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }

    private static func startOfCurrentMonth() -> Date {
        let comps = Calendar.current.dateComponents([.year, .month], from: Date())
        return Calendar.current.date(from: comps) ?? Date()
    }
}
