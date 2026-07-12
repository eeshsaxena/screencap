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
    /// backfill ride alongside results (banner slots), keeping the retired
    /// Search pane's mutual-exclusivity rule: the backfill affordance
    /// displaces the consent banner while active.
    struct State: Equatable {
        /// SCR-261 — why a searched query rendered zero rows. Derived from the
        /// live settings flag + per-stream coverage, never from the wire's
        /// frozen `consentNeeded` snapshot (that flag flips the moment the
        /// user turns indexing on).
        enum EmptyCause: Equatable {
            /// Everything searched ran fine — genuinely nothing matched.
            case noMatches
            /// On-screen-text indexing is off and the user hasn't declined —
            /// the body owns the Turn-on ask.
            case consentNeeded
            /// Indexing was declined — a button-free honest notice.
            case consentDeclined
            /// Indexing is on but this history isn't indexed yet.
            /// `ctaAvailable` is false while the backfill section owns the ask
            /// (offer / progress / resume states); the done and start-failed
            /// sections carry no action button, so the body CTA returns.
            case notIndexed(ctaAvailable: Bool)
            /// Content search fell back to the LIKE scan (FTS5 absent) —
            /// results may be incomplete.
            case degraded
            /// A searched stream's store errored outright.
            case unavailable
        }

        enum Body: Equatable {
            /// Empty query — curated examples + recent-search chips.
            case idle(recents: [String])
            case searching
            /// Daemon unreachable — the palette's error variant.
            case daemonDown
            /// Lapsed / not-entitled (U12) — the palette's upgrade-CTA variant.
            /// Distinct from `daemonDown` so lapsed search reads as "upgrade to
            /// search", never "search is broken".
            case subscriptionRequired
            case results
            /// Zero rendered rows — carries the honest cause so the body can
            /// say *why* (SCR-261), plus whether a *different* searched stream
            /// failed outright (rendered as one de-emphasized note line).
            case empty(cause: EmptyCause, showsUnavailableNote: Bool)
        }

        let body: Body
        let showsConsentBanner: Bool
        let backfill: SearchViewModel.BackfillUIState
    }

    static func state(
        phase: SearchViewModel.Phase,
        consentDeclined: Bool,
        backfillState: SearchViewModel.BackfillUIState,
        contentIndexEnabled: Bool?,
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
        case .subscriptionRequired:
            body = .subscriptionRequired
        case .loaded(let results):
            consentNeeded = results.consentNeeded
            if results.items.isEmpty {
                body = emptyBody(
                    coverage: results.coverage,
                    contentIndexEnabled: contentIndexEnabled,
                    consentDeclined: consentDeclined,
                    backfillState: backfillState
                )
            } else {
                body = .results
            }
        }
        // KTD6 — the live settings flag is authoritative for the banner too:
        // nil (unresolved) stays quiet per R12, and a stale wire
        // `consentNeeded` with the flag resolved true must not show it.
        var showsBanner = contentIndexEnabled == false && consentNeeded
            && !consentDeclined && backfillState == .hidden
        // SCR-261 U3 (R1/KTD4) — when the empty body owns the Turn-on ask, the
        // banner must not co-render the same question. It stays for `.results`
        // with consentNeeded (rows render, the body carries no ask).
        if case .empty(.consentNeeded, _) = body { showsBanner = false }
        return State(body: body, showsConsentBanner: showsBanner, backfill: backfillState)
    }

    /// SCR-261 — the cause behind zero rendered rows, in precedence order: a
    /// pure time query (content stream not searched) never nags about
    /// indexing; unknown settings (`contentIndexEnabled == nil`) stay quiet;
    /// the consent tier follows the *live* flag; then not-indexed, stream
    /// failure, degraded, and only then a genuine "no matches".
    private static func emptyBody(
        coverage: CoverageReport,
        contentIndexEnabled: Bool?,
        consentDeclined: Bool,
        backfillState: SearchViewModel.BackfillUIState
    ) -> State.Body {
        let searched = [coverage.screen, coverage.audio, coverage.activity]
            .filter { $0 != .notRun }
        func healthOrNoMatches() -> State.Body {
            if searched.contains(.unavailable) {
                return .empty(cause: .unavailable, showsUnavailableNote: false)
            }
            if searched.contains(.degraded) {
                return .empty(cause: .degraded, showsUnavailableNote: false)
            }
            return .empty(cause: .noMatches, showsUnavailableNote: false)
        }
        guard coverage.screen != .notRun else { return healthOrNoMatches() }
        guard let enabled = contentIndexEnabled else {
            return .empty(cause: .noMatches, showsUnavailableNote: false)
        }
        let otherUnavailable = coverage.audio == .unavailable || coverage.activity == .unavailable
        if !enabled {
            return .empty(
                cause: consentDeclined ? .consentDeclined : .consentNeeded,
                showsUnavailableNote: otherUnavailable
            )
        }
        if coverage.screen == .notIndexed {
            return .empty(
                cause: .notIndexed(ctaAvailable: backfillCTAAvailable(backfillState)),
                showsUnavailableNote: otherUnavailable
            )
        }
        return healthOrNoMatches()
    }

    /// Whether the not-indexed empty body may carry its own Index CTA: only
    /// while the backfill section isn't already owning the ask. The done and
    /// start-failed sections render no action button (done is text-only), so
    /// the body CTA returning violates nothing — and for `.done` it prevents a
    /// dead end when a backfill completes with nothing indexed while coverage
    /// stays not-indexed (the state re-derives `.done` across sessions).
    private static func backfillCTAAvailable(_ state: SearchViewModel.BackfillUIState) -> Bool {
        switch state {
        case .hidden, .done, .startFailed:
            return true
        case .offering, .starting, .indexing, .paused, .cancelled:
            return false
        }
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
/// it (the retired SearchView's cancellation pattern, extracted for
/// testability: the esc-cancels test drives this with a hook-counter fake
/// instead of a render).
@MainActor
final class RecallPaletteQueryRunner: ObservableObject {
    /// The live search body — injected so tests can observe start/cancel.
    private let run: (String) async -> Void
    private let debounceNanos: UInt64
    private(set) var searchTask: Task<Void, Never>?
    /// Dedupes the trailing `.onChange` a programmatic query set produces
    /// (the retired SearchView's `lastIssuedQuery` pattern).
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

    /// SCR-261: the backfill finished — re-issue the last issued query so
    /// results reflect the newly built index. No-op while nothing meaningful
    /// has been searched.
    func refreshIfNonEmpty() {
        guard let last = lastIssuedQuery, !last.isEmpty else { return }
        search(last, debounced: false)
    }

    /// esc / scrim-click: cancel whatever is in flight before the palette goes
    /// away, so no stale search publishes into a dismissed panel.
    func cancel() {
        searchTask?.cancel()
        searchTask = nil
    }
}
