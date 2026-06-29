import SwiftUI

// SCR-184 U1 — the phase-driven Search rendering, extracted from `SearchView` as
// a pure, hostable view so it can be exercised in NSHostingView-based layout
// tests without a live model, environment, or daemon. `SearchView` keeps the
// `@StateObject` model, env, focus, and side-effecting actions; this view takes
// a plain `phase` value plus display flags and action callbacks and renders the
// states.
//
// The List-as-root + self-sizing `.frame(maxHeight: .infinity)` here, paired with
// the `searchDetailLayout` seam below, is the composition that fixed the SCR-174
// blank/frozen-detail collapse — see `SearchView` and the U3 regression guard.
struct SearchResultsView: View {
    let phase: SearchViewModel.Phase
    let consentDeclined: Bool
    /// SCR-178 U8 — drives the "index your existing recordings" affordance, which
    /// shares the consent banner's slot but is keyed on this state (NOT
    /// `consentNeeded`) so it survives the "Turn on" flag flip and shows live
    /// progress.
    let backfillState: SearchViewModel.BackfillUIState
    @Binding var selection: SearchResultItem.ID?
    var frameIndex: RecordingFrameIndex?
    var thumbnailLoader: ThumbnailLoader?
    /// A snapshot of the search field's focus (the live `@FocusState` lives in
    /// `SearchView`). Gates the Return-key handler so it never steals Return from
    /// the field's run-a-search binding.
    let isSearchFieldFocused: Bool
    let onEnableConsent: () -> Void
    let onDeclineConsent: () -> Void
    let onAcceptBackfill: () -> Void
    let onSkipBackfill: () -> Void
    let onCancelBackfill: () -> Void
    let onResumeBackfill: () -> Void
    let onOpen: (SearchResultItem) -> Void

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Content by phase

    @ViewBuilder
    private var content: some View {
        switch phase {
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

        return List(selection: $selection) {
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
                // SCR-178 U8 — the consent banner and the backfill affordance
                // share one slot but are independent: tapping "Turn on" flips the
                // flag (so the banner's `consentNeeded` guard goes false on the
                // next search), and the backfill affordance — keyed on
                // `backfillState`, NOT `consentNeeded` — takes over in place so
                // the progress UI does not vanish the instant indexing is enabled.
                if backfillState != .hidden {
                    backfillAffordance
                        .listRowSeparator(.hidden)
                } else if results.consentNeeded && !consentDeclined {
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
        .simultaneousGesture(TapGesture(count: 2).onEnded { onOpen(item) })
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
        // Also stand down while the backfill affordance is active (offering /
        // starting / indexing) — its prominent Accept/Cancel control should own
        // Return there, not this hidden handler (SCR-178 U8).
        if !isSearchFieldFocused,
           !(results.consentNeeded && !consentDeclined),
           !backfillState.isActive,
           let target = searchReviewTarget(for: selection, in: results) {
            Button("") { onOpen(target) }
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
                Button("Not now") { onDeclineConsent() }
                    .buttonStyle(.bordered)
                Button("Turn on") { onEnableConsent() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(10)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Backfill affordance (SCR-178 U8)

    /// The "also index your existing recordings" affordance. Rendered in the same
    /// slot as `consentBanner` but driven by `backfillState` (NOT `consentNeeded`),
    /// so it survives the "Turn on" flag flip and shows live progress, cancel,
    /// resume, and a defined terminal state for every outcome.
    @ViewBuilder
    private var backfillAffordance: some View {
        VStack(alignment: .leading, spacing: 8) {
            switch backfillState {
            case .hidden:
                EmptyView()

            case .offering:
                affordanceHeader("Index your existing recordings now?")
                Text("Search the on-screen text in recordings you already made \u{2014} it all stays on this Mac and is never uploaded.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    Spacer()
                    Button("Skip") { onSkipBackfill() }
                        .buttonStyle(.bordered)
                    Button("Index now") { onAcceptBackfill() }
                        .buttonStyle(.borderedProminent)
                }

            case .starting:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Preparing to index\u{2026}").font(.callout)
                    Spacer()
                    Button("Cancel") { onCancelBackfill() }
                        .buttonStyle(.bordered)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Preparing to index your existing recordings")

            case .indexing(let done, let total, let failed):
                affordanceHeader("Indexing your existing recordings")
                ProgressView(value: Double(done), total: Double(max(total, 1))) {
                    EmptyView()
                } currentValueLabel: {
                    Text(indexingProgressText(done: done, total: total, failed: failed))
                        .font(.caption).foregroundStyle(.secondary)
                }
                .accessibilityLabel(indexingProgressText(done: done, total: total, failed: failed))
                HStack {
                    Spacer()
                    Button("Cancel") { onCancelBackfill() }
                        .buttonStyle(.bordered)
                }

            case .done(let done, let total, let failed):
                affordanceHeader(failed == 0 ? "All set" : "Indexing finished")
                Text(doneText(done: done, total: total, failed: failed))
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

            case .paused(let done, let total):
                affordanceHeader("Indexing paused")
                Text("Indexed \(done) of \(total) so far \u{2014} resume to continue.")
                    .font(.caption).foregroundStyle(.secondary)
                resumeRow

            case .cancelled:
                affordanceHeader("Indexing paused")
                Text("Indexing paused \u{2014} you can resume later.")
                    .font(.caption).foregroundStyle(.secondary)
                resumeRow

            case .startFailed:
                affordanceHeader("Couldn\u{2019}t start indexing")
                Text("Couldn\u{2019}t start indexing \u{2014} try again later. You can still search what\u{2019}s already indexed.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    private func affordanceHeader(_ title: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "text.viewfinder").foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(title).font(.callout).bold()
            Spacer()
        }
    }

    private var resumeRow: some View {
        HStack {
            Spacer()
            Button("Resume") { onResumeBackfill() }
                .buttonStyle(.borderedProminent)
        }
    }

    /// "Indexed N of M" with the failed count appended only when non-zero
    /// (`skipped` is a privacy-correct outcome and is never surfaced).
    private func indexingProgressText(done: Int, total: Int, failed: Int) -> String {
        var text = "Indexed \(done) of \(total)"
        if failed > 0 { text += " \u{2014} \(failed) couldn\u{2019}t be indexed" }
        return text
    }

    private func doneText(done: Int, total: Int, failed: Int) -> String {
        if failed == 0 {
            return "Done \u{2014} your recording history is now searchable."
        }
        return "Indexed \(done) of \(total) recordings. \(failed) could not be indexed."
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
}

/// SCR-184 U1 — the shared layout seam used by both `SearchView.body` and the U3
/// regression test, so the test exercises the real composition shape and cannot
/// drift from production. Pins a top bar via `.safeAreaInset` above the content;
/// the content self-sizes via `SearchResultsView`'s `.frame(maxHeight: .infinity)`.
/// A List-as-root content here sizes reliably inside the `NavigationSplitView`
/// detail, where nesting a List/ScrollView below siblings in a VStack does not
/// (the SCR-174 starvation).
@ViewBuilder
func searchDetailLayout<Bar: View, Content: View>(
    @ViewBuilder bar: () -> Bar,
    @ViewBuilder content: () -> Content
) -> some View {
    content()
        .safeAreaInset(edge: .top, spacing: 0) {
            VStack(spacing: 0) {
                bar()
                Divider()
            }
            .background(.bar)
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
