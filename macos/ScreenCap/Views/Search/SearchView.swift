import SwiftUI

// SCR-174 U5 — the in-app "ask your history" Search surface. Owns a
// SearchViewModel, reads the OCR-indexing flag + chunk duration from settings,
// renders the search field, honest coverage/empty states, and the per-day
// timeline of pointer results. Selecting a result opens the Review window at
// that moment (seek wired in U6). The consent affordance is a minimal note
// here; U7 makes it interactive.
struct SearchView: View {
    @StateObject private var model = SearchViewModel()
    @Environment(\.openWindow) private var openWindow

    @State private var query = ""
    @State private var contentIndexEnabled = false
    @State private var consentDeclined = false
    @State private var searchTask: Task<Void, Never>?

    var body: some View {
        VStack(spacing: 0) {
            searchField
            Divider()
            content
        }
        .task { await loadSettings() }
    }

    // MARK: - Search field

    private var searchField: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
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
            resultsView(results)
        }
    }

    @ViewBuilder
    private func resultsView(_ results: SearchResults) -> some View {
        let anchored = results.items.filter { $0.anchorMs != nil }
        let unanchored = results.items.filter { $0.anchorMs == nil }

        VStack(alignment: .leading, spacing: 0) {
            interpretation(results)
            coverageBar(results.coverage)
            if results.consentNeeded && !consentDeclined {
                consentBanner
            }
            Divider()

            if results.items.isEmpty {
                emptyState(results)
            } else {
                SearchTimelineView(items: anchored) { openReview($0) }
                if !unanchored.isEmpty {
                    unanchoredSection(unanchored)
                }
            }
        }
    }

    @ViewBuilder
    private func interpretation(_ results: SearchResults) -> some View {
        if results.timeWindow != nil || results.appFilter != nil {
            HStack(spacing: 6) {
                Image(systemName: "wand.and.stars").font(.caption2)
                Text(interpretationText(results)).font(.caption)
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 12)
            .padding(.top, 8)
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

    private func coverageBar(_ coverage: CoverageReport) -> some View {
        HStack(spacing: 10) {
            coverageChip("On screen", coverage.screen)
            coverageChip("Audio", coverage.audio)
            coverageChip("Activity", coverage.activity)
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
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

    // MARK: - Consent (minimal note; U7 makes this interactive)

    private var consentBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "text.viewfinder").foregroundStyle(.orange)
            Text("On-screen text isn\u{2019}t being indexed yet, so screen matches are limited.")
                .font(.caption)
            Spacer()
        }
        .padding(10)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
        .padding(.horizontal, 12)
        .padding(.bottom, 8)
    }

    // MARK: - Unanchored (transcript hits with no resolvable time)

    private func unanchoredSection(_ items: [SearchResultItem]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Heard in audio (time approximate)")
                .font(.caption).foregroundStyle(.secondary)
                .padding(.horizontal, 12).padding(.top, 8)
            ForEach(items) { item in
                Button { openReview(item) } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "waveform").foregroundStyle(.purple)
                        Text(item.primaryText).lineLimit(1)
                        Spacer()
                    }
                    .padding(.horizontal, 12).padding(.vertical, 4)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Empty / message states

    @ViewBuilder
    private func emptyState(_ results: SearchResults) -> some View {
        if results.timeWindow != nil, case .empty = results.coverage.activity {
            stateMessage(icon: "calendar.badge.exclamationmark", title: "Nothing recorded then",
                         detail: "No activity was recorded in that time range.")
        } else {
            stateMessage(icon: "magnifyingglass", title: "No matches",
                         detail: "Try different words, an app name, or a time like \u{201C}yesterday\u{201D}.")
        }
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

    /// Opens the Review window for the result's recording. U6 augments this to
    /// also seek to `item.anchorMs`.
    private func openReview(_ item: SearchResultItem) {
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
            // Best-effort: search still works (timeline is authoritative); the
            // consent CTA simply may show since the flag reads false.
            contentIndexEnabled = false
        }
    }
}
