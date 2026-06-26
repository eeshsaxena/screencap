import AppKit
import SwiftUI

// SCR-174 U5 — the in-app "ask your history" Search surface. The results render
// as a single `List` that is the detail root (per-day `Section`s); the search
// field is pinned with `.safeAreaInset`. A List-as-root sizes reliably inside
// the NavigationSplitView detail — stacking a List/ScrollView below siblings in
// a VStack does not. Selecting a result opens the Review window at that moment.
struct SearchView: View {
    @StateObject private var model = SearchViewModel()
    @Environment(\.openWindow) private var openWindow

    // SCR-177 — shared per-result-set frame resolver + thumbnail cache (one
    // cache across the whole list, not per-row).
    @State private var frameIndex = RecordingFrameIndex()
    @State private var thumbnailLoader = ThumbnailLoader()

    @State private var query = ""
    @State private var contentIndexEnabled = false
    @State private var consentDeclined = false
    @State private var searchTask: Task<Void, Never>?

    // SCR-183 U3 — focus the field when Search opens. `MainWindow.detail` builds
    // a fresh `SearchView()` per open, so `.onAppear` re-fires each time.
    @FocusState private var searchFieldFocused: Bool

    // SCR-183 U4 — keyboard selection in the results list. Arrow keys move this
    // selection; Return opens the selected result (the field keeps its own Return
    // for running a search, gated by focus).
    @State private var selectedResultID: SearchResultItem.ID?

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .safeAreaInset(edge: .top, spacing: 0) {
                VStack(spacing: 0) {
                    searchField
                    Divider()
                }
                .background(.bar)
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

    // MARK: - Content by phase

    @ViewBuilder
    private var content: some View {
        switch model.phase {
        case .idle:
            stateMessage(
                icon: "magnifyingglass",
                title: "Ask your history",
                detail: "Search across what was on screen, said aloud, and which apps you used \u{2014} all locally."
            )
        case .searching:
            ProgressView().controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityLabel("Searching your history")
        case .daemonDown:
            stateMessage(
                icon: "bolt.horizontal.circle",
                title: "ScreenCap isn\u{2019}t running",
                detail: "Start ScreenCap to search your history."
            )
        case .loaded(let results):
            resultsList(results)
        }
    }

    private func resultsList(_ results: SearchResults) -> some View {
        let anchored = results.items.filter { $0.anchorMs != nil }
        let unanchored = results.items.filter { $0.anchorMs == nil }
        let days = searchResultsGroupedByDay(anchored)

        return List(selection: $selectedResultID) {
            Section {
                if results.timeWindow != nil || results.appFilter != nil {
                    let interpretation = interpretationText(results)
                    Label(interpretation, systemImage: "wand.and.stars")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .listRowSeparator(.hidden)
                        .accessibilityLabel(interpretation)
                }
                coverageRow(results.coverage)
                    .listRowSeparator(.hidden)
                if results.consentNeeded && !consentDeclined {
                    consentBanner
                        .listRowSeparator(.hidden)
                }
            }

            if results.items.isEmpty {
                emptyRow(results)
            } else {
                ForEach(days, id: \.day) { group in
                    Section(searchDayLabel(group.day)) {
                        ForEach(group.items) { item in
                            resultRow(item, queryTerms: results.queryTerms)
                        }
                    }
                }
                if !unanchored.isEmpty {
                    Section("Heard in audio (time approximate)") {
                        ForEach(unanchored) { item in
                            resultRow(item, queryTerms: results.queryTerms)
                        }
                    }
                }
            }
        }
        .listStyle(.inset)
        // U4 — invisible Return handler for the keyboard-selected result.
        .background(returnKeyHandler(results))
    }

    /// SCR-183 U4 — one result row. Plain selectable row (NOT a `Button`): a
    /// `.plain` Button per row hijacks the window's default action, so Return
    /// fired the *first* row's button instead of the keyboard-selected one
    /// (verified on-device). As a plain row, single-click selects (keyboard
    /// parity) and `returnKeyHandler` owns Return; double-click opens for the
    /// mouse. The combined VoiceOver label lives on `ResultRow` (U2).
    private func resultRow(_ item: SearchResultItem, queryTerms: [String]) -> some View {
        ResultRow(
            item: item,
            queryTerms: queryTerms,
            frameIndex: frameIndex,
            thumbnailLoader: thumbnailLoader
        )
        .contentShape(Rectangle())
        .tag(item.id)   // arrow-key selection target
        .simultaneousGesture(TapGesture(count: 2).onEnded { openReview(item) })
    }

    /// SCR-183 U4 — opens the keyboard-selected result on Return. The lone
    /// `.defaultAction` button in the window (rows are no longer Buttons), so it
    /// unambiguously owns Return. Present only when a result is selected, the
    /// search field is not focused, AND the consent banner is not showing — so it
    /// never steals Return from the field's run-a-search binding, nor from the
    /// banner's prominent "Turn on" action (SCR-183 review #1). A 1×1 (non-zero)
    /// frame keeps SwiftUI from culling it and dropping the shortcut registration.
    @ViewBuilder
    private func returnKeyHandler(_ results: SearchResults) -> some View {
        // Stand down while the consent banner is up: its prominent "Turn on"
        // button should own Return there, so this hidden open-the-selected-result
        // handler must not be the window's default action (SCR-183 review #1).
        if !searchFieldFocused,
           !(results.consentNeeded && !consentDeclined),
           let target = searchReviewTarget(for: selectedResultID, in: results) {
            Button("") { openReview(target) }
                .keyboardShortcut(.defaultAction)
                .frame(width: 1, height: 1)
                .opacity(0)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }

    private func interpretationText(_ results: SearchResults) -> String {
        var parts: [String] = []
        if let app = results.appFilter { parts.append("in \(app)") }
        if let w = results.timeWindow {
            let f = DateFormatter()
            f.dateFormat = "MMM d, HH:mm"
            let start = Date(timeIntervalSince1970: Double(w.startMs) / 1000)
            let end = Date(timeIntervalSince1970: Double(w.endMs) / 1000)
            parts.append("\(f.string(from: start)) \u{2013} \(f.string(from: end))")
        }
        return "Interpreted as: " + parts.joined(separator: ", ")
    }

    // MARK: - Coverage

    private func coverageRow(_ coverage: CoverageReport) -> some View {
        HStack(spacing: 12) {
            coverageChip("On screen", coverage.screen)
            coverageChip("Audio", coverage.audio)
            coverageChip("Activity", coverage.activity)
            Spacer()
        }
    }

    @ViewBuilder
    private func coverageChip(_ label: String, _ state: StreamState) -> some View {
        if case .notRun = state {
            EmptyView()
        } else {
            HStack(spacing: 4) {
                // The dot encodes the state by color only — hide it and let the
                // chip's combined label spell the state out in words instead.
                Circle().fill(coverageColor(state)).frame(width: 7, height: 7)
                    .accessibilityHidden(true)
                Text("\(label): \(coverageText(state))").font(.caption2)
            }
            .foregroundStyle(.secondary)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(SearchAccessibility.coverageChipLabel(stream: label, state: state) ?? "")
        }
    }

    private func coverageText(_ state: StreamState) -> String {
        switch state {
        case .notRun: return ""
        case .ok(let count): return "\(count)"
        // Count-independent words live in one place so the visible chip and the
        // spoken label (SearchAccessibility.coverageChipLabel) can't drift.
        case .empty, .notIndexed, .degraded, .unavailable:
            return SearchAccessibility.coverageStatePhrase(for: state) ?? ""
        }
    }

    private func coverageColor(_ state: StreamState) -> Color {
        switch state {
        case .ok: return .green
        case .empty, .notRun: return .secondary
        case .notIndexed, .degraded: return .orange
        case .unavailable: return .red
        }
    }

    // MARK: - Consent (U7)

    private var consentBanner: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Heading + body read as one VoiceOver element (the viewfinder glyph
            // is decorative); the two actions stay as their own buttons.
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Image(systemName: "text.viewfinder").foregroundStyle(.orange)
                        .accessibilityHidden(true)
                    Text("Search on-screen text too?").font(.callout).bold()
                    Spacer()
                }
                Text("Turn this on to also search the text that was on your screen. Newly recorded screens become searchable \u{2014} it all stays on this Mac and is never uploaded.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
            HStack {
                Spacer()
                Button("Not now") { declineConsent() }
                    .buttonStyle(.bordered)
                Button("Turn on") { enableConsent() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(10)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Empty

    @ViewBuilder
    private func emptyRow(_ results: SearchResults) -> some View {
        let isAuthoritativeEmpty: Bool = {
            if results.timeWindow != nil, case .empty = results.coverage.activity { return true }
            return false
        }()
        VStack(alignment: .leading, spacing: 4) {
            Text(isAuthoritativeEmpty ? "Nothing recorded then" : "No matches")
                .font(.headline)
            Text(isAuthoritativeEmpty
                 ? "No activity was recorded in that time range."
                 : "Try different words, an app name, or a time like \u{201C}yesterday\u{201D}.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 8)
        .listRowSeparator(.hidden)
        // One VoiceOver element: "No matches. Try different words…".
        .accessibilityElement(children: .combine)
    }

    private func stateMessage(icon: String, title: String, detail: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.largeTitle).foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text(title).font(.headline)
            Text(detail).font(.callout).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        // Read title + detail as one element; the large glyph is decorative.
        .accessibilityElement(children: .combine)
    }

    // MARK: - Actions

    private func runSearch() {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        searchTask?.cancel()
        searchTask = Task { await model.search(trimmed, contentIndexEnabled: contentIndexEnabled) }
    }

    /// Opens the Review window for the result's recording and (U6) requests a
    /// one-shot seek to the hit moment, delivered out-of-band so the window
    /// stays keyed on the recording name.
    private func openReview(_ item: SearchResultItem) {
        if let anchorMs = item.anchorMs {
            ReviewWindowOpener.shared.pendingSeekMs[item.recording] = anchorMs
        }
        openWindow(id: ReviewWindowID, value: item.recording)
    }

    private func loadSettings() async {
        do {
            let data = try await CLIClient.runJSONRaw(["settings", "--json"])
            let env = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            contentIndexEnabled = env.settings.contentIndexEnabled ?? false
            consentDeclined = env.settings.contentIndexConsentDeclined ?? false
            if let dur = env.settings.chunkDuration { model.chunkDurationSeconds = dur }
        } catch {
            contentIndexEnabled = false
        }
    }

    /// Enable on-screen-text indexing going forward, then re-run the query.
    /// Optimistic with success-latch (revert on write failure).
    private func enableConsent() {
        contentIndexEnabled = true
        Task {
            do {
                _ = try await CLIClient.runJSONRaw(
                    ["settings", "--set", "content_index_enabled=true", "--json"]
                )
            } catch {
                contentIndexEnabled = false
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

/// SCR-183 U4 — which result a keyboard Return acts on, given the current
/// selection. Pure + file-scope so it is unit-testable without the view: a `nil`
/// selection (nothing focused) or a stale id (selection left over after a
/// re-search) both resolve to `nil`, so Return is a safe no-op rather than
/// opening an arbitrary row.
func searchReviewTarget(for id: SearchResultItem.ID?, in results: SearchResults) -> SearchResultItem? {
    guard let id else { return nil }
    return results.items.first { $0.id == id }
}
