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

    // SCR-182 U4 — recent committed searches for the idle state. Held as a
    // StateObject so it survives body rebuilds; the data lives in UserDefaults.
    @StateObject private var recentStore = RecentSearchesStore()

    @State private var query = ""
    @State private var contentIndexEnabled = false
    @State private var consentDeclined = false
    /// SCR-178 U8 — whether the user already skipped the "index existing
    /// recordings" offer (read from settings on load). Gates re-offering after a
    /// "Turn on" so the backfill prompt never re-nags.
    @State private var backfillDeclined = false
    @State private var searchTask: Task<Void, Never>?
    // SCR-182 U1 — the last query actually issued, used to dedupe the trailing
    // `.onChange(of: query)` that a Return or a programmatic chip-tap `query` set
    // produces, so the same query is never searched twice.
    @State private var lastIssuedQuery: String?

    /// SCR-182 U1 — live-search debounce. 300 ms balances responsiveness against
    /// issuing a daemon round-trip on every keystroke.
    private static let searchDebounceNanos: UInt64 = 300_000_000

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

    // SCR-181 U3 — the day the pinned timeline shows; nil defaults to the
    // most-recent day with a hit. Reset on a new result set so a stale day from a
    // prior search can't show an empty axis (mirrors the selectedResultID reset).
    @State private var selectedDay: Date?

    var body: some View {
        searchDetailLayout {
            searchField
        } content: {
            SearchResultsView(
                phase: model.phase,
                consentDeclined: consentDeclined,
                backfillState: model.backfillState,
                isSearching: model.isSearching,
                recentSearches: recentStore.recent,
                selection: $selectedResultID,
                selectedDay: $selectedDay,
                frameIndex: frameIndex,
                thumbnailLoader: thumbnailLoader,
                isSearchFieldFocused: searchFieldFocused,
                onEnableConsent: enableConsent,
                onDeclineConsent: declineConsent,
                onAcceptBackfill: model.acceptBackfill,
                onSkipBackfill: skipBackfill,
                onCancelBackfill: model.cancelBackfill,
                onResumeBackfill: model.resumeBackfill,
                onRunChip: runChipQuery,
                onOpen: openInspect
            )
        }
        .task { await loadSettings() }
        .onAppear {
            // A run-loop hop lets the TextField finish entering the hierarchy
            // before focus is assigned — more reliable than a synchronous set.
            Task { @MainActor in searchFieldFocused = true }
        }
        // SCR-182 U1 — live, debounced search-as-you-type.
        .onChange(of: query) { _ in runSearch(debounced: true) }
        // SCR-183 U6 — announce the search outcome so a VoiceOver user who
        // can't see the screen learns the result instead of hearing silence.
        .onChange(of: model.phase) { phase in
            // SCR-182 U2 — a new result set may not contain the previously
            // selected row; reset so Return-to-open never acts on a stale id.
            // SCR-181 U3 — also reset the timeline's day so it re-defaults to the
            // most-recent day of the new result set, not a stale prior day.
            if case .loaded = phase {
                selectedResultID = nil
                selectedDay = nil
            }
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
                    .onSubmit { submitSearch() }
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

    /// SCR-182 U1 — run a search. Debounced from the field's `.onChange`
    /// (live search-as-you-type); immediate from Return (`.onSubmit`) and chip
    /// taps. A single `searchTask` handle owns both the debounce delay and the
    /// search, so cancelling it on the next keystroke can't leave a sleeping
    /// timer to orphan-fire a stale search. `lastIssuedQuery` (claimed
    /// synchronously on immediate paths, after the delay on debounced ones)
    /// dedupes the trailing `.onChange` that a Return or a programmatic chip-tap
    /// `query` set produces.
    private func runSearch(debounced: Bool = false) {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if debounced, trimmed == lastIssuedQuery { return }
        searchTask?.cancel()
        if !debounced { lastIssuedQuery = trimmed }
        searchTask = Task { @MainActor in
            if debounced {
                try? await Task.sleep(nanoseconds: Self.searchDebounceNanos)
                if Task.isCancelled { return }
                lastIssuedQuery = trimmed
            }
            await model.search(trimmed, contentIndexEnabled: contentIndexEnabled)
        }
    }

    /// SCR-182 U4 — Return commits the current query: record it, then run it
    /// immediately (the `.onSubmit` path, bypassing the live debounce).
    private func submitSearch() {
        recentStore.record(query)
        runSearch()
    }

    /// SCR-182 U4 — a chip tap commits its query: fill the field, record it, and
    /// run immediately. Setting `query` triggers `.onChange`, but `runSearch`'s
    /// synchronous `lastIssuedQuery` claim makes that trailing change a no-op.
    private func runChipQuery(_ text: String) {
        query = text
        recentStore.record(text)
        runSearch()
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
