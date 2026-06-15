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
        setupDismissed: Bool
    ) -> Bool {
        guard daemonProbeCompleted, !setupDismissed else { return false }
        switch transport {
        case .cliFallback:
            return true
        case .daemon:
            return daemonGrants.anyRequiredDenied
        }
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

    enum SidebarSection: Hashable { case calendar, recordings, privacy }

    @State private var section: SidebarSection = .calendar
    @State private var selectedDate: Date?
    @State private var visibleMonth: Date = startOfCurrentMonth()
    @State private var showingPermissionsSheet = false

    var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
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
                RecordingBanner()
                    .padding(.horizontal, 16)
                    .padding(.top, recorder.state.isRecording ? 12 : 0)
                detail
            }
            .overlay(alignment: .top) {
                if let err = recorder.lastError {
                    RecorderErrorMessage(message: err)
                        .padding(8)
                        .background(.red.opacity(0.15), in: RoundedRectangle(cornerRadius: 6))
                        .padding(.top, 4)
                        .transition(.opacity)
                } else if let advisory = recorder.captureAdvisory {
                    // Advisory, non-terminal (SCR-76) — distinct yellow tint so
                    // it doesn't read as a terminal error. Errors take priority.
                    RecorderErrorMessage(message: advisory)
                        .padding(8)
                        .background(.yellow.opacity(0.18), in: RoundedRectangle(cornerRadius: 6))
                        .padding(.top, 4)
                        .transition(.opacity)
                }
            }
        }
        .sheet(isPresented: $showingPermissionsSheet) {
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
            updateFirstRunSheetPresentation()
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
        .onChange(of: permissions.reopenSetupRequested) { requested in
            // Explicit user recovery action ("Finish setup" in the Privacy tab):
            // present the walkthrough even though `setupDismissed` would suppress
            // the launch gate. This is the way back from a mistaken "Skip for
            // now". Consume the latch so it doesn't re-present on later updates.
            guard requested else { return }
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
        // Close once the daemon path is satisfied — reachable and NOT reporting
        // a required denial (all granted, or indeterminate). Covers the cold-boot
        // cliFallback→daemon bounce and the post-grant refresh (U5). A reachable
        // daemon that still reports a denial keeps the walkthrough up.
        if recorder.transport == .daemon, !permissions.daemonGrants.anyRequiredDenied {
            showingPermissionsSheet = false
        }
        if FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
            daemonProbeCompleted: recorder.daemonProbeCompleted,
            transport: recorder.transport,
            daemonGrants: permissions.daemonGrants,
            setupDismissed: permissions.setupDismissed
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
        List(selection: $section) {
            NavigationLink(value: SidebarSection.calendar) {
                Label("Calendar", systemImage: "calendar")
            }
            NavigationLink(value: SidebarSection.recordings) {
                Label("Recordings", systemImage: "list.bullet.rectangle")
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
            .tint(.red)
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
        // Three distinct states the user can be in. Without this gate the
        // welcome state (CalendarView) would render misleadingly during
        // first-load and after any CLI failure — both of which look like
        // "no recordings" but mean something different.
        if index.isLoading && index.recordings.isEmpty {
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
                .foregroundStyle(.orange)
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
