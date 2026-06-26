import SwiftUI

// SCR-174 U5 — the in-app "ask your history" Search surface. The results render
// as a single `List` that is the detail root (per-day `Section`s); the search
// field is pinned with `.safeAreaInset`. A List-as-root sizes reliably inside
// the NavigationSplitView detail — stacking a List/ScrollView below siblings in
// a VStack does not. Selecting a result opens the Review window at that moment.
struct SearchView: View {
    @StateObject private var model = SearchViewModel()
    @Environment(\.openWindow) private var openWindow

    @State private var query = ""
    @State private var contentIndexEnabled = false
    @State private var consentDeclined = false
    @State private var searchTask: Task<Void, Never>?

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
    }

    // MARK: - Search field

    private var searchField: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField("Search your history — e.g. \u{201C}salesforce yesterday afternoon\u{201D}", text: $query)
                    .textFieldStyle(.plain)
                    .onSubmit { runSearch() }
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 10)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
            Label("Searches only what\u{2019}s on this Mac", systemImage: "lock.fill")
                .font(.caption2)
                .foregroundStyle(.secondary)
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

        return List {
            Section {
                if results.timeWindow != nil || results.appFilter != nil {
                    Label(interpretationText(results), systemImage: "wand.and.stars")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .listRowSeparator(.hidden)
                }
                coverageRow(results.coverage)
                    .listRowSeparator(.hidden)
                if results.capReached {
                    Label("Showing the most relevant matches — refine with an app or time to narrow further.",
                          systemImage: "line.3.horizontal.decrease.circle")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .listRowSeparator(.hidden)
                }
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
                            Button { openReview(item) } label: { ResultRow(item: item) }
                                .buttonStyle(.plain)
                        }
                    }
                }
                if !unanchored.isEmpty {
                    Section("Heard in audio (time approximate)") {
                        ForEach(unanchored) { item in
                            Button { openReview(item) } label: { ResultRow(item: item) }
                                .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .listStyle(.inset)
    }

    /// Groups anchored results into day buckets, most-recent day first, newest
    /// within each day. Private to the only consumer (this view).
    private func searchResultsGroupedByDay(_ items: [SearchResultItem]) -> [(day: Date, items: [SearchResultItem])] {
        let groups = Dictionary(grouping: items) { item -> Date in
            let secs = Double(item.anchorMs ?? 0) / 1000
            return Calendar.current.startOfDay(for: Date(timeIntervalSince1970: secs))
        }
        return groups
            .map { (day: $0.key, items: $0.value.sorted { ($0.anchorMs ?? 0) > ($1.anchorMs ?? 0) }) }
            .sorted { $0.day > $1.day }
    }

    /// Hoisted so a per-day section header doesn't allocate a DateFormatter.
    private static let dayLabelFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "EEE MMM d"
        return f
    }()

    private func searchDayLabel(_ day: Date) -> String {
        if Calendar.current.isDateInToday(day) { return "Today" }
        if Calendar.current.isDateInYesterday(day) { return "Yesterday" }
        return Self.dayLabelFormatter.string(from: day)
    }

    /// Hoisted so it isn't re-allocated on every render of the interpretation
    /// row (DateFormatter init is expensive — mirrors RecordingsListView).
    private static let interpretationFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMM d, HH:mm"
        return f
    }()

    private func interpretationText(_ results: SearchResults) -> String {
        var parts: [String] = []
        if let app = results.appFilter { parts.append("in \(app)") }
        if let w = results.timeWindow {
            let f = Self.interpretationFormatter
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
                Circle().fill(coverageColor(state)).frame(width: 7, height: 7)
                Text("\(label): \(coverageText(state))").font(.caption2)
            }
            .foregroundStyle(.secondary)
        }
    }

    private func coverageText(_ state: StreamState) -> String {
        switch state {
        case .notRun: return ""
        case .ok(let count): return "\(count)"
        case .empty: return "no matches"
        case .notIndexed: return "not indexed"
        case .degraded: return "limited"
        case .unavailable: return "unavailable"
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
            HStack(spacing: 8) {
                Image(systemName: "text.viewfinder").foregroundStyle(.orange)
                Text("Search on-screen text too?").font(.callout).bold()
                Spacer()
            }
            Text("Turn this on to also search the text that was on your screen. Newly recorded screens become searchable \u{2014} it all stays on this Mac and is never uploaded.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
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
    }

    private func stateMessage(icon: String, title: String, detail: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.largeTitle).foregroundStyle(.secondary)
            Text(title).font(.headline)
            Text(detail).font(.callout).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
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
            ReviewWindowOpener.shared.setPendingSeek(anchorMs, for: item.recording)
        }
        // Opens a new window, or surfaces an already-open one for this recording
        // (the WindowGroup is keyed on the name). The notification covers the
        // already-open case: that window won't re-run its `.ready` seek path, so
        // it applies the pending seek on receipt instead (#1).
        openWindow(id: ReviewWindowID, value: item.recording)
        if item.anchorMs != nil {
            NotificationCenter.default.post(
                name: .reviewWindowSeekRequested,
                object: nil,
                userInfo: ["recording": item.recording]
            )
        }
    }

    /// The two-bool consent state, kept in sync with the `@State` flags that
    /// drive the view. Mutations go through `ContentIndexConsent` so the
    /// optimistic/revert/preserve invariants are the unit-tested ones (#10).
    private var consentState: ContentIndexConsentState {
        ContentIndexConsentState(indexEnabled: contentIndexEnabled, declined: consentDeclined)
    }

    private func setConsent(_ s: ContentIndexConsentState) {
        contentIndexEnabled = s.indexEnabled
        consentDeclined = s.declined
    }

    private func loadSettings() async {
        do {
            let data = try await CLIClient.runJSONRaw(["settings", "--json"])
            let env = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            setConsent(ContentIndexConsent.applyLoaded(
                indexEnabled: env.settings.contentIndexEnabled,
                declined: env.settings.contentIndexConsentDeclined,
                into: consentState
            ))
            if let dur = env.settings.chunkDuration { model.chunkDurationSeconds = dur }
        } catch {
            // Decode/read failure: assume indexing off (conservative CTA) but
            // PRESERVE a prior decline — a transient read failure must not
            // resurface a dismissed banner (#10).
            setConsent(ContentIndexConsent.loadDidFail(consentState))
        }
    }

    /// Enable on-screen-text indexing going forward, then re-run the query.
    /// Optimistic with success-latch (revert on write failure).
    private func enableConsent() {
        setConsent(ContentIndexConsent.optimisticEnable(consentState))
        Task {
            do {
                _ = try await CLIClient.runJSONRaw(
                    ["settings", "--set", "content_index_enabled=true", "--json"]
                )
            } catch {
                setConsent(ContentIndexConsent.enableDidFail(consentState))
                return
            }
            runSearch()
        }
    }

    /// Persist the decline so the prompt never re-fires (revert on failure).
    private func declineConsent() {
        setConsent(ContentIndexConsent.optimisticDecline(consentState))
        Task {
            do {
                _ = try await CLIClient.runJSONRaw(
                    ["settings", "--set", "content_index_consent_declined=true", "--json"]
                )
            } catch {
                setConsent(ContentIndexConsent.declineDidFail(consentState))
            }
        }
    }
}
