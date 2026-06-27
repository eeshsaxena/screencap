import AppKit
import SwiftUI

// SCR-174 U5 — the in-app "ask your history" Search surface. The results render
// as a single `List` that is the detail root (per-day `Section`s); the search
// field is pinned with `.safeAreaInset`. A List-as-root sizes reliably inside
// the NavigationSplitView detail — stacking a List/ScrollView below siblings in
// a VStack does not. Selecting a result opens the read-only inspect window at that moment.
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
    @State private var searchTask: Task<Void, Never>?
    // SCR-182 U1 — the last query actually issued, used to dedupe the trailing
    // `.onChange(of: query)` that a Return or a programmatic chip-tap `query` set
    // produces, so the same query is never searched twice.
    @State private var lastIssuedQuery: String?

    /// SCR-182 U1 — live-search debounce. 300 ms balances responsiveness against
    /// issuing a daemon round-trip on every keystroke.
    private static let searchDebounceNanos: UInt64 = 300_000_000

    /// SCR-182 U4 — curated example queries for the idle state, shown so a new
    /// user sees the kinds of questions this surface answers.
    private static let exampleQueries = [
        "salesforce yesterday afternoon",
        "that error message",
        "zoom call this morning",
        "stripe dashboard last week",
    ]
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
            // SCR-182 U1 — live, debounced search-as-you-type.
            .onChange(of: query) { _ in runSearch(debounced: true) }
            .onChange(of: model.phase) { phase in
                // SCR-182 U2 — a new result set may not contain the previously
                // selected row; reset so Return-to-open never acts on a stale id.
                if case .loaded = phase { selectedResultID = nil }
                if let message = SearchAccessibility.searchOutcomeAnnouncement(for: phase) {
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

    // MARK: - Content by phase

    @ViewBuilder
    private var content: some View {
        switch model.phase {
        case .idle:
            idleState
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
                resultsHeader(results)
                    .listRowSeparator(.hidden)
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
        .simultaneousGesture(TapGesture(count: 2).onEnded { openInspect(item) })
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
            Button("") { openInspect(target) }
                .keyboardShortcut(.defaultAction)
                .frame(width: 1, height: 1)
                .opacity(0)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }

    // SCR-182 U3 — result-count + truncation cue. The U2 inline refresh spinner
    // trails the count (same row, not a separate spinner screen) so an in-place
    // refresh reads as "still showing these, fetching more" rather than a blank.
    @ViewBuilder
    private func resultsHeader(_ results: SearchResults) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            if let count = SearchAccessibility.resultCountLabel(results) {
                HStack(spacing: 6) {
                    Text(count)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if model.isSearching {
                        ProgressView()
                            .controlSize(.small)
                            .accessibilityHidden(true)
                    }
                }
            }
            if let note = SearchAccessibility.truncationNote(results) {
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        // Read count + truncation as one VoiceOver stop; the spoken search-outcome
        // announcement (onChange of phase) already covers live updates.
        .accessibilityElement(children: .combine)
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

    // MARK: - Idle state (U4)

    /// SCR-182 U4 — the idle surface: the framing, curated example chips, and the
    /// user's recent searches (when any). Example chips lead unlabeled; recents
    /// follow under a "Recent" heading only when present (no empty placeholder).
    private var idleState: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Ask your history").font(.headline)
                    Text("Search across what was on screen, said aloud, and which apps you used \u{2014} all locally.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)

                chipGroup(title: nil, queries: Self.exampleQueries)

                if !recentStore.recent.isEmpty {
                    chipGroup(title: "Recent", queries: recentStore.recent)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(24)
        }
    }

    @ViewBuilder
    private func chipGroup(title: String?, queries: [String]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            FlowLayout(spacing: 8) {
                ForEach(queries, id: \.self) { queryChip($0) }
            }
        }
    }

    /// A tappable query chip. A plain tappable element (NOT a `Button`) so it
    /// never claims the window's default action — the SCR-183 row discipline,
    /// applied here so an idle chip can't steal Return from the focused field.
    private func queryChip(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .lineLimit(1)
            .truncationMode(.tail)
            .padding(.vertical, 5)
            .padding(.horizontal, 10)
            .background(Color(nsColor: .controlBackgroundColor), in: Capsule())
            .overlay(Capsule().strokeBorder(Color(nsColor: .separatorColor)))
            .contentShape(Capsule())
            .onTapGesture { runChipQuery(text) }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Search \(text)")
            .accessibilityAddTraits(.isButton)
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

    /// SCR-182 U1 — run a search. Debounced from the field's `.onChange`
    /// (live search-as-you-type); immediate from Return (`.onSubmit`) and chip
    /// taps. A single `searchTask` handle owns both the debounce delay and the
    /// search, so cancelling it on the next keystroke can't leave a sleeping
    /// timer to orphan-fire a stale search. `lastIssuedQuery` (set only when a
    /// search actually issues) dedupes the trailing `.onChange` that a Return or
    /// a programmatic chip-tap `query` set produces.
    private func runSearch(debounced: Bool = false) {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if debounced, trimmed == lastIssuedQuery { return }
        searchTask?.cancel()
        // Immediate paths claim the query synchronously so the trailing
        // `.onChange(of: query)` a programmatic chip-tap set produces dedupes
        // against it (the debounced path claims only after the delay, so a
        // superseded delay can't poison the dedupe with a never-searched query).
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

/// SCR-182 U4 — a minimal wrapping layout for the idle-state chips so a narrow
/// window wraps chips onto new lines instead of clipping or overflowing. Uses
/// the macOS 13 `Layout` protocol (the app's deployment floor).
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) -> CGSize {
        walk(subviews, maxWidth: proposal.width ?? .infinity) { _, _, _ in }
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) {
        _ = walk(subviews, maxWidth: bounds.width) { sub, origin, size in
            sub.place(
                at: CGPoint(x: bounds.minX + origin.x, y: bounds.minY + origin.y),
                anchor: .topLeading, proposal: ProposedViewSize(size)
            )
        }
    }

    /// Walks subviews left-to-right, wrapping at `maxWidth`, invoking `place` for
    /// each with its origin relative to (0, 0). Returns the bounding size. The
    /// wrap math lives here once so the two protocol methods can't drift.
    private func walk(
        _ subviews: Subviews, maxWidth: CGFloat,
        place: (LayoutSubviews.Element, CGPoint, CGSize) -> Void
    ) -> CGSize {
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var widest: CGFloat = 0
        for sub in subviews {
            let size = sub.sizeThatFits(.unspecified)
            if x > 0, x + size.width > maxWidth {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            place(sub, CGPoint(x: x, y: y), size)
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: maxWidth.isFinite ? maxWidth : widest, height: y + rowHeight)
    }
}
