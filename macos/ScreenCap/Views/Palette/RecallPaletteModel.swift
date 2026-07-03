import Foundation

// U10 — the Recall palette's pure model layer: the render-variant derivation,
// day grouping with mono headers, keyboard-selection movement (wrapping), and
// the ↵ jump target. Factored out of `RecallPaletteView` so the state and nav
// rules are unit-testable without a render (RecallPaletteStateTests). The
// search stack itself is untouched — this only re-presents `SearchViewModel`
// state (KTD-2).

enum RecallPalette {

    // MARK: - Render state

    /// What the palette body shows for a given search-stack state. Consent and
    /// backfill ride alongside results (banner slots), mirroring
    /// SearchResultsView's mutual-exclusivity rule: the backfill affordance
    /// displaces the consent banner while active.
    struct State: Equatable {
        enum Body: Equatable {
            /// Empty query — curated examples + recent-search chips.
            case idle(recents: [String])
            case searching
            /// Daemon unreachable — the palette's error variant.
            case daemonDown
            case results
            case empty
        }

        let body: Body
        let showsConsentBanner: Bool
        let backfill: SearchViewModel.BackfillUIState
    }

    static func state(
        phase: SearchViewModel.Phase,
        consentDeclined: Bool,
        backfillState: SearchViewModel.BackfillUIState,
        recents: [String]
    ) -> State {
        let body: State.Body
        var consentNeeded = false
        switch phase {
        case .idle:
            body = .idle(recents: recents)
        case .searching:
            body = .searching
        case .daemonDown:
            body = .daemonDown
        case .loaded(let results):
            consentNeeded = results.consentNeeded
            body = results.items.isEmpty ? .empty : .results
        }
        let showsBanner = consentNeeded && !consentDeclined && backfillState == .hidden
        return State(body: body, showsConsentBanner: showsBanner, backfill: backfillState)
    }

    // MARK: - Day grouping

    /// One header group (design 565: mono "TODAY"). Unanchored (time-
    /// approximate audio) hits trail under their own header, off the day axis.
    struct Group: Equatable {
        let label: String
        let items: [SearchResultItem]
    }

    static let unanchoredLabel = "HEARD IN AUDIO · TIME APPROXIMATE"

    /// Group ranked results by local calendar day, newest day first; within a
    /// day the ranking order is preserved. Labels reuse the Journal's day
    /// vocabulary, uppercased to the palette's mono header style.
    static func groups(
        _ results: SearchResults,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> [Group] {
        var byDay: [Date: [SearchResultItem]] = [:]
        var unanchored: [SearchResultItem] = []
        for item in results.items {
            if let ms = item.anchorMs {
                let day = calendar.startOfDay(for: Date(timeIntervalSince1970: Double(ms) / 1000))
                byDay[day, default: []].append(item)
            } else {
                unanchored.append(item)
            }
        }
        var groups: [Group] = byDay
            .sorted { $0.key > $1.key }
            .map { day, items in
                Group(
                    label: JournalModel.label(for: day, now: now, calendar: calendar).uppercased(),
                    items: items
                )
            }
        if !unanchored.isEmpty {
            groups.append(Group(label: unanchoredLabel, items: unanchored))
        }
        return groups
    }

    /// The keyboard-navigation order: the flattened visual order of the groups.
    static func orderedItems(
        _ results: SearchResults,
        now: Date = Date(),
        calendar: Calendar = .current
    ) -> [SearchResultItem] {
        groups(results, now: now, calendar: calendar).flatMap(\.items)
    }

    // MARK: - Selection

    /// The row rendered (and jumped to) as selected: the explicit selection if
    /// it still resolves, else the first row — the design's highlighted lead
    /// result (566).
    static func effectiveSelection(
        _ id: SearchResultItem.ID?,
        in ordered: [SearchResultItem]
    ) -> SearchResultItem? {
        ordered.first { $0.id == id } ?? ordered.first
    }

    /// ↓ — the next row, wrapping past the end. A nil/stale selection moves
    /// from the effective (first) row.
    static func next(
        after id: SearchResultItem.ID?,
        in ordered: [SearchResultItem]
    ) -> SearchResultItem.ID? {
        guard !ordered.isEmpty else { return nil }
        guard let current = ordered.firstIndex(where: { $0.id == id }) else { return ordered.first?.id }
        return ordered[(current + 1) % ordered.count].id
    }

    /// ↑ — the previous row, wrapping past the start.
    static func previous(
        before id: SearchResultItem.ID?,
        in ordered: [SearchResultItem]
    ) -> SearchResultItem.ID? {
        guard !ordered.isEmpty else { return nil }
        guard let current = ordered.firstIndex(where: { $0.id == id }) else { return ordered.first?.id }
        return ordered[(current + ordered.count - 1) % ordered.count].id
    }

    // MARK: - Jump target

    /// Where ↵ lands: the Day timeline for the hit's day, seeked to the hit
    /// timestamp (U9 resolves the nearest frame from its chunk map). nil for an
    /// unanchored hit — there is no moment to land on.
    static func timelineTarget(
        for item: SearchResultItem,
        calendar: Calendar = .current
    ) -> (day: Date, seekMs: Int)? {
        guard let ms = item.anchorMs else { return nil }
        let day = calendar.startOfDay(for: Date(timeIntervalSince1970: Double(ms) / 1000))
        return (day: day, seekMs: ms)
    }
}

/// U10 — owns the palette's in-flight query task so dismissal provably cancels
/// it (the existing SearchView cancellation pattern, extracted for testability:
/// the esc-cancels test drives this with a hook-counter fake instead of a
/// render).
@MainActor
final class RecallPaletteQueryRunner: ObservableObject {
    /// The live search body — injected so tests can observe start/cancel.
    private let run: (String) async -> Void
    private let debounceNanos: UInt64
    private(set) var searchTask: Task<Void, Never>?
    /// Dedupes the trailing `.onChange` a programmatic query set produces
    /// (the SearchView `lastIssuedQuery` pattern).
    private var lastIssuedQuery: String?

    init(debounceNanos: UInt64 = 300_000_000, run: @escaping (String) async -> Void) {
        self.debounceNanos = debounceNanos
        self.run = run
    }

    func search(_ query: String, debounced: Bool) {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if debounced, trimmed == lastIssuedQuery { return }
        searchTask?.cancel()
        if !debounced { lastIssuedQuery = trimmed }
        let nanos = debounceNanos
        searchTask = Task { @MainActor in
            if debounced {
                try? await Task.sleep(nanoseconds: nanos)
                if Task.isCancelled { return }
                lastIssuedQuery = trimmed
            }
            await run(trimmed)
        }
    }

    /// esc / scrim-click: cancel whatever is in flight before the palette goes
    /// away, so no stale search publishes into a dismissed panel.
    func cancel() {
        searchTask?.cancel()
        searchTask = nil
    }
}
