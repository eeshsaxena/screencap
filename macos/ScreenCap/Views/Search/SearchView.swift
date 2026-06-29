import AppKit
import SwiftUI

// SCR-174 U5 — the in-app "ask your history" Search surface. The results render
// as a single `List` that is the detail root (per-day `Section`s); the search
// field is pinned with `.safeAreaInset`. A List-as-root sizes reliably inside
// the NavigationSplitView detail — stacking a List/ScrollView below siblings in
// a VStack does not. Selecting a result opens the read-only inspect window at that moment.
//
// SCR-184 U1 — the phase-driven rendering lives in `SearchResultsView` (a pure,
// hostable view) and is composed here through the shared `searchDetailLayout`
// seam. This view owns the live state the rendering can't: the `@StateObject`
// model, the recordings index, window opening, the focus state, and the
// side-effecting actions, wiring them into the pure view as values + callbacks.
struct SearchView: View {
    @StateObject private var model = SearchViewModel()
    @Environment(\.openWindow) private var openWindow
    /// The app-wide recordings index — cross-referenced by name to detect a stub
    /// result (uploaded; local media deleted) before opening inspect.
    @EnvironmentObject private var index: RecordingsIndex

    // SCR-177 — shared per-result-set frame resolver + thumbnail cache (one
    // cache across the whole list, not per-row).
    @State private var frameIndex = RecordingFrameIndex()
    @State private var thumbnailLoader = ThumbnailLoader()

    @State private var query = ""
    @State private var contentIndexEnabled = false
    @State private var consentDeclined = false
    /// SCR-178 U8 — whether the user already skipped the "index existing
    /// recordings" offer (read from settings on load). Gates re-offering after a
    /// "Turn on" so the backfill prompt never re-nags.
    @State private var backfillDeclined = false
    @State private var searchTask: Task<Void, Never>?
    /// Stub-recording guard message — shown via an alert instead of opening an
    /// inspect window that would fail to load (mirrors the Recordings list).
    @State private var rowError: String?

    // SCR-183 U3 — focus the field when Search opens. `MainWindow.detail` builds
    // a fresh `SearchView()` per open, so `.onAppear` re-fires each time.
    @FocusState private var searchFieldFocused: Bool

    // SCR-183 U4 — keyboard selection in the results list. Arrow keys move this
    // selection; Return opens the selected result (the field keeps its own Return
    // for running a search, gated by focus).
    @State private var selectedResultID: SearchResultItem.ID?

    var body: some View {
        searchDetailLayout {
            searchField
        } content: {
            SearchResultsView(
                phase: model.phase,
                consentDeclined: consentDeclined,
                backfillState: model.backfillState,
                selection: $selectedResultID,
                frameIndex: frameIndex,
                thumbnailLoader: thumbnailLoader,
                isSearchFieldFocused: searchFieldFocused,
                onEnableConsent: enableConsent,
                onDeclineConsent: declineConsent,
                onAcceptBackfill: model.acceptBackfill,
                onSkipBackfill: skipBackfill,
                onCancelBackfill: model.cancelBackfill,
                onResumeBackfill: model.resumeBackfill,
                onOpen: openInspect
            )
        }
        .task { await loadSettings() }
        .onAppear {
            // A run-loop hop lets the TextField finish entering the hierarchy
            // before focus is assigned — more reliable than a synchronous set.
            Task { @MainActor in searchFieldFocused = true }
        }
        // SCR-183 U6 — announce the search outcome so a VoiceOver user who
        // can't see the screen learns the result instead of hearing silence.
        .onChange(of: model.phase) { phase in
            if let message = SearchAccessibility.searchOutcomeAnnouncement(for: phase) {
                announceToVoiceOver(message)
            }
        }
        // SCR-178 U8 — announce the backfill outcome on a terminal transition
        // only (done/paused/cancelled/start-failed); in-progress ticks return nil
        // from the builder and stay silent.
        .onChange(of: model.backfillState) { state in
            if let message = SearchAccessibility.backfillAnnouncement(for: state) {
                announceToVoiceOver(message)
            }
        }
        .alert("Can\u{2019}t open recording", isPresented: errorBinding) {
            Button("OK") { rowError = nil }
        } message: {
            Text(rowError ?? "")
        }
    }

    private var errorBinding: Binding<Bool> {
        Binding(get: { rowError != nil }, set: { if !$0 { rowError = nil } })
    }

    /// Post a VoiceOver announcement. Uses the AppKit API (macOS 10.9+) rather
    /// than SwiftUI's `AccessibilityNotification.Announcement`, which is macOS 14+
    /// and unavailable on this app's 13.0 floor.
    private func announceToVoiceOver(_ message: String) {
        NSAccessibility.post(
            element: (NSApp.keyWindow ?? NSApp.mainWindow ?? NSApp) as Any,
            notification: .announcementRequested,
            userInfo: [
                .announcement: message,
                .priority: NSAccessibilityPriorityLevel.high.rawValue,
            ]
        )
    }

    // MARK: - Search field

    private var searchField: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField("Search your history — e.g. \u{201C}salesforce yesterday afternoon\u{201D}", text: $query)
                    .textFieldStyle(.plain)
                    .accessibilityLabel("Search your history")
                    .focused($searchFieldFocused)
                    .onSubmit { runSearch() }
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 10)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
            Label("Searches only what\u{2019}s on this Mac", systemImage: "lock.fill")
                .font(.caption2)
                .foregroundStyle(.secondary)
                // Keep the lock glyph visible but read only the text to VoiceOver.
                .accessibilityLabel("Searches only what\u{2019}s on this Mac")
        }
        .padding(12)
    }

    // MARK: - Actions

    private func runSearch() {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        searchTask?.cancel()
        searchTask = Task { await model.search(trimmed, contentIndexEnabled: contentIndexEnabled) }
    }

    /// Opens the read-only inspect window for the result's recording and (U6)
    /// requests a one-shot seek to the hit moment, delivered out-of-band so the
    /// window stays keyed on the recording name. A stub recording (uploaded;
    /// local media deleted) has nothing to inspect, so it shows the same
    /// friendly download message the Recordings list shows rather than opening a
    /// window that would fail to load.
    private func openInspect(_ item: SearchResultItem) {
        let isStub = index.recordings.first(where: { $0.name == item.recording })?.isStub ?? false
        switch InspectRouting.decide(
            recording: item.recording, anchorMs: item.anchorMs, isStub: isStub
        ) {
        case .unavailable(let message):
            rowError = message
        case .open(let recording, let seekMs):
            // Assign the (possibly nil) seek — assigning nil clears any stale
            // entry from a prior reuse, so a later open-at-start for this
            // recording can't inherit an old search moment.
            InspectWindowOpener.shared.pendingSeekMs[recording] = seekMs
            openWindow(id: InspectWindowID, value: recording)
        }
    }

    /// SCR-178 U8 — skip the backfill offer: persist the decline (so it doesn't
    /// re-prompt), remember it locally for this session, and hide the affordance.
    /// No job runs.
    private func skipBackfill() {
        backfillDeclined = true
        model.skipBackfill()
    }

    private func loadSettings() async {
        do {
            let data = try await CLIClient.runJSONRaw(["settings", "--json"])
            let env = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            contentIndexEnabled = env.settings.contentIndexEnabled ?? false
            consentDeclined = env.settings.contentIndexConsentDeclined ?? false
            backfillDeclined = env.settings.contentIndexBackfillDeclined ?? false
            if let dur = env.settings.chunkDuration { model.chunkDurationSeconds = dur }
        } catch {
            contentIndexEnabled = false
        }
    }

    /// Enable on-screen-text indexing going forward, then re-run the query.
    /// Optimistic with success-latch (revert on write failure).
    private func enableConsent() {
        contentIndexEnabled = true
        // SCR-178 U8 — offer the historical backfill the moment indexing is
        // enabled (unless the user already skipped it). Done before the write so
        // the offer is up immediately; the affordance lives in `model` and is
        // independent of `consentNeeded`, so the upcoming re-search (which flips
        // `consentNeeded` false) does not dismiss it.
        model.offerBackfill(alreadyDeclined: backfillDeclined)
        Task {
            do {
                _ = try await CLIClient.runJSONRaw(
                    ["settings", "--set", "content_index_enabled=true", "--json"]
                )
            } catch {
                contentIndexEnabled = false
                model.dismissBackfillOffer()
                return
            }
            runSearch()
        }
    }

    /// Persist the decline so the prompt never re-fires (revert on failure).
    private func declineConsent() {
        consentDeclined = true
        Task {
            do {
                _ = try await CLIClient.runJSONRaw(
                    ["settings", "--set", "content_index_consent_declined=true", "--json"]
                )
            } catch {
                consentDeclined = false
            }
        }
    }
}
